import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tv_cast import cast, media, progress


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


@pytest.fixture
def series(monkeypatch):
    """E01 playing with a Russian dub (stream 2); in E02 the same dub is stream 3."""
    ep1 = media.Item("1", Path("/s/Dark.S01E01.mkv"), "Dark.S01E01.mkv", 1, 0, "Dark", 1, 1)
    ep2 = media.Item("2", Path("/s/Dark.S01E02.mkv"), "Dark.S01E02.mkv", 1, 0, "Dark", 1, 2)
    probes = {
        "1": media.Probe(3600, media.MP4_FORMAT, "h264", "High", "yuv420p", 1920, 1080, audio=[
            media.Track(1, "aac", "eng", default=True), media.Track(2, "aac", "rus")],
            subtitles=[media.SubTrack("e4", "eng", "eng")]),
        "2": media.Probe(3600, media.MP4_FORMAT, "h264", "High", "yuv420p", 1920, 1080, audio=[
            media.Track(1, "aac", "eng", default=True), media.Track(3, "aac", "rus")],
            subtitles=[media.SubTrack("e5", "eng", "eng")]),
    }
    monkeypatch.setattr(media, "get", {"1": ep1, "2": ep2}.get)
    monkeypatch.setattr(media, "next_episode", lambda i: ep2 if i is ep1 else None)
    monkeypatch.setattr(media, "probe", lambda i: probes[i.id])
    m, listener = manager_with_session(url="http://pc/stream/1.mp4?start=0")
    s = m.sessions["tv"]
    s.audio_index, s.sub_key, s.duration, s.last_pos = 2, "e4", 3600.0, 3595.0
    started = threading.Event()
    m.real_play = m.play
    m.play = lambda *args, **kw: (m.calls.append(args), started.set())
    m.calls, m.started = [], started
    return m, listener


def test_finished_episode_queues_the_next_with_matching_tracks(series, monkeypatch):
    m, listener = series
    monkeypatch.setattr(cast, "settings", replace(cast.settings, autoplay=True))
    queue = m.queue_next
    monkeypatch.setattr(m, "queue_next", lambda uuid, s, gen: queue(uuid, s, gen, delay=0.05))
    listener.new_media_status(status("IDLE", "FINISHED"))
    assert m.started.wait(5)
    ((uuid, item, audio_index, sub_key, start, force),) = m.calls
    assert (uuid, item.id, audio_index, sub_key, start) == ("tv", "2", 3, "e5", 0.0)
    assert progress.store.get("1")["watched"] is True
    assert m.pending == {}


def test_broken_off_stream_is_neither_watched_nor_followed(series, monkeypatch):
    m, listener = series
    monkeypatch.setattr(cast, "settings", replace(cast.settings, autoplay=True))
    m.sessions["tv"].last_pos = 1200.0
    listener.new_media_status(status("IDLE", "FINISHED"))
    assert progress.store.get("1") == {}
    assert m.pending == {}


def test_autoplay_off_only_marks_watched(series):
    m, listener = series
    listener.new_media_status(status("IDLE", "FINISHED"))
    assert progress.store.get("1")["watched"] is True
    assert m.pending == {} and m.calls == []


def test_cancel_drops_the_queued_episode(series):
    m, _ = series
    m.queue_next("tv", m.sessions["tv"], 0, delay=0.2)
    assert m.pending["tv"].item.id == "2"
    m.get = lambda uuid: fake_cast(cast.APP_MEDIA_RECEIVER)
    up = m.status("tv")["next_up"]
    assert (up["episode"], 0 < up["in"] <= 0.2) == (2, True)
    m.control("tv", "cancel_next")
    assert not m.started.wait(0.4)
    assert m.status("tv")["next_up"] is None


def test_next_starts_right_away_during_and_without_a_countdown(series):
    m, _ = series
    tv = playing_cast("http://pc/stream/1.mp4?start=0")
    m.get = lambda uuid: tv
    m.queue_next("tv", m.sessions["tv"], 0, delay=30)
    m.control("tv", "next")
    m.control("tv", "next")  # mid-episode: the session's next one, tracks matched
    assert [(c[1].id, c[2], c[3]) for c in m.calls] == [("2", 3, "e5"), ("2", 3, "e5")]
    assert m.pending == {}


def receiver():
    loads = []
    mc = SimpleNamespace(
        status=SimpleNamespace(content_id=None, player_state="IDLE", adjusted_current_time=0,
                               duration=0, idle_reason=None),
        play_media=lambda url, *a, **k: loads.append(url),
        block_until_active=lambda timeout: None,
        stop=lambda: loads.append("stop"),
        seek=lambda t: loads.append(f"seek {t}"),
    )
    return SimpleNamespace(loads=loads, media_controller=mc, app_id=None, app_display_name="",
                           cast_info=SimpleNamespace(host="127.0.0.1"),
                           quit_app=lambda: loads.append("quit"),
                           status=SimpleNamespace(volume_level=0.5, volume_muted=False))


def test_user_action_while_probing_keeps_the_episode_out_of_the_queue(series):
    m, _ = series
    gen_at_finish = 0
    m._bump("tv")  # the user started a film while the next episode was being probed
    m.queue_next("tv", m.sessions["tv"], gen_at_finish, delay=0.05)
    assert m.pending == {}
    assert not m.started.wait(0.2)


def test_autoplay_launch_gives_up_after_a_user_action(series):
    m, _ = series
    tv = receiver()
    m.get = lambda uuid: tv
    m.play = m.real_play
    before = m.sessions["tv"]
    m._bump("tv")  # "Стоп" or "Отмена" landed while the timer's play() was probing
    ep2 = media.get("2")
    assert m.play("tv", ep2, 3, "e5", 0.0, False, gen=0) is None
    assert tv.loads == [] and m.sessions["tv"] is before


def test_playing_by_hand_cancels_the_countdown(series):
    m, _ = series
    tv = receiver()
    m.get = lambda uuid: tv
    m._casts["tv"] = tv
    m.queue_next("tv", m.sessions["tv"], 0, delay=30)
    m.play = m.real_play
    session = m.play("tv", media.get("1"))
    assert m.pending == {}
    assert session is m.sessions["tv"] and len(tv.loads) == 1


def test_stop_during_the_countdown_cancels_it(series):
    m, _ = series
    tv = receiver()
    m.get = lambda uuid: tv
    m.queue_next("tv", m.sessions["tv"], 0, delay=0.2)
    m.control("tv", "stop")
    assert m.pending == {}
    assert not m.started.wait(0.4)


def test_countdown_does_not_take_the_tv_back_from_another_app(series):
    m, _ = series
    m._casts["tv"] = SimpleNamespace(app_id="233637DE", app_display_name="YouTube")
    m.queue_next("tv", m.sessions["tv"], 0, delay=0.05)
    assert not m.started.wait(0.3)


def test_direct_seek_to_the_credits_then_finished_counts(series):
    m, listener = series
    tv = receiver()
    m.get = lambda uuid: tv
    s = m.sessions["tv"]
    s.mode, s.last_pos = "direct", 600.0
    m.control("tv", "seek", 3560)
    listener.new_media_status(status("IDLE", "FINISHED"))
    assert progress.store.get("1")["watched"] is True


def test_next_reports_an_unreadable_next_episode(series, monkeypatch):
    m, _ = series
    m.get = lambda uuid: receiver()

    def broken(_item):
        raise ValueError("bad ffprobe json")

    monkeypatch.setattr(media, "probe", broken)
    with pytest.raises(RuntimeError, match="следующую серию"):
        m.control("tv", "next")


def test_failed_autoplay_is_shown_until_the_user_acts(series):
    m, _ = series

    def broken_play(*args, **kw):
        m.started.set()
        raise OSError("TV went away")

    m.play = broken_play
    m.queue_next("tv", m.sessions["tv"], 0, delay=0.05)
    assert m.started.wait(5)
    for _ in range(50):
        if "tv" in m.errors:
            break
        time.sleep(0.02)
    m.get = lambda uuid: fake_cast(cast.APP_MEDIA_RECEIVER)
    assert m.status("tv")["error"] == "не удалось запустить Dark · S01E02"
    m.cancel_next("tv")
    assert m.status("tv")["error"] is None


def test_unreadable_next_episode_is_shown(series, monkeypatch):
    m, _ = series

    def broken(_item):
        raise ValueError("bad ffprobe json")

    monkeypatch.setattr(media, "probe", broken)
    m.queue_next("tv", m.sessions["tv"], 0)
    assert m.errors["tv"] == "не удалось прочитать следующую серию"


def test_next_right_after_the_end_uses_the_finished_episode(series):
    m, listener = series
    listener.new_media_status(status("IDLE", "FINISHED"))  # autoplay off: nothing queued
    assert "tv" not in m.sessions
    m.get = lambda uuid: fake_cast(cast.APP_MEDIA_RECEIVER)
    assert m.status("tv")["after"] == "1"
    m.control("tv", "next")
    assert [(c[1].id, c[2], c[3]) for c in m.calls] == [("2", 3, "e5")]


def test_next_while_autoplay_is_still_probing_starts_it_once(series):
    m, listener = series
    gen_at_finish = 0
    listener.new_media_status(status("IDLE", "FINISHED"))
    m.get = lambda uuid: fake_cast(cast.APP_MEDIA_RECEIVER)
    m.control("tv", "next")
    # The autoplay that was probing in the background finishes only now.
    m.queue_next("tv", m.finished["tv"], gen_at_finish, delay=0.05)
    time.sleep(0.2)
    assert len(m.calls) == 1 and m.pending == {}


def test_stop_forgets_the_finished_episode(series):
    m, listener = series
    listener.new_media_status(status("IDLE", "FINISHED"))
    m.get = lambda uuid: receiver()
    m.control("tv", "stop")
    assert m.status("tv")["after"] is None


def test_after_is_forgotten_once_something_else_plays(series):
    m, listener = series
    listener.new_media_status(status("IDLE", "FINISHED"))
    tv = receiver()
    m.get = lambda uuid: tv
    m._casts["tv"] = tv
    m.play = m.real_play
    film = m.play("tv", media.get("1"))
    # The film is stopped with the TV remote: its session just ends, no FINISHED.
    listener.new_media_status(status("IDLE", "CANCELLED", content_id=film.url))
    assert m.status("tv")["after"] is None


def test_autoplay_that_yields_to_another_app_says_so(series):
    m, _ = series
    m._casts["tv"] = SimpleNamespace(app_id="233637DE", app_display_name="YouTube")
    m.queue_next("tv", m.sessions["tv"], 0, delay=0.01)
    for _ in range(50):
        if "tv" in m.errors:
            break
        time.sleep(0.02)
    assert m.errors["tv"] == "Dark · S01E02 не запущена: на телевизоре открыто YouTube"
    assert m.calls == []
