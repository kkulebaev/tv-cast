import time
from types import SimpleNamespace

import pytest

from tv_cast import cast


def manager_with_session(url="http://pc/stream/1.mp4?start=0"):
    m = cast.CastManager()
    m.sessions["tv"] = cast.Session(
        item_id="1", title="Film", mode="remux", reason="", audio_index=None, sub_key=None,
        force_transcode=False, duration=100.0, url=url,
    )
    return m, cast._Listener(m, "tv")


def status(state, reason=None, content_id="http://pc/stream/1.mp4?start=0"):
    return SimpleNamespace(player_state=state, idle_reason=reason, content_id=content_id)


def test_finished_playback_ends_the_session():
    m, listener = manager_with_session()
    listener.new_media_status(status("IDLE", "FINISHED"))
    assert "tv" not in m.sessions


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
