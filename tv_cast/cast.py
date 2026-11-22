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

from . import media
from .config import settings

log = logging.getLogger("tvcast.cast")


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
    error: str | None = None
    started_at: float = field(default_factory=time.time)


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
        self.sessions: dict[str, Session] = {}

    def start(self):
        self._browser = pychromecast.get_chromecasts(
            blocking=False, callback=self._on_cast, known_hosts=settings.known_hosts
        )

    def stop(self):
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
    ) -> Session:
        cast = self.get(uuid)
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
        if sub_key:
            subtitles = f"{base}/subs/{item.id}/{quote(sub_key)}.vtt?start={offset:.3f}"

        session = Session(
            item_id=item.id, title=item.title, mode=mode, reason=reason,
            audio_index=audio_index, sub_key=sub_key, force_transcode=force_transcode,
            duration=probe.duration, url=url, offset=offset,
        )
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
            subtitles_lang="ru",
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
                    mc.seek(target)
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
        return {
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
