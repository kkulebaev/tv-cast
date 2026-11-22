from dataclasses import replace
from pathlib import Path

import pytest

from tv_cast import media, posters
from tv_cast.config import settings


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    root.mkdir()
    monkeypatch.setattr(posters, "settings", replace(settings, media_dir=root,
                                                     state_dir=tmp_path / "state"))
    return root


def item(root: Path, rel: str) -> media.Item:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    ep = media.episode_info(rel)
    return media.Item("id", path, rel, 0, 0.0, *(ep or ("", 0, 0)))


def probe(**kw) -> media.Probe:
    return media.Probe(**{**dict(duration=3000.0, format_name="", video_codec="h264",
                                 video_profile="", pix_fmt="", width=0, height=0), **kw})


def test_embedded_cover_wins_over_a_frame(lib):
    film = item(lib, "Film.mkv")
    assert posters.source(film, probe(cover=3)) == ("cover", 3)
    assert posters.source(film, probe()) == ("frame", 300.0)


def test_image_named_like_the_film(lib):
    film = item(lib, "Film.2020.mkv")
    (lib / "Film.2020-poster.png").touch()
    assert posters.source(film, probe()) == ("image", lib / "Film.2020-poster.png")


def test_folder_poster_counts_only_inside_the_films_own_folder(lib):
    (lib / "poster.jpg").touch()  # the library root is shared by every film
    loose = item(lib, "Loose.mkv")
    assert posters.source(loose, probe())[0] == "frame"
    film = item(lib, "Film (2020)/Film.mkv")
    (lib / "Film (2020)" / "folder.jpg").touch()
    assert posters.source(film, probe()) == ("image", lib / "Film (2020)" / "folder.jpg")


def test_series_art_comes_from_the_show_folder_above_the_season(lib):
    ep = item(lib, "Dark/Season 1/Dark.S01E02.mkv")
    (lib / "Dark" / "poster.jpg").touch()
    assert posters.source(ep, probe(), series=True) == ("image", lib / "Dark" / "poster.jpg")
    # The episode itself gets its own frame, not the show's poster.
    assert posters.source(ep, probe()) == ("frame", 300.0)


def test_unknown_duration_still_gets_a_frame(lib):
    assert posters.source(item(lib, "Film.mkv"), probe(duration=0)) == ("frame", 30.0)


def test_hdr_frames_are_tonemapped(lib, monkeypatch):
    monkeypatch.setattr(media, "gpu_tonemap", lambda: True)
    film = item(lib, "Film.mkv")
    out = lib / "x.jpg"
    sdr = posters.ffmpeg_cmd(film, probe(), ("frame", 300.0), out)
    hdr = posters.ffmpeg_cmd(film, probe(color_transfer="smpte2084"), ("frame", 300.0), out)
    assert sdr[sdr.index("-vf") + 1] == "scale=480:-2"
    assert hdr[hdr.index("-vf") + 1].startswith("libplacebo=")
    assert sdr.index("-ss") < sdr.index("-i")


def test_get_caches_by_source(lib, monkeypatch):
    film = item(lib, "Film.mkv")
    runs = []

    def fake_ffmpeg(cmd, **kw):
        runs.append(cmd)
        Path(cmd[-1]).write_bytes(b"jpeg")

    monkeypatch.setattr(media, "probe", lambda i: probe())
    monkeypatch.setattr(posters.subprocess, "run", fake_ffmpeg)
    first = posters.get(film)
    assert posters.get(film) == first and len(runs) == 1
    assert first.read_bytes() == b"jpeg"
    assert list(first.parent.glob("*.tmp.jpg")) == []
    # A poster dropped next to the film later makes a new picture, not the cached frame.
    (lib / "Film.jpg").touch()
    assert posters.get(film) != first and len(runs) == 2


def test_failed_ffmpeg_leaves_no_partial_file(lib, monkeypatch):
    film = item(lib, "Film.mkv")

    def broken(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"half")
        raise posters.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(media, "probe", lambda i: probe())
    monkeypatch.setattr(posters.subprocess, "run", broken)
    with pytest.raises(posters.subprocess.CalledProcessError):
        posters.get(film)
    (marker,) = (lib.parent / "state" / "posters").iterdir()
    assert marker.suffix == ".fail"  # no half picture, only the note that it failed

    def must_not_run(cmd, **kw):
        raise AssertionError("ffmpeg ran again for a known failure")

    monkeypatch.setattr(posters.subprocess, "run", must_not_run)
    with pytest.raises(posters.NoPoster):
        posters.get(film)


def test_ffmpeg_without_output_counts_as_failure(lib, monkeypatch):
    film = item(lib, "Film.mkv")
    monkeypatch.setattr(media, "probe", lambda i: probe())
    monkeypatch.setattr(posters.subprocess, "run", lambda cmd, **kw: None)
    with pytest.raises(FileNotFoundError):
        posters.get(film)
    with pytest.raises(posters.NoPoster):
        posters.get(film)


def test_new_picture_replaces_the_old_one(lib, monkeypatch):
    film = item(lib, "Film.mkv")
    monkeypatch.setattr(media, "probe", lambda i: probe())
    monkeypatch.setattr(posters.subprocess, "run",
                        lambda cmd, **kw: Path(cmd[-1]).write_bytes(b"jpeg"))
    frame = posters.get(film)
    show = posters.get(film, series=True)  # its own picture, not swept with the item's
    (lib / "Film.jpg").touch()
    image = posters.get(film)
    assert not frame.exists() and image.exists() and show.exists()


def test_art_names_are_matched_case_blind(lib):
    film = item(lib, "Film (2020)/Film.mkv")
    (lib / "Film (2020)" / "Folder.JPG").touch()
    assert posters.source(film, probe()) == ("image", lib / "Film (2020)" / "Folder.JPG")


def test_percent_in_an_image_name_is_not_a_pattern(lib):
    film = item(lib, "100% Film.mkv")
    cmd = posters.ffmpeg_cmd(film, probe(), ("image", lib / "100% Film.jpg"), Path("out.jpg"))
    assert cmd[cmd.index("-pattern_type") + 1] == "none"
    # -pattern_type exists only in image2; a probed jpeg_pipe input would reject it.
    assert cmd[cmd.index("-f") + 1] == "image2"
    assert cmd.index("-f") < cmd.index("-pattern_type") < cmd.index("-i")
