import time
from types import SimpleNamespace

import pytest

from tv_cast import cast, progress


def manager_with_session(url="http://pc/stream/1.mp4?start=0"):
    m = cast.CastManager()
    m.sessions["tv"] = cast.Session(
        item_id="1", title="Film", mode="remux", reason="", audio_index=None, sub_key=None,
        force_transcode=False, duration=100.0, url=url,
    )
    return m, cast._Listener(m, "tv")


def status(state, reason=None, content_id="http://pc/stream/1.mp4?start=0", current_time=None):
    return SimpleNamespace(player_state=state, idle_reason=reason, content_id=content_id,
                           current_time=current_time)


def test_finished_playback_ends_the_session_and_marks_watched():
    m, listener = manager_with_session()
    listener.new_media_status(status("IDLE", "FINISHED"))
    assert "tv" not in m.sessions
    assert progress.store.get("1")["watched"] is True
    assert progress.store.path.exists()


def test_reload_after_seek_keeps_the_session():
    m, listener = manager_with_session(url="http://pc/stream/1.mp4?start=600")
    # The URL being replaced reports INTERRUPTED, and a late FINISHED for it isn't ours either.
    listener.new_media_status(status("IDLE", "INTERRUPTED"))
    listener.new_media_status(status("IDLE", "FINISHED"))
    listener.new_media_status(status("PLAYING"))
    assert "tv" in m.sessions


def test_error_keeps_the_session_for_its_message():
    m, listener = manager_with_session()
    listener.new_media_status(status("IDLE", "ERROR"))
    assert "tv" in m.sessions


def test_cancel_from_the_tv_ends_the_session():
    m, listener = manager_with_session()
    listener.new_media_status(status("IDLE", "CANCELLED"))
    assert "tv" not in m.sessions


def test_finish_of_another_url_keeps_the_session():
    m, listener = manager_with_session()
    listener.new_media_status(status("IDLE", "FINISHED", content_id="http://pc/media/2.mp4"))
    assert "tv" in m.sessions


def test_stale_end_does_not_remove_a_newer_session():
    m, _ = manager_with_session()
    old = m.sessions["tv"]
    newer, _ = manager_with_session()
    m.sessions["tv"] = newer.sessions["tv"]
    m.end_session("tv", old)
    assert m.sessions["tv"] is newer.sessions["tv"]


def fake_cast(app_id):
    return SimpleNamespace(
        app_id=app_id, app_display_name="",
        media_controller=SimpleNamespace(status=SimpleNamespace(
            player_state="PLAYING", idle_reason=None, adjusted_current_time=5.0, duration=50.0)),
        status=SimpleNamespace(volume_level=0.5, volume_muted=False),
    )


@pytest.mark.parametrize(
    ("app_id", "age", "kept"),
    [
        ("233637DE", 60, False),  # YouTube took over the TV
        ("233637DE", 5, True),  # our receiver may still be starting
        (cast.APP_MEDIA_RECEIVER, 600, True),
        (None, 600, True),
    ],
)
def test_status_drops_session_when_another_app_runs(app_id, age, kept):
    m, _ = manager_with_session()
    m.sessions["tv"].started_at = time.time() - age
    m.sessions["tv"].offset = 100.0
    m.get = lambda uuid: fake_cast(app_id)
    st = m.status("tv")
    assert ("tv" in m.sessions) is kept
    assert (st["session"] is not None) is kept
    assert st["position"] == (105.0 if kept else 5.0)


def playing_cast(content_id, state="PLAYING", t=500.0):
    calls = []
    return SimpleNamespace(
        calls=calls,
        media_controller=SimpleNamespace(
            status=SimpleNamespace(content_id=content_id, player_state=state,
                                   adjusted_current_time=t),
            stop=lambda: calls.append("stop")),
        quit_app=lambda: calls.append("quit"),
    )


@pytest.mark.parametrize(
    ("content_id", "state", "saved"),
    [
        ("http://pc/stream/1.mp4?start=0", "PLAYING", 1500.0),
        ("http://pc/stream/1.mp4?start=0", "PAUSED", 1500.0),
        ("http://pc/stream/1.mp4?start=0", "BUFFERING", None),
        # Right after a seek the receiver still reports the previous URL's clock.
        ("http://pc/stream/1.mp4?start=900", "PLAYING", None),
    ],
)
def test_record_saves_the_film_position(content_id, state, saved):
    m, _ = manager_with_session()
    m.sessions["tv"].offset = 1000.0
    m.sessions["tv"].duration = 7200.0
    m._casts["tv"] = playing_cast(content_id, state)
    m.record("tv")
    assert progress.store.get("1").get("pos") == saved


def test_finished_without_known_duration_still_marks_watched():
    m, listener = manager_with_session()
    m.sessions["tv"].duration = 0.0
    listener.new_media_status(status("IDLE", "FINISHED"))
    assert progress.store.get("1")["watched"] is True


def test_stop_saves_the_position_before_stopping():
    m, _ = manager_with_session()
    m.sessions["tv"].duration = 7200.0
    tv = playing_cast("http://pc/stream/1.mp4?start=0", t=900.0)
    m._casts["tv"] = tv
    m.get = lambda uuid: tv
    m.control("tv", "stop")
    assert tv.calls == ["stop", "quit"]
    assert progress.store.get("1")["pos"] == 900.0
    assert progress.store.path.exists()


def test_failing_save_does_not_block_stop(monkeypatch):
    m, _ = manager_with_session()
    tv = playing_cast("http://pc/stream/1.mp4?start=0")
    m._casts["tv"] = tv
    m.get = lambda uuid: tv

    def disk_full():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(progress.store, "flush", disk_full)
    m.control("tv", "stop")
    assert tv.calls == ["stop", "quit"]
    m.save(None)  # what the tracker thread runs each round: must not raise either


def test_broken_off_stream_is_not_marked_watched():
    m, listener = manager_with_session()
    s = m.sessions["tv"]
    s.duration, s.last_pos = 3600.0, 1200.0
    listener.new_media_status(status("IDLE", "FINISHED"))
    assert progress.store.get("1") == {}
    assert "tv" not in m.sessions


def test_direct_seek_to_the_credits_then_finished_counts():
    m, listener = manager_with_session()
    s = m.sessions["tv"]
    s.mode, s.duration, s.last_pos = "direct", 3600.0, 600.0
    tv = playing_cast(s.url)
    tv.media_controller.seek = lambda t: None
    m.get = lambda uuid: tv
    m.control("tv", "seek", 3560)
    listener.new_media_status(status("IDLE", "FINISHED"))
    assert progress.store.get("1")["watched"] is True
