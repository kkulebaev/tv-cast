import logging
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("tvcast.live")
PLAYLIST = "live.m3u8"
# What the HLS muxer below writes; anything else asked for under /live/ is refused.
FILE_RE = re.compile(r"(live\.m3u8|seg\d+\.ts)")
# Twitch logins: 4–25 letters, digits and underscores (a few legacy ones are shorter).
CHANNEL_RE = re.compile(r"[A-Za-z0-9_]{1,25}")
READY_SEGMENTS = 3
READY_TIMEOUT = 40.0


def channel_name(value: str) -> str:
    """Accepts a bare login as well as a pasted twitch.tv link."""
    name = value.strip().rstrip("/").split("?")[0].split("/")[-1].lower()
    if not CHANNEL_RE.fullmatch(name):
        raise ValueError(f"не похоже на Twitch-канал: {value!r}")
    return name


def commands(channel: str, quality: str, playlist: Path) -> list[list[str]]:
    source = ["streamlink", "--loglevel", "warning", "-O", f"twitch.tv/{channel}", quality]
    # The receiver plays HLS of short segments from the PC rather than Twitch's own playlist:
    # Twitch may be reachable only through the PC's VPN, and the TV doesn't go through it.
    # The receiver starts three target durations behind the live edge, and Twitch hands out
    # its segments in bursts (6 s on some channels): with 2 s segments the TV caught up with
    # the edge between bursts and rebuffered every few seconds.
    hls = ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy", "-f", "hls",
           "-hls_time", "4", "-hls_list_size", "10",
           "-hls_flags", "delete_segments+omit_endlist",
           "-hls_segment_filename", str(playlist.parent / "seg%d.ts"), str(playlist)]
    return [source, hls]


class Stream:
    def __init__(self, channel: str, quality: str = "best"):
        self.channel, self.quality = channel, quality
        # Part of the URL the TV gets; a stale URL from a previous stream must not resolve.
        self.key = secrets.token_hex(8)
        self.dir = Path(tempfile.mkdtemp(prefix="tvcast-live-"))
        self.playlist = self.dir / PLAYLIST
        self._procs: list[subprocess.Popen] = []

    def start(self, cmds: list[list[str]] | None = None):
        source, hls = cmds or commands(self.channel, self.quality, self.playlist)
        for tool in (source[0], hls[0]):
            if not shutil.which(tool):
                raise RuntimeError(f"не найден {tool}")
        # Own process groups: a Ctrl+C in the server's terminal is handled by the server,
        # which then stops these in order instead of them dying mid-write.
        src = subprocess.Popen(source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
        mux = subprocess.Popen(hls, stdin=src.stdout, stderr=subprocess.DEVNULL,
                               start_new_session=True)
        src.stdout.close()
        self._procs = [src, mux]

    @property
    def alive(self) -> bool:
        return bool(self._procs) and all(p.poll() is None for p in self._procs)

    def segments(self) -> int:
        try:
            return self.playlist.read_text().count(".ts")
        except FileNotFoundError:
            return 0

    def wait_ready(self, cancelled: Callable[[], bool] = lambda: False,
                   timeout: float = READY_TIMEOUT, every: float = 0.5) -> bool:
        """False when cancelled; the receiver gives up on a playlist with too few segments."""
        deadline = time.monotonic() + timeout
        while self.segments() < READY_SEGMENTS:
            if cancelled():
                return False
            if not self.alive:
                raise RuntimeError(f"twitch.tv/{self.channel} не запустился: канал офлайн "
                                   f"или нет качества {self.quality}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"поток twitch.tv/{self.channel} не пошёл за {timeout:.0f} с")
            time.sleep(every)
        return True

    def stop(self):
        for p in self._procs:
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(self.dir, ignore_errors=True)
        log.info("stopped twitch.tv/%s", self.channel)

    def file(self, name: str) -> Path | None:
        if not FILE_RE.fullmatch(name):
            return None
        path = self.dir / name
        return path if path.is_file() else None
