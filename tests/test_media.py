from dataclasses import replace
from pathlib import Path

import pytest

from tv_cast import media
from tv_cast.config import settings
from tv_cast.media import Probe, Track


def probe(**kw) -> Probe:
    base = dict(
        duration=100.0, format_name=media.MP4_FORMAT, video_codec="h264",
        video_profile="High", pix_fmt="yuv420p", width=1920, height=1080,
        audio=[Track(index=1, codec="aac", channels=2, default=True)],
    )
    return Probe(**{**base, **kw})


@pytest.mark.parametrize(
    ("kw", "audio", "force", "mode"),
    [
        ({}, None, False, "direct"),
        ({}, None, True, "transcode"),
        ({"format_name": media.WEBM_FORMAT, "video_codec": "vp9",
          "audio": [Track(1, "opus", default=True)]}, None, False, "direct"),
        # ffprobe reports MKV as matroska,webm too; H.264 inside means it isn't a real WebM.
        ({"format_name": media.WEBM_FORMAT}, None, False, "remux"),
        ({"audio": [Track(1, "ac3", default=True)]}, None, False, "remux"),
        ({"audio": [Track(1, "aac", default=True), Track(2, "aac")]}, 2, False, "remux"),
        ({"audio": [Track(1, "aac", default=True), Track(2, "aac")]}, 1, False, "direct"),
        ({"audio": []}, None, False, "direct"),
        ({"video_profile": "High 10", "pix_fmt": "yuv420p10le"}, None, False, "transcode"),
        ({"video_codec": "hevc", "video_profile": "Main 10", "pix_fmt": "yuv420p10le"},
         None, False, "direct"),
        ({"video_codec": "mpeg4"}, None, False, "transcode"),
    ],
)
def test_plan_mode(kw, audio, force, mode):
    assert media.plan(probe(**kw), audio, force)[0] == mode


def test_plan_mentions_hdr_tonemapping():
    _, reason = media.plan(probe(color_transfer="smpte2084"), None, force_transcode=True)
    assert "HDR → SDR" in reason


@pytest.fixture
def no_vaapi(monkeypatch):
    monkeypatch.setattr(media, "settings", replace(settings, vaapi_device="/nonexistent"))


@pytest.fixture
def vaapi(monkeypatch, tmp_path):
    dev = tmp_path / "renderD128"
    dev.touch()
    monkeypatch.setattr(media, "settings", replace(settings, vaapi_device=str(dev)))
    return dev


def item(path="/m/film.mkv") -> media.Item:
    return media.Item("id", Path(path), "film.mkv", 1, 0.0)


def vf(cmd: list[str]) -> str:
    return cmd[cmd.index("-vf") + 1]


def test_remux_copies_video_and_converts_audio(no_vaapi):
    cmd = media.ffmpeg_stream_cmd(
        item(), probe(audio=[Track(3, "dts", channels=6, default=True)]), "remux", None, 12.5)
    assert cmd[cmd.index("-ss") + 1] == "12.500"
    assert ["-c:v", "copy"] == cmd[cmd.index("-c:v"):cmd.index("-c:v") + 2]
    assert "0:3" in cmd
    assert cmd[cmd.index("-c:a") + 1] == "aac"


def test_remux_tags_hevc_as_hvc1(no_vaapi):
    cmd = media.ffmpeg_stream_cmd(item(), probe(video_codec="hevc"), "remux", None, 0)
    assert "hvc1" in cmd
    assert "-ss" not in cmd


def test_transcode_sdr_decodes_on_gpu_with_software_fallback(vaapi):
    cmd = media.ffmpeg_stream_cmd(item(), probe(), "transcode", None, 0)
    assert cmd.index("-hwaccel") < cmd.index("-i")
    assert vf(cmd) == "format=nv12|vaapi,hwupload,scale_vaapi=format=nv12"
    assert "h264_vaapi" in cmd
    assert "-color_trc" not in cmd


def test_transcode_hdr_uses_libplacebo_on_gpu(vaapi, monkeypatch):
    monkeypatch.setattr(media, "gpu_tonemap", lambda: True)
    cmd = media.ffmpeg_stream_cmd(item(), probe(color_transfer="smpte2084"), "transcode", None, 0)
    assert cmd.index("-hwaccel") < cmd.index("-i")
    assert vf(cmd).startswith("libplacebo=") and vf(cmd).endswith(",hwupload")
    assert cmd[cmd.index("-color_trc") + 1] == "bt709"


def test_transcode_hdr_without_vulkan_tonemaps_on_cpu(vaapi, monkeypatch):
    monkeypatch.setattr(media, "gpu_tonemap", lambda: False)
    hlg = probe(color_transfer="arib-std-b67")
    cmd = media.ffmpeg_stream_cmd(item(), hlg, "transcode", None, 0)
    # zscale can't take VAAPI frames, so decoding has to stay in software.
    assert "-hwaccel" not in cmd
    assert "tonemap=tonemap=hable" in vf(cmd)


def test_transcode_without_vaapi_uses_libx264(no_vaapi):
    cmd = media.ffmpeg_stream_cmd(item(), probe(), "transcode", None, 0)
    assert "libx264" in cmd
    assert "-vf" not in cmd
    assert "-init_hw_device" not in cmd


VTT = """WEBVTT

1
00:00:05.000 --> 00:00:07.000
gone

2
00:00:09.000 --> 00:00:12.500 align:start
straddles

3
01:00:10.250 --> 01:00:11.000
later
"""


def test_shift_vtt_moves_drops_and_clamps_cues():
    out = media.shift_vtt(VTT.replace("\n", "\r\n"), 10.0)
    assert "gone" not in out
    assert "00:00:00.000 --> 00:00:02.500 align:start\nstraddles" in out
    assert "01:00:00.250 --> 01:00:01.000" in out
    assert out.startswith("WEBVTT")


def test_shift_vtt_without_offset_is_identity():
    assert media.shift_vtt(VTT, 0) == VTT


@pytest.mark.parametrize("key", ["", "e", "x", "eabc", "y1", "e1;rm", "../x0", "e\u0661"])
def test_subs_cmd_rejects_malformed_keys(key):
    assert media.ffmpeg_subs_cmd(item(), key) is None


def test_sidecar_subs_and_languages(tmp_path):
    film = tmp_path / "Film.mkv"
    for name in ["Film.mkv", "Film.srt", "Film.ru.srt", "Film.forced.en.vtt", "Film.en.hi.srt",
                 "Film 2.srt", "Other.srt", "Film.nfo"]:
        (tmp_path / name).touch()
    subs = media._sidecar_subs(film)
    assert [p.name for p in subs] == [
        "Film.en.hi.srt", "Film.forced.en.vtt", "Film.ru.srt", "Film.srt"]
    assert [media._sidecar_lang(p, film) for p in subs] == ["en", "en", "ru", ""]
    cmd = media.ffmpeg_subs_cmd(media.Item("id", film, "Film.mkv", 0, 0), "x2")
    assert str(tmp_path / "Film.ru.srt") in cmd
    assert media.ffmpeg_subs_cmd(media.Item("id", film, "Film.mkv", 0, 0), "x9") is None


@pytest.mark.parametrize(("raw", "code"), [("rus", "ru"), ("ENG", "en"), ("de", "de"),
                                          ("", "und"), ("forced", "und"), ("xyz", "und")])
def test_bcp47(raw, code):
    assert media.bcp47(raw) == code


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("TheWitcherWebRip720pS01x04.mkv", ("The Witcher", 1, 4)),
        ("The.Witcher.S02E10.1080p.WEB-DL.Rus.mkv", ("The Witcher", 2, 10)),
        ("Dark - S03E08 [1080p].mkv", ("Dark", 3, 8)),
        ("Friends 1x04.avi", ("Friends", 1, 4)),
        ("Show/Show.s1.e4.mkv", ("Show", 1, 4)),
        ("Ведьмак/Season 1/S01E04.mkv", ("Ведьмак", 1, 4)),
        ("Ведьмак/Сезон 2/02x05.mkv", ("Ведьмак", 2, 5)),
        ("S01E01.mkv", ("Без названия", 1, 1)),
        ("Nevidimyi_gost`_2016_BDRip_by_Dalemake.avi", None),
        ("Movie.1920x1080.mkv", None),
        ("Film.x264.mkv", None),
        ("Jaws2.x264.mkv", None),
        ("Cars3.x265.mkv", None),
        ("Rocky.2x264.mkv", None),
        ("Pixels2x04.mkv", None),
        ("10x10.2018.mkv", None),
        ("Сериал/1 сезон/S01E02.mkv", ("Сериал", 1, 2)),
        ("Show/Specials/S00E01.mkv", ("Show", 0, 1)),
        ("The.Witcher.2019.S01E01.mkv", ("The Witcher", 1, 1)),
        ("iZombie.S02E03.mkv", ("iZombie", 2, 3)),
        ("Show_S01E01.mkv", ("Show", 1, 1)),
    ],
)
def test_episode_info(rel, expected):
    assert media.episode_info(rel) == expected


def test_scan_finds_videos_recursively(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "settings", replace(settings, media_dir=tmp_path))
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "Film.MKV").write_bytes(b"x" * 3)
    (tmp_path / "a.mp4").touch()
    (tmp_path / "notes.txt").touch()
    items = media.scan()
    assert [i.rel for i in items] == ["a.mp4", "b/Film.MKV"]
    assert media.get(items[1].id).size == 3
    assert media.get("missing") is None
    assert (items[0].series, items[1].series) == ("", "")


def test_scan_tags_episodes(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "settings", replace(settings, media_dir=tmp_path))
    (tmp_path / "Dark.S01E02.mkv").touch()
    (ep,) = media.scan()
    assert (ep.series, ep.season, ep.episode) == ("Dark", 1, 2)
