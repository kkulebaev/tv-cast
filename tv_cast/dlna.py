"""TVs that take a stream only over DLNA (UPnP AV), such as SaluteTV.

SaluteTV also announces itself as a Google Cast device, but refuses Cast connections, so
Twitch reaches it this way. Films are not offered here: DLNA has no subtitles or track choice.
"""
import html
import logging
import re
import socket
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin

from . import live
from .cast import local_ip_for
from .config import settings

log = logging.getLogger("tvcast.dlna")
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
PREFIX = "dlna:"
# The renderer's own player volume, separate from the TV's: it starts low on SaluteTV.
VOLUME = 100
# A short STOPPED happens while the renderer reconnects; longer means stop on the TV remote.
STOPPED_GRACE = 15.0
RESEND_EVERY = 25.0
STATES = {"PLAYING": "PLAYING", "PAUSED_PLAYBACK": "PAUSED", "TRANSITIONING": "BUFFERING",
          "STOPPED": "IDLE", "NO_MEDIA_PRESENT": "IDLE"}


@dataclass
class Renderer:
    id: str
    name: str
    model: str
    host: str
    avt: str
    rc: str | None


@dataclass
class Live:
    channel: str
    quality: str
    stream: live.Stream
    url: str
    meta: str
    played: bool = False
    stopped_since: float | None = None
    sent_at: float = 0.0
    error: str | None = None
    started_at: float = field(default_factory=time.time)


def is_dlna(device: str) -> bool:
    return device.startswith(PREFIX)


def ssdp_search(ip: str, wait: float = 2.5) -> set[str]:
    # Multicast goes out of the LAN interface explicitly: by default it leaves through the
    # VPN tunnel and no TV answers.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        s.bind((ip, 0))
        s.settimeout(0.5)
        msg = (b"M-SEARCH * HTTP/1.1\r\nHOST:239.255.255.250:1900\r\nMAN:\"ssdp:discover\"\r\n"
               b"MX:2\r\nST:urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n")
        found: set[str] = set()
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                data, _ = s.recvfrom(4096)
            except TimeoutError:
                continue
            m = re.search(r"(?im)^location:\s*(\S+)", data.decode(errors="ignore"))
            if m:
                found.add(m.group(1))
        return found


def parse_description(location: str, desc: str) -> Renderer | None:
    def tag(name: str) -> str:
        m = re.search(rf"<{name}>([^<]*)</{name}>", desc)
        return html.unescape(m.group(1).strip()) if m else ""

    def control(service: str) -> str | None:
        m = re.search(rf"<serviceType>{re.escape(service)}</serviceType>.*?"
                      r"<controlURL>([^<]+)</controlURL>", desc, re.S)
        return urljoin(location, m.group(1).strip()) if m else None

    avt, udn = control(AVT), tag("UDN")
    if not avt or not udn:
        return None
    host = re.sub(r"^\w+://([^:/]+).*", r"\1", location)
    name = tag("friendlyName") or host
    maker = tag("manufacturer")
    return Renderer(id=PREFIX + udn.removeprefix("uuid:"), name=name,
                    model=f"{maker} DLNA" if maker else "DLNA", host=host, avt=avt, rc=control(RC))


def soap(url: str, action: str, body: str = "", service: str = AVT, timeout: float = 5) -> str:
    env = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope '
           'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:{action} xmlns:u="{service}"><InstanceID>0</InstanceID>{body}</u:{action}>'
           "</s:Body></s:Envelope>")
    req = urllib.request.Request(url, env.encode(), {
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="ignore")


def _field(xml: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<]*)</{name}>", xml)
    return m.group(1) if m else None


def didl(title: str, url: str) -> str:
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<item id="0" parentID="-1" restricted="1"><dc:title>{html.escape(title)}</dc:title>'
            "<upnp:class>object.item.videoItem</upnp:class>"
            '<res protocolInfo="http-get:*:application/vnd.apple.mpegurl:*">'
            f"{html.escape(url)}</res></item></DIDL-Lite>")


class DlnaManager:
    def __init__(self, lan_ip: Callable[[], str | None]):
        self._lan_ip = lan_ip
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self.renderers: dict[str, Renderer] = {}
        self.sessions: dict[str, Live] = {}
        self.errors: dict[str, str] = {}
        self._gen: dict[str, int] = {}
        self._play_locks: dict[str, threading.Lock] = {}

    def start(self):
        threading.Thread(target=self._discover_loop, name="tvcast-dlna", daemon=True).start()
        threading.Thread(target=self._watch_loop, name="tvcast-dlna-watch", daemon=True).start()

    def stop(self):
        self._stopping.set()
        for device in list(self.sessions):
            self._end(device, send_stop=True)

    def _discover_loop(self):
        # Frequent at first, since a TV that was just switched on answers late.
        delay = 5.0
        while not self._stopping.is_set():
            try:
                self.discover()
            except Exception:
                log.exception("DLNA discovery failed")
            if self._stopping.wait(delay):
                return
            delay = min(delay * 2, 60.0)

    def discover(self):
        ip = self._lan_ip()
        if not ip:
            return
        for location in ssdp_search(ip):
            try:
                with urllib.request.urlopen(location, timeout=5) as r:
                    desc = r.read().decode(errors="ignore")
            except OSError:
                continue
            renderer = parse_description(location, desc)
            if renderer is None:
                continue
            with self._lock:
                known = renderer.id in self.renderers
                self.renderers[renderer.id] = renderer
            if not known:
                log.info("found %s (%s) at %s", renderer.name, renderer.model, renderer.host)

    def devices(self) -> list[dict]:
        with self._lock:
            return [{"id": r.id, "name": r.name, "model": r.model, "host": r.host}
                    for r in self.renderers.values()]

    def get(self, device: str) -> Renderer:
        with self._lock:
            renderer = self.renderers.get(device)
        if renderer is None:
            raise KeyError(device)
        return renderer

    def live_file(self, key: str, name: str):
        with self._lock:
            stream = next((s.stream for s in self.sessions.values() if s.stream.key == key), None)
        return stream.file(name) if stream else None

    def _bump(self, device: str) -> int:
        with self._lock:
            gen = self._gen[device] = self._gen.get(device, 0) + 1
            self.errors.pop(device, None)
        return gen

    def play_live(self, device: str, channel: str, quality: str = "best") -> Live | None:
        renderer = self.get(device)
        gen = self._bump(device)
        with self._lock:
            lock = self._play_locks.setdefault(device, threading.Lock())
        with lock:
            stream = live.Stream(channel, quality)
            try:
                stream.start()
                ready = stream.wait_ready(lambda: self._gen.get(device, 0) != gen)
            except Exception:
                stream.stop()
                raise
            url = (f"http://{local_ip_for(renderer.host)}:{settings.port}"
                   f"/live/{stream.key}/{live.PLAYLIST}")
            session = Live(channel, quality, stream, url, didl(channel, url))
            with self._lock:
                old = None
                if ready and self._gen.get(device, 0) == gen:
                    old = self.sessions.get(device)
                    self.sessions[device] = session
                else:
                    ready = False
            if not ready:
                stream.stop()
                return None
            if old:
                old.stream.stop()
            log.info("live twitch.tv/%s (%s) on %s: %s", channel, quality, renderer.name, url)
            self._send(renderer, session)
            return session

    def _send(self, renderer: Renderer, s: Live):
        s.sent_at = time.monotonic()
        try:
            soap(renderer.avt, "Stop")
        except OSError:
            pass  # nothing was playing
        try:
            soap(renderer.avt, "SetAVTransportURI",
                 f"<CurrentURI>{html.escape(s.url)}</CurrentURI>"
                 f"<CurrentURIMetaData>{html.escape(s.meta)}</CurrentURIMetaData>")
            time.sleep(1.5)
            soap(renderer.avt, "Play", "<Speed>1</Speed>")
            if renderer.rc:
                soap(renderer.rc, "SetMute",
                     "<Channel>Master</Channel><DesiredMute>0</DesiredMute>", RC)
                soap(renderer.rc, "SetVolume",
                     f"<Channel>Master</Channel><DesiredVolume>{VOLUME}</DesiredVolume>", RC)
        except OSError as e:
            log.warning("%s did not take the stream: %s", renderer.name, e)

    def _end(self, device: str, send_stop: bool, error: str | None = None):
        with self._lock:
            s = self.sessions.pop(device, None)
            renderer = self.renderers.get(device)
            if s and error:
                self.errors[device] = error
        if s is None:
            return
        if send_stop and renderer:
            try:
                soap(renderer.avt, "Stop")
            except OSError:
                pass
        s.stream.stop()

    def _watch_loop(self):
        while not self._stopping.wait(5):
            for device in list(self.sessions):
                try:
                    self.check(device)
                except Exception:
                    log.exception("DLNA watch failed for %s", device)

    def check(self, device: str):
        s, renderer = self.sessions.get(device), self.renderers.get(device)
        if s is None or renderer is None:
            return
        if not s.stream.alive:
            self._end(device, send_stop=True,
                      error=f"трансляция twitch.tv/{s.channel} закончилась")
            return
        state = self._transport_state(renderer)
        now = time.monotonic()
        if state == "PLAYING":
            s.played, s.stopped_since = True, None
        elif not s.played:
            # The renderer sometimes ignores the first command, e.g. right after it restarts.
            if state != "TRANSITIONING" and now - s.sent_at >= RESEND_EVERY:
                self._send(renderer, s)
        elif state in ("STOPPED", "NO_MEDIA_PRESENT"):
            s.stopped_since = s.stopped_since or now
            if now - s.stopped_since >= STOPPED_GRACE:
                log.info("stopped from the TV: %s", renderer.name)
                self._end(device, send_stop=False)
        else:
            s.stopped_since = None

    def _transport_state(self, renderer: Renderer) -> str | None:
        try:
            return _field(soap(renderer.avt, "GetTransportInfo", timeout=3),
                          "CurrentTransportState")
        except OSError:
            return None

    def control(self, device: str, action: str, value: float | None = None):
        renderer = self.get(device)
        match action:
            case "stop":
                self._bump(device)
                self._end(device, send_stop=True)
            case "play":
                soap(renderer.avt, "Play", "<Speed>1</Speed>")
            case "pause":
                soap(renderer.avt, "Pause")
            case "volume" if renderer.rc:
                level = round(max(0.0, min(1.0, float(value or 0))) * 100)
                soap(renderer.rc, "SetVolume",
                     f"<Channel>Master</Channel><DesiredVolume>{level}</DesiredVolume>", RC)
            case "mute" if renderer.rc:
                muted = self._muted(renderer)
                soap(renderer.rc, "SetMute",
                     f"<Channel>Master</Channel><DesiredMute>{0 if muted else 1}</DesiredMute>",
                     RC)
            case "seek" | "next" | "cancel_next" | "volume" | "mute":
                pass  # a live stream on a renderer without these: nothing to do
            case _:
                raise ValueError(action)

    def _muted(self, renderer: Renderer) -> bool | None:
        try:
            v = _field(soap(renderer.rc, "GetMute", "<Channel>Master</Channel>", RC, 3),
                       "CurrentMute")
        except OSError:
            return None
        return None if v is None else v in ("1", "true")

    def _volume(self, renderer: Renderer) -> float | None:
        try:
            v = _field(soap(renderer.rc, "GetVolume", "<Channel>Master</Channel>", RC, 3),
                       "CurrentVolume")
        except OSError:
            return None
        return int(v) / 100 if v and v.isdigit() else None

    def status(self, device: str) -> dict:
        renderer = self.get(device)
        s = self.sessions.get(device)
        state = self._transport_state(renderer)
        return {
            "error": self.errors.get(device),
            "after": None,
            "next_up": None,
            "app": None,
            "state": STATES.get(state or "", "UNKNOWN"),
            "idle_reason": None,
            "position": 0.0,
            "duration": 0,
            "volume": self._volume(renderer) if renderer.rc else None,
            "muted": self._muted(renderer) if renderer.rc else None,
            "session": None if s is None else {
                "item_id": "", "title": s.channel, "mode": "live",
                "reason": f"twitch.tv/{s.channel} · {s.quality}",
                "audio_index": None, "sub_key": None, "transcode": False,
                "error": s.error, "channel": s.channel,
            },
        }
