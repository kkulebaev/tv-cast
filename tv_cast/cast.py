import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import pychromecast
from pychromecast.config import APP_MEDIA_RECEIVER
from pychromecast.controllers.media import (
    METADATA_TYPE_MOVIE,
    STREAM_TYPE_BUFFERED,
    MediaStatusListener,
)

from . import media, progress
from .config import settings

log = logging.getLogger("tvcast.cast")
# Long enough to reach the remote and cancel, short enough not to wait through an idle screen.
AUTOPLAY_DELAY = 10.0


@dataclass
class Session:
    item_id: str
    title: str
    mode: media.Mode
    reason: str
    audio_index: int | None
    sub_key: str | None
    force_transcode: bool
    duration: float
    url: str = ""
    # Streamed (remux/transcode) output always starts at t=0, so the receiver's clock
    # is relative to where ffmpeg was told to start.
    offset: float = 0.0
    # Film time last seen by the tracker; tells a real ending from a stream that broke off.
    last_pos: float = 0.0
    error: str | None = None
    started_at: float = field(default_factory=time.time)


@dataclass
class NextUp:
    item: media.Item
    audio_index: int | None
    sub_key: str | None
    force_transcode: bool
    at: float
    # The device generation the episode was queued in; see CastManager._bump.
    gen: int = 0
    timer: threading.Timer | None = None


class _Listener(MediaStatusListener):
    def __init__(self, manager: "CastManager", uuid: str):
        self.manager, self.uuid = manager, uuid

    def new_media_status(self, status):
        # A seek in a piped stream reloads the URL and reports the old one as IDLE/INTERRUPTED,
        # so only the end of the current URL closes the session. ERROR keeps it for its message.
        if status.player_state != "IDLE" or status.idle_reason not in ("FINISHED", "CANCELLED"):
            return
        session = self.manager.sessions.get(self.uuid)
        if session and status.content_id == session.url:
            if status.idle_reason == "FINISHED":
                self.manager.on_finished(self.uuid, session, status.current_time)
            self.manager.end_session(self.uuid, session)

    def load_media_failed(self, queue_item_id, error_code):
        session = self.manager.sessions.get(self.uuid)
        if session:
            session.error = f"телевизор не смог открыть поток (код {error_code})"
        log.warning("load failed on %s: %s", self.uuid, error_code)


def local_ip_for(host: str) -> str:
    # The default route goes through the VPN tunnel; asking the kernel for the route to
    # the TV itself yields the LAN address the TV can actually reach.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 8009))
        return s.getsockname()[0]


class CastManager:
    def __init__(self):
        self._casts: dict[str, pychromecast.Chromecast] = {}
        self._lock = threading.Lock()
        self._browser = None
        self._stopping = threading.Event()
        self.sessions: dict[str, Session] = {}
        self.pending: dict[str, NextUp] = {}
        self._gen: dict[str, int] = {}
        self._play_locks: dict[str, threading.Lock] = {}

    def start(self):
        self._browser = pychromecast.get_chromecasts(
            blocking=False, callback=self._on_cast, known_hosts=settings.known_hosts
        )
        # Positions are saved here rather than from the web page, so they survive a closed
        # browser or a phone that went to sleep.
        threading.Thread(target=self._track, name="tvcast-progress", daemon=True).start()

    def _track(self):
        while not self._stopping.wait(10):
            for uuid in list(self.sessions):
                self.save(uuid, flush=False)
            self.save(None)

    def save(self, uuid: str | None, flush: bool = True):
        # Saving is a side effect: a full disk or a flaky TV must not break playback control
        # or kill the tracker thread for the rest of the run.
        try:
            if uuid is not None:
                self.record(uuid)
            if flush:
                progress.store.flush()
        except Exception:
            log.exception("could not save watch progress")

    def finish(self, session: Session):
        try:
            if session.duration > 0:
                progress.store.update(session.item_id, session.duration, session.duration)
            else:
                progress.store.set_watched(session.item_id, True)
            progress.store.flush()
        except Exception:
            log.exception("could not mark %s watched", session.item_id)

    def _bump(self, uuid: str) -> int:
        """Marks a user action on the device (play, stop, cancel) and drops a queued episode.

        Autoplay works from the generation it was queued in, so a countdown or a launch already
        under way gives up instead of overriding what the user just did."""
        with self._lock:
            gen = self._gen[uuid] = self._gen.get(uuid, 0) + 1
            up = self.pending.pop(uuid, None)
        if up and up.timer:
            up.timer.cancel()
        return gen

    def _play_lock(self, uuid: str) -> threading.Lock:
        with self._lock:
            return self._play_locks.setdefault(uuid, threading.Lock())

    def on_finished(self, uuid: str, session: Session, current_time: float | None = None):
        pos = max(session.last_pos, session.offset + (current_time or 0))
        # A stream whose ffmpeg died mid-film also ends as FINISHED on the receiver.
        if session.duration > 0 and not progress.is_finished(pos, session.duration):
            log.warning("%s ended at %.0f of %.0f s; not marking it watched",
                        session.title, pos, session.duration)
            return
        self.finish(session)
        if settings.autoplay:
            with self._lock:
                gen = self._gen.get(uuid, 0)
            # Runs on pychromecast's socket thread, and queueing probes the next file.
            threading.Thread(target=self.queue_next, args=(uuid, session, gen),
                             daemon=True).start()

    def next_for(self, session: Session, gen: int = 0) -> NextUp | None:
        item = media.get(session.item_id)
        nxt = media.next_episode(item) if item else None
        if nxt is None:
            return None
        prev, probe = media.probe(item), media.probe(nxt)
        return NextUp(
            item=nxt,
            audio_index=media.match_audio(prev, session.audio_index, probe),
            sub_key=media.match_sub(prev, session.sub_key, probe),
            force_transcode=session.force_transcode,
            at=time.time(),
            gen=gen,
        )

    def queue_next(self, uuid: str, session: Session, gen: int, delay: float = AUTOPLAY_DELAY):
        try:
            up = self.next_for(session, gen)
        except Exception:
            log.exception("could not prepare the episode after %s", session.title)
            return
        if up is None:
            return
        up.at = time.time() + delay
        up.timer = threading.Timer(delay, self._start_next, (uuid, up))
        up.timer.daemon = True
        with self._lock:
            # Probing took a while; if the user played or stopped something meanwhile, the
            # episode that ended is no longer what the TV is about.
            if self._gen.get(uuid, 0) != gen:
                return
            old = self.pending.pop(uuid, None)
            self.pending[uuid] = up
        if old and old.timer:
            old.timer.cancel()
        up.timer.start()

    def _start_next(self, uuid: str, up: NextUp):
        with self._lock:
            if self.pending.get(uuid) is not up:
                return
            del self.pending[uuid]
            cast = self._casts.get(uuid)
        if cast is not None and cast.app_id not in (None, APP_MEDIA_RECEIVER):
            log.info("not starting %s: %s is open on the TV", up.item.rel, cast.app_display_name)
            return
        try:
            self.play(uuid, up.item, up.audio_index, up.sub_key, 0.0, up.force_transcode,
                      gen=up.gen)
        except Exception:
            log.exception("could not start %s", up.item.rel)

    def cancel_next(self, uuid: str):
        self._bump(uuid)

    def record(self, uuid: str):
        session = self.sessions.get(uuid)
        with self._lock:
            cast = self._casts.get(uuid)
        if session is None or cast is None:
            return
        ms = cast.media_controller.status
        # Between a seek and the new load the receiver still reports the old URL's clock.
        if ms.content_id != session.url or ms.player_state not in ("PLAYING", "PAUSED"):
            return
        session.last_pos = session.offset + (ms.adjusted_current_time or 0)
        progress.store.update(session.item_id, session.last_pos, session.duration)

    def stop(self):
        self._stopping.set()
        for uuid in list(self.pending):
            self.cancel_next(uuid)
        for uuid in list(self.sessions):
            self.save(uuid, flush=False)
        self.save(None)
        if self._browser:
            self._browser.stop_discovery()
        for cast in self._casts.values():
            cast.disconnect(timeout=2)

    def _on_cast(self, cast: pychromecast.Chromecast):
        uuid = str(cast.uuid)
        with self._lock:
            if uuid in self._casts:
                return
            self._casts[uuid] = cast
        cast.media_controller.register_status_listener(_Listener(self, uuid))
        cast.start()
        log.info("found %s (%s) at %s", cast.cast_info.friendly_name,
                 cast.cast_info.model_name, cast.cast_info.host)

    def devices(self) -> list[dict]:
        with self._lock:
            casts = list(self._casts.items())
        return [
            {
                "id": uuid,
                "name": c.cast_info.friendly_name,
                "model": c.cast_info.model_name,
                "host": c.cast_info.host,
            }
            for uuid, c in casts
        ]

    def end_session(self, uuid: str, session: Session):
        # Listener callbacks run on pychromecast's thread; a play() that just stored a new
        # session must not lose it to a stale check.
        with self._lock:
            if self.sessions.get(uuid) is session:
                del self.sessions[uuid]

    def get(self, uuid: str) -> pychromecast.Chromecast:
        with self._lock:
            cast = self._casts.get(uuid)
        if cast is None:
            raise KeyError(uuid)
        cast.wait(timeout=10)
        return cast

    def play(
        self,
        uuid: str,
        item: media.Item,
        audio_index: int | None = None,
        sub_key: str | None = None,
        start: float = 0.0,
        force_transcode: bool = False,
        gen: int | None = None,
    ) -> Session | None:
        """Without gen this is the user's choice and supersedes any queued autoplay. Autoplay
        passes the generation it was queued in and gives up (None) if the user acted since."""
        if gen is None:
            gen = self._bump(uuid)
        # One load at a time per TV, so an autoplay launch and a click can't interleave.
        with self._play_lock(uuid):
            return self._play(uuid, item, audio_index, sub_key, start, force_transcode, gen)

    def _play(self, uuid: str, item: media.Item, audio_index: int | None, sub_key: str | None,
              start: float, force_transcode: bool, gen: int) -> Session | None:
        cast = self.get(uuid)
        # The new load replaces the session; the episode being left keeps its last seconds.
        if uuid in self.sessions:
            self.save(uuid)
        probe = media.probe(item)
        mode, reason = media.plan(probe, audio_index, force_transcode)
        base = f"http://{local_ip_for(cast.cast_info.host)}:{settings.port}"

        if mode == "direct":
            url = f"{base}/media/{item.id}{item.path.suffix.lower()}"
            content_type = "video/webm" if item.path.suffix.lower() == ".webm" else "video/mp4"
            offset, current_time = 0.0, start
        else:
            if mode == "remux":
                start = media.keyframe_before(item, start)
            q = f"mode={mode}&start={start:.3f}"
            if audio_index is not None:
                q += f"&audio={audio_index}"
            url = f"{base}/stream/{item.id}.mp4?{q}"
            content_type = "video/mp4"
            offset, current_time = start, 0.0

        subtitles = None
        sub_lang = "und"
        if sub_key:
            subtitles = f"{base}/subs/{item.id}/{quote(sub_key)}.vtt?start={offset:.3f}"
            track = next((s for s in probe.subtitles if s.key == sub_key), None)
            sub_lang = media.bcp47(track.lang if track else "")

        session = Session(
            item_id=item.id, title=item.title, mode=mode, reason=reason,
            audio_index=audio_index, sub_key=sub_key, force_transcode=force_transcode,
            duration=probe.duration, url=url, offset=offset, last_pos=offset + current_time,
        )
        with self._lock:
            if self._gen.get(uuid, 0) != gen:
                log.info("not starting %s: superseded on %s", item.rel, uuid)
                return None
            self.sessions[uuid] = session
        log.info("play %s on %s: %s (%s) %s", item.rel, uuid, mode, reason, url)

        mc = cast.media_controller
        mc.play_media(
            url, content_type,
            title=item.title,
            current_time=current_time or None,
            stream_type=STREAM_TYPE_BUFFERED,
            metadata={"metadataType": METADATA_TYPE_MOVIE, "title": item.title},
            subtitles=subtitles,
            subtitles_lang=sub_lang,
        )
        mc.block_until_active(timeout=20)
        return session

    def control(self, uuid: str, action: str, value: float | None = None):
        cast = self.get(uuid)
        mc = cast.media_controller
        session = self.sessions.get(uuid)
        match action:
            case "play":
                mc.play()
            case "pause":
                mc.pause()
            case "stop":
                self._bump(uuid)
                # Waits out a launch already past its last check, so it can't restart the TV.
                with self._play_lock(uuid):
                    self.save(uuid)
                    mc.stop()
                    # Otherwise the receiver lingers on its idle cast screen for minutes.
                    cast.quit_app()
                    self.sessions.pop(uuid, None)
            case "seek":
                target = max(0.0, float(value or 0))
                if session and session.mode != "direct":
                    # A piped stream has no byte ranges to seek in: restart ffmpeg there.
                    item = media.get(session.item_id)
                    if item:
                        self.play(uuid, item, session.audio_index, session.sub_key,
                                  target, session.force_transcode)
                else:
                    if session:
                        # Lets a FINISHED right after a jump to the credits count as watched.
                        session.last_pos = target
                    mc.seek(target)
            case "next":
                with self._lock:
                    up = self.pending.get(uuid)
                    gen = self._gen.get(uuid, 0)
                if up is None and session is not None:
                    try:
                        up = self.next_for(session, gen)
                    except Exception as e:
                        raise RuntimeError("не удалось прочитать следующую серию") from e
                    if up is not None:
                        with self._lock:
                            self.pending[uuid] = up
                if up is not None:
                    if up.timer:
                        up.timer.cancel()
                    self._start_next(uuid, up)
            case "cancel_next":
                self.cancel_next(uuid)
            case "volume":
                cast.set_volume(max(0.0, min(1.0, float(value or 0))))
            case "mute":
                cast.set_volume_muted(not cast.status.volume_muted)
            case _:
                raise ValueError(action)

    def status(self, uuid: str) -> dict:
        cast = self.get(uuid)
        ms = cast.media_controller.status
        session = self.sessions.get(uuid)
        # Another app opened on the TV; the grace period covers our own receiver still launching.
        if (session and cast.app_id not in (None, APP_MEDIA_RECEIVER)
                and time.time() - session.started_at > 30):
            self.end_session(uuid, session)
            session = None
        offset = session.offset if session else 0.0
        duration = session.duration if session else (ms.duration or 0)
        up = self.pending.get(uuid)
        return {
            "next_up": None if up is None else {
                "item_id": up.item.id,
                "series": up.item.series,
                "season": up.item.season,
                "episode": up.item.episode,
                "in": max(0.0, up.at - time.time()),
            },
            "app": cast.app_display_name,
            "state": ms.player_state,
            "idle_reason": ms.idle_reason,
            "position": offset + (ms.adjusted_current_time or 0),
            "duration": duration,
            "volume": cast.status.volume_level if cast.status else None,
            "muted": cast.status.volume_muted if cast.status else None,
            "session": None if session is None else {
                "item_id": session.item_id,
                "title": session.title,
                "mode": session.mode,
                "reason": session.reason,
                "audio_index": session.audio_index,
                "sub_key": session.sub_key,
                "error": session.error,
            },
        }


manager = CastManager()
