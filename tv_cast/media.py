import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from .config import settings

VIDEO_EXT = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m2ts",
    ".wmv", ".flv", ".mpg", ".mpeg",
}
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa"}

# What the Default Media Receiver plays reliably on Google TV. MKV is deliberately not
# here: the receiver often loads it but silently drops the audio track.
MP4_FORMAT = "mov,mp4,m4a,3gp,3g2,mj2"
WEBM_FORMAT = "matroska,webm"
CAST_VIDEO = {"h264", "hevc", "vp8", "vp9"}
CAST_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
# Bitmap subtitles (PGS, VobSub) can't become WebVTT without OCR.
TEXT_SUBS = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}

Mode = Literal["direct", "remux", "transcode"]


@dataclass
class Track:
    index: int
    codec: str
    lang: str = ""
    title: str = ""
    channels: int = 0
    default: bool = False
    supported: bool = True


@dataclass
class SubTrack:
    key: str  # "e<stream index>" for embedded, "x<n>" for a sidecar file
    label: str
    lang: str = ""
    supported: bool = True


@dataclass
class Probe:
    duration: float
    format_name: str
    video_codec: str
    video_profile: str
    pix_fmt: str
    width: int
    height: int
    audio: list[Track] = field(default_factory=list)
    subtitles: list[SubTrack] = field(default_factory=list)


@dataclass
class Item:
    id: str
    path: Path
    rel: str
    size: int
    mtime: float

    @property
    def title(self) -> str:
        return self.path.stem


_library: dict[str, Item] = {}
_probe_cache: dict[tuple[str, float], Probe] = {}


def scan() -> list[Item]:
    root = settings.media_dir
    found: dict[str, Item] = {}
    if root.is_dir():
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                p = Path(dirpath) / name
                if p.suffix.lower() not in VIDEO_EXT:
                    continue
                rel = str(p.relative_to(root))
                st = p.stat()
                item_id = hashlib.sha1(rel.encode()).hexdigest()[:16]
                found[item_id] = Item(item_id, p, rel, st.st_size, st.st_mtime)
    _library.clear()
    _library.update(found)
    return sorted(found.values(), key=lambda i: i.rel.lower())


def get(item_id: str) -> Item | None:
    if item_id not in _library:
        scan()
    return _library.get(item_id)


def _sidecar_subs(path: Path) -> list[Path]:
    # Accept "Movie.srt" as well as "Movie.ru.srt" / "Movie.forced.en.srt".
    return sorted(
        p for p in path.parent.iterdir()
        if p.suffix.lower() in SUB_EXT
        and (p.stem == path.stem or p.name.startswith(path.stem + "."))
    )


def sidecar(item: Item, n: int) -> Path | None:
    subs = _sidecar_subs(item.path)
    return subs[n] if 0 <= n < len(subs) else None


def probe(item: Item) -> Probe:
    key = (str(item.path), item.mtime)
    if key in _probe_cache:
        return _probe_cache[key]
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(item.path)],
        capture_output=True, check=True, text=True,
    ).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next(
        (s for s in streams
         if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")),
        {},
    )
    audio, subs = [], []
    for s in streams:
        tags = s.get("tags", {})
        lang, title = tags.get("language", ""), tags.get("title", "")
        if s["codec_type"] == "audio":
            audio.append(Track(
                index=s["index"], codec=s.get("codec_name", "?"), lang=lang, title=title,
                channels=s.get("channels", 0),
                default=bool(s.get("disposition", {}).get("default")),
                supported=True,  # anything can be re-encoded to AAC
            ))
        elif s["codec_type"] == "subtitle":
            codec = s.get("codec_name", "?")
            subs.append(SubTrack(
                key=f"e{s['index']}",
                label=" · ".join(x for x in (lang, title, codec) if x),
                lang=lang, supported=codec in TEXT_SUBS,
            ))
    for n, p in enumerate(_sidecar_subs(item.path)):
        subs.append(SubTrack(key=f"x{n}", label=f"файл: {p.name}"))

    result = Probe(
        duration=float(data.get("format", {}).get("duration") or 0),
        format_name=data.get("format", {}).get("format_name", ""),
        video_codec=video.get("codec_name", ""),
        video_profile=video.get("profile", ""),
        pix_fmt=video.get("pix_fmt", ""),
        width=video.get("width", 0),
        height=video.get("height", 0),
        audio=audio,
        subtitles=subs,
    )
    _probe_cache[key] = result
    return result


def video_ok(p: Probe) -> bool:
    if p.video_codec not in CAST_VIDEO:
        return False
    # The TV's H.264 decoder has no High 10 profile; HEVC Main 10 is fine.
    return not (p.video_codec == "h264" and ("10" in p.video_profile or "10" in p.pix_fmt))


def plan(p: Probe, audio_index: int | None, force_transcode: bool = False) -> tuple[Mode, str]:
    if force_transcode or not video_ok(p):
        why = "принудительно" if force_transcode else f"видео {p.video_codec} {p.video_profile}".strip()
        return "transcode", f"{why} → H.264 через VAAPI"
    default_audio = next((a for a in p.audio if a.default), p.audio[0] if p.audio else None)
    chosen = next((a for a in p.audio if a.index == audio_index), default_audio)
    # ffprobe reports .mkv and .webm identically; only VP8/VP9 means a real WebM.
    direct_container = p.format_name == MP4_FORMAT or (
        p.format_name == WEBM_FORMAT and p.video_codec in {"vp8", "vp9"}
    )
    # The receiver always plays the file's default audio track, so another one needs a remux.
    if direct_container and (chosen is None or (chosen.codec in CAST_AUDIO and chosen is default_audio)):
        return "direct", "файл подходит как есть"
    reasons = []
    if not direct_container:
        reasons.append(f"контейнер {p.format_name.split(',')[0]}")
    if chosen and chosen.codec not in CAST_AUDIO:
        reasons.append(f"звук {chosen.codec}")
    elif chosen and chosen is not default_audio:
        reasons.append("не основная аудиодорожка")
    return "remux", ", ".join(reasons) + " → видео без перекодирования, звук в AAC"


def ffmpeg_stream_cmd(
    item: Item, p: Probe, mode: Mode, audio_index: int | None, start: float
) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if mode == "transcode" and Path(settings.vaapi_device).exists():
        cmd += ["-init_hw_device", f"vaapi=va:{settings.vaapi_device}", "-filter_hw_device", "va"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(item.path), "-map", "0:V:0"]

    chosen = next((a for a in p.audio if a.index == audio_index), None)
    if chosen is None and p.audio:
        chosen = next((a for a in p.audio if a.default), p.audio[0])
    if chosen:
        cmd += ["-map", f"0:{chosen.index}"]

    if mode == "transcode":
        if Path(settings.vaapi_device).exists():
            cmd += ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "21"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]
        if p.video_codec == "hevc":
            # Without the hvc1 tag Chromium-based receivers refuse HEVC in MP4.
            cmd += ["-tag:v", "hvc1"]

    if chosen:
        if chosen.codec in {"aac", "mp3"} and chosen.channels <= 2:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-ac", "2", "-b:a", "192k"]

    cmd += [
        "-sn", "-dn",
        # Fragmented MP4 can be written to a pipe; a regular MP4 needs a seekable output.
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1",
    ]
    return cmd


def keyframe_before(item: Item, t: float) -> float:
    """Stream copy can only start on a keyframe, and ffmpeg silently backs up to the
    previous one. Starting exactly there keeps the TV clock and subtitles in sync."""
    if t <= 0:
        return 0.0
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "V:0", "-skip_frame", "nokey",
         "-read_intervals", f"{max(0.0, t - 20):.3f}%{t + 0.05:.3f}",
         "-show_entries", "frame=pts_time,best_effort_timestamp_time", "-of", "csv=p=0",
         str(item.path)],
        check=False, capture_output=True, text=True,
    ).stdout
    times = []
    for line in out.split():
        for v in line.split(","):
            try:
                times.append(float(v))
                break
            except ValueError:
                continue
    before = [x for x in times if x <= t + 0.05]
    return max(before) if before else t


_VTT_TS = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})")


def _vtt_seconds(m: re.Match) -> float:
    h, mnt, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(s) + int(ms) / 1000


def _vtt_format(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def shift_vtt(vtt: str, offset: float) -> str:
    """ffmpeg's -ss doesn't shift text subtitles reliably, so cues are moved here."""
    if offset <= 0:
        return vtt
    blocks, out = vtt.replace("\r\n", "\n").split("\n\n"), []
    for block in blocks:
        lines = block.split("\n")
        idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if idx is None:
            out.append(block)
            continue
        a, _, rest = lines[idx].partition("-->")
        ma, mb = _VTT_TS.search(a), _VTT_TS.search(rest)
        if not (ma and mb):
            continue
        start, end = _vtt_seconds(ma) - offset, _vtt_seconds(mb) - offset
        if end <= 0:
            continue
        settings_tail = rest[mb.end():]
        lines[idx] = f"{_vtt_format(max(0.0, start))} --> {_vtt_format(end)}{settings_tail}"
        out.append("\n".join(lines))
    return "\n\n".join(out)


def ffmpeg_subs_cmd(item: Item, key: str) -> list[str] | None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if key.startswith("e"):
        cmd += ["-i", str(item.path), "-map", f"0:{int(key[1:])}"]
    elif key.startswith("x"):
        src = sidecar(item, int(key[1:]))
        if src is None:
            return None
        cmd += ["-i", str(src), "-map", "0:s:0"]
    else:
        return None
    return cmd + ["-f", "webvtt", "pipe:1"]


def probe_dict(p: Probe) -> dict:
    return asdict(p)
