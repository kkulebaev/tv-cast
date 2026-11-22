import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tv_cast import app, cast, cli, live, progress


@pytest.mark.parametrize(
    ("value", "name"),
    [
        ("dota2_paragon_ru", "dota2_paragon_ru"),
        ("https://www.twitch.tv/Dota2_Paragon_RU/", "dota2_paragon_ru"),
        ("twitch.tv/shroud?sr=a", "shroud"),
    ],
)
def test_channel_name_accepts_logins_and_links(value, name):
    assert live.channel_name(value) == name


@pytest.mark.parametrize("value", ["", "two words", "chan!", "x" * 26])
def test_channel_name_rejects_anything_else(value):
    with pytest.raises(ValueError):
        live.channel_name(value)


def fake_cmds(stream, segments=3, source_runs=True):
    source = [sys.executable, "-c", "import time; time.sleep(30)" if source_runs else "pass"]
    body = "".join(f"seg{i}.ts\\n" for i in range(segments))
    writer = (f"import time; open({str(stream.playlist)!r}, 'w').write('#EXTM3U\\n{body}');"
              " time.sleep(30)")
    return [source, [sys.executable, "-c", writer]]


def test_ready_once_enough_segments_are_written():
    s = live.Stream("chan")
    s.start(fake_cmds(s))
    try:
        assert s.wait_ready(timeout=10, every=0.05)
        assert s.alive
        assert s.file("live.m3u8") == s.playlist
    finally:
        s.stop()
    assert not s.dir.exists()
    assert not s.alive


def test_offline_channel_fails_fast():
    s = live.Stream("chan")
    s.start(fake_cmds(s, segments=0, source_runs=False))
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="офлайн"):
            s.wait_ready(timeout=10, every=0.05)
    finally:
        s.stop()
    assert time.monotonic() - started < 5


def test_wait_gives_up_when_cancelled():
    s = live.Stream("chan")
    s.start(fake_cmds(s, segments=1))
    try:
        assert s.wait_ready(cancelled=lambda: True) is False
    finally:
        s.stop()


@pytest.mark.parametrize("name", ["../live.m3u8", "x.ts", "seg1.ts.bak", "live.m3u8/.."])
def test_only_muxer_files_are_served(name):
    s = live.Stream("chan")
    (s.dir / "x.ts").write_text("")
    try:
        assert s.file(name) is None
    finally:
        s.stop()


class FakeStream:
    def __init__(self, channel, quality="best"):
        self.channel, self.quality, self.key = channel, quality, "k" + channel
        self.alive, self.stopped = True, False

    def start(self):
        pass

    def wait_ready(self, cancelled=lambda: False):
        return not cancelled()

    def stop(self):
        self.stopped, self.alive = True, False

    def file(self, name):
        return None


def tv():
    calls = []
    mc = SimpleNamespace(
        play_media=lambda url, ctype, **kw: calls.append(("play", url, ctype, kw)),
        block_until_active=lambda timeout: None,
        stop=lambda: calls.append("stop"),
        status=SimpleNamespace(content_id="", player_state="PLAYING", adjusted_current_time=9.0),
    )
    return SimpleNamespace(calls=calls, media_controller=mc, quit_app=lambda: calls.append("quit"),
                           cast_info=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(live, "Stream", FakeStream)
    m = cast.CastManager()
    m._casts["tv"] = tv()
    m.get = lambda uuid: m._casts[uuid]
    return m


def test_play_live_casts_the_local_playlist(manager):
    s = manager.play_live("tv", "chan", "720p")
    (_, url, ctype, kw), = manager._casts["tv"].calls
    assert url.endswith("/live/kchan/live.m3u8") and url == s.url
    assert (ctype, kw["stream_type"]) == ("application/x-mpegURL", cast.STREAM_TYPE_LIVE)
    assert manager.sessions["tv"] is s and s.channel == "chan"
    assert manager.lives["tv"].quality == "720p"


def test_live_keeps_no_watch_history(manager):
    s = manager.play_live("tv", "chan")
    manager._casts["tv"].media_controller.status.content_id = s.url
    manager.record("tv")
    cast._Listener(manager, "tv").new_media_status(SimpleNamespace(
        player_state="IDLE", idle_reason="FINISHED", content_id=s.url, current_time=5.0))
    assert progress.store.all() == {}
    assert "tv" not in manager.sessions and "tv" not in manager.finished
    assert s.channel and manager.lives == {}


def test_stop_ends_the_relay(manager):
    manager.play_live("tv", "chan")
    stream = manager.lives["tv"]
    manager.control("tv", "stop")
    assert stream.stopped and manager.lives == {} and manager.sessions == {}


def test_a_new_channel_replaces_the_old_relay(manager):
    manager.play_live("tv", "one")
    first = manager.lives["tv"]
    manager.play_live("tv", "two")
    assert first.stopped and manager.lives["tv"].channel == "two"


def test_ended_broadcast_stops_the_tv_and_says_why(manager):
    manager.play_live("tv", "chan")
    stream = manager.lives["tv"]
    stream.alive = False
    manager._live_ended("tv", stream)
    assert manager.sessions == {} and manager.lives == {}
    assert "закончилась" in manager.errors["tv"]
    assert manager._casts["tv"].calls[-2:] == ["stop", "quit"]


def test_live_ignores_seek(manager):
    manager.play_live("tv", "chan")
    manager._casts["tv"].media_controller.seek = lambda t: pytest.fail("seeked a live stream")
    manager.control("tv", "seek", 30)


def test_live_routes(monkeypatch, tmp_path):
    playlist = tmp_path / "live.m3u8"
    playlist.write_text("#EXTM3U\n")
    monkeypatch.setattr(app.manager, "live_file",
                        lambda key, name: playlist if (key, name) == ("k", "live.m3u8") else None)
    # Without the lifespan: it would start TV discovery on the real network.
    client = TestClient(app.app)
    r = client.get("/live/k/live.m3u8")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.apple.mpegurl"
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/live/other/live.m3u8").status_code == 404
    r = client.post("/api/live", json={"device": "tv", "channel": "not a channel"})
    assert r.status_code == 400


DEVICES = [
    {"id": "1", "name": "TV-N52", "model": "TV-N52", "host": "192.168.3.35"},
    {"id": "2", "name": "Телевизор в гостиной", "model": "MiTV-MSSP3", "host": "192.168.3.4"},
]


@pytest.mark.parametrize(("wanted", "picked"),
                         [("n52", "1"), ("гостин", "2"), ("mitv", "2"), ("192.168.3.4", "2")])
def test_cli_picks_the_tv_by_name_model_or_ip(wanted, picked):
    assert cli.pick_device(DEVICES, wanted)["id"] == picked


@pytest.mark.parametrize("wanted", ["", "kitchen", "tv"])
def test_cli_refuses_to_guess_between_tvs(wanted):
    with pytest.raises(SystemExit):
        cli.pick_device(DEVICES, wanted)
