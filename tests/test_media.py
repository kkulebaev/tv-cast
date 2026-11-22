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


@pytest.fixture
def no_vaapi(monkeypatch):
    monkeypatch.setattr(media, "settings", replace(settings, vaapi_device="/nonexistent"))


def item(path="/m/film.mkv") -> media.Item:
    return media.Item("id", Path(path), "film.mkv", 1, 0.0)


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
