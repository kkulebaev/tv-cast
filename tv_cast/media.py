import functools
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
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_TO_BT709 = "colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
_SUB_KEY = re.compile(r"[ex][0-9]+")
# The receiver wants BCP 47 codes, while ffprobe reports ISO 639-2 ones.
_LANGS = {
    "rus": "ru", "eng": "en", "ukr": "uk", "bel": "be", "ger": "de", "deu": "de",
    "fre": "fr", "fra": "fr", "spa": "es", "ita": "it", "por": "pt", "pol": "pl",
    "cze": "cs", "ces": "cs", "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh",
    "tur": "tr", "ara": "ar", "heb": "he", "dut": "nl", "nld": "nl", "swe": "sv",
    "fin": "fi", "nor": "no", "dan": "da", "hun": "hu", "gre": "el", "ell": "el",
}

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
    # What tells same-language tracks apart: the stream title ("Forced", "SDH") or the
    # sidecar's tags ("forced.en" in "Movie.forced.en.srt").
    variant: str = ""


@dataclass
class Probe:
    duration: float
    format_name: str
    video_codec: str
    video_profile: str
    pix_fmt: str
    width: int
    height: int
    color_transfer: str = ""
    audio: list[Track] = field(default_factory=list)
    subtitles: list[SubTrack] = field(default_factory=list)


@dataclass
class Item:
    id: str
    path: Path
    rel: str
    size: int
    mtime: float
    series: str = ""
    season: int = 0
    episode: int = 0

    @property
    def title(self) -> str:
        return self.path.stem


# "S01E04", "s1.e4", "S01x04", "1x04", and glued to a release tag: "WebRip720pS01x04".
# Glued forms need a capital S and two digits, and x264/x265 is never an episode number,
# so sequels like "Jaws2.x264" or "Pixels2x04" stay films.
_NOT_CODEC = r"(?!26[45](?!\d))"
_EPISODE = re.compile(
    rf"(?:(?<![^\W_])[sS](?P<s1>\d{{1,2}})|(?<=[A-Za-z])S(?P<s2>\d{{2}}))"
    rf"[ ._-]?[eExX]{_NOT_CODEC}(?P<e1>\d{{1,3}})(?!\d)"
    rf"|(?<![^\W_])(?P<s3>\d{{1,2}})[xX]{_NOT_CODEC}(?P<e2>\d{{2,3}})(?!\d)")
# Release tags that trail the show name: "The.Witcher.WebRip.720p.S01E04".
_RELEASE_TAG = re.compile(
    r"(?i)[\s\-_.\[(]*(?:web-?rip|web-?dl|bd-?rip|hd-?rip|hdtv|dvd-?rip|blu-?ray|\d{3,4}p"
    r"|x26[45]|h\.?26[45]|hevc|rus|eng|\(?(?:19|20)\d\d\)?)[\s\-_.\])]*$")
_SEASON_DIR = re.compile(
    r"(?i)^(?:(?:season|сезон|s)[ ._-]*\d+|\d+[ ._-]*(?:сезон|season)|specials?)$")


def _clean_name(raw: str) -> str:
    name = spaced = raw.replace(".", " ").replace("_", " ")
    while (stripped := _RELEASE_TAG.sub("", name)) != name:
        name = stripped
    glued = " " not in spaced.strip(" -") and name != spaced
    name = name.strip(" -[(")
    if glued:
        # "TheWitcherWebRip720p" -> "The Witcher"; real names like "iZombie" have no tags glued.
        name = re.sub(r"(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])", " ", name)
    return name


def episode_info(rel: str) -> tuple[str, int, int] | None:
    path = Path(rel)
    m = _EPISODE.search(path.stem)
    if m is None:
        return None
    season = int(m["s1"] or m["s2"] or m["s3"])
    episode = int(m["e1"] or m["e2"])
    name = _clean_name(path.stem[:m.start()])
    if m["s3"] and not name and not path.parent.parts:
        # A bare "10x10.2018.mkv" in the library root is a film title, not an episode.
        return None
    if not name:
        # "Ведьмак/Season 1/S01E04.mkv": the show is named by its folder.
        dirs = [d for d in path.parent.parts if not _SEASON_DIR.match(d)]
        name = dirs[-1] if dirs else ""
    return (name or "Без названия"), season, episode


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
                ep = episode_info(rel)
                found[item_id] = Item(item_id, p, rel, st.st_size, st.st_mtime,
                                      *(ep or ("", 0, 0)))
    global _library
    # Swapped whole: requests served from other threads must never see a half-built dict.
    _library = found
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


def _default_audio(p: Probe) -> Track | None:
    # What the receiver plays when no track is asked for: the flagged one, else the first.
    return next((a for a in p.audio if a.default), p.audio[0] if p.audio else None)


def next_episode(item: Item) -> Item | None:
    if not item.series:
        return None
    if not _library:
        scan()
    later = [i for i in _library.values()
             if i.series == item.series and (i.season, i.episode) > (item.season, item.episode)]
    if not later:
        return None
    first = min((i.season, i.episode) for i in later)
    copies = [i for i in later if (i.season, i.episode) == first]
    # With a 720p and a 1080p copy, stay with the release being watched, else take the biggest.
    return min(copies, key=lambda i: (_release(i) != _release(item), -i.size, i.rel))


def _release(item: Item) -> tuple[str, str]:
    """Folder and file name without the episode code: equal for episodes of one release."""
    stem = item.path.stem
    m = _EPISODE.search(stem)
    return str(Path(item.rel).parent), (stem[:m.start()] + stem[m.end():]) if m else stem


def match_audio(prev: Probe, index: int | None, nxt: Probe) -> int | None:
    """The track in the next episode that matches the one being heard; None is its default.

    Stream indexes differ between episodes, so tracks are matched by language first, then
    by title, codec and channels."""
    old = next((a for a in prev.audio if a.index == index), _default_audio(prev))
    if old is None or not nxt.audio:
        return None
    lang = bcp47(old.lang)
    candidates = [a for a in nxt.audio if bcp47(a.lang) == lang] if lang != "und" else nxt.audio
    # Ties go to the default track, which keeps direct playback possible.
    candidates = sorted(candidates, key=lambda a: not a.default)

    def score(a: Track) -> int:
        # The title names the dub ("LostFilm", "Дубляж") and outweighs codec and channels together.
        return 3 * (a.title == old.title) + (a.codec == old.codec) + (a.channels == old.channels)

    best = max(candidates, key=score, default=None)
    return None if best is None or best is _default_audio(nxt) else best.index


def match_sub(prev: Probe, key: str | None, nxt: Probe) -> str | None:
    old = next((s for s in prev.subtitles if s.key == key), None) if key else None
    if old is None:
        return None
    # Sidecar files say "ru" and embedded tracks "rus"; both mean the same language.
    lang = bcp47(old.lang)
    candidates = [s for s in nxt.subtitles if s.supported and bcp47(s.lang) == lang]
    forced = "forced" in old.variant.lower()

    def score(s: SubTrack) -> int:
        # Forced subtitles only cover foreign lines: swapping them for full ones (or back) is
        # worse than a different file kind.
        return (2 * (s.key[0] == old.key[0]) + 2 * (s.variant == old.variant)
                - 3 * (("forced" in s.variant.lower()) != forced))

    best = max(candidates, key=score, default=None)
    # Without a language tag only a track of the same kind and variant is a safe guess.
    if best is None or (lang == "und" and score(best) < 4):
        return None
    return best.key


def bcp47(lang: str) -> str:
    lang = lang.lower()
    if lang in _LANGS:
        return _LANGS[lang]
    return lang if len(lang) == 2 and lang.isalpha() else "und"


def _sidecar_lang(sub: Path, video: Path) -> str:
    # "Movie.forced.en.srt" -> "en": the language is the last tag that looks like one.
    # "hi" and "cc" in subtitle names mean hearing-impaired / closed captions, not Hindi.
    tags = sub.name[len(video.stem) + 1 : -len(sub.suffix)].split(".")
    return next(
        (t for t in reversed(tags) if t.lower() not in {"hi", "cc"} and bcp47(t) != "und"), "")


def probe(item: Item) -> Probe:
    # The directory mtime changes when sidecar subtitles are added or renamed.
    key = (str(item.path), item.mtime, item.path.parent.stat().st_mtime)
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
                label=" · ".join(x for x in (lang, title, codec) if x), variant=title,
                lang=lang, supported=codec in TEXT_SUBS,
            ))
    for n, p in enumerate(_sidecar_subs(item.path)):
        subs.append(SubTrack(key=f"x{n}", label=f"файл: {p.name}",
                             lang=_sidecar_lang(p, item.path),
                             variant=p.name[len(item.path.stem) + 1 : -len(p.suffix)]))

    result = Probe(
        duration=float(data.get("format", {}).get("duration") or 0),
        format_name=data.get("format", {}).get("format_name", ""),
        video_codec=video.get("codec_name", ""),
        video_profile=video.get("profile", ""),
        pix_fmt=video.get("pix_fmt", ""),
        width=video.get("width", 0),
        height=video.get("height", 0),
        color_transfer=video.get("color_transfer", ""),
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
        why = ("принудительно" if force_transcode
               else f"видео {p.video_codec} {p.video_profile}".strip())
        hdr = ", HDR → SDR" if p.color_transfer in HDR_TRANSFERS else ""
        return "transcode", f"{why} → H.264 через VAAPI{hdr}"
    default_audio = _default_audio(p)
    chosen = next((a for a in p.audio if a.index == audio_index), default_audio)
    # ffprobe reports .mkv and .webm identically; only VP8/VP9 means a real WebM.
    direct_container = p.format_name == MP4_FORMAT or (
        p.format_name == WEBM_FORMAT and p.video_codec in {"vp8", "vp9"}
    )
    # The receiver always plays the file's default audio track, so another one needs a remux.
    if direct_container and (
        chosen is None or (chosen.codec in CAST_AUDIO and chosen is default_audio)
    ):
        return "direct", "файл подходит как есть"
    reasons = []
    if not direct_container:
        reasons.append(f"контейнер {p.format_name.split(',')[0]}")
    if chosen and chosen.codec not in CAST_AUDIO:
        reasons.append(f"звук {chosen.codec}")
    elif chosen and chosen is not default_audio:
        reasons.append("не основная аудиодорожка")
    return "remux", ", ".join(reasons) + " → видео без перекодирования, звук в AAC"


@functools.cache
def gpu_tonemap() -> bool:
    """libplacebo needs a working Vulkan driver, and only a trial run can tell."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=s=64x64:d=0.04",
             "-vf", f"libplacebo={_TO_BT709}", "-f", "null", "-"],
            capture_output=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _transcode_video(p: Probe, vaapi: bool) -> list[str]:
    hdr = p.color_transfer in HDR_TRANSFERS
    filters = []
    if hdr:
        # An 8-bit H.264 stream still tagged PQ/HLG shows washed-out colors on the TV.
        filters.append(
            f"libplacebo=tonemapping=auto:{_TO_BT709}:format=nv12" if gpu_tonemap()
            else "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                 "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=nv12"
        )
    if vaapi:
        # Codecs the GPU can't decode (H.264 High 10, MPEG-4 ASP) fall back to software
        # frames, so the graph has to accept both kinds; hwupload passes VAAPI frames through.
        if hdr:
            filters.append("hwupload")
        else:
            filters += ["format=nv12|vaapi", "hwupload", "scale_vaapi=format=nv12"]
        out = ["-vf", ",".join(filters), "-c:v", "h264_vaapi", "-qp", "21"]
    else:
        out = (["-vf", ",".join(filters)] if filters else []) + [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]
    if hdr:
        out += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    return out


def ffmpeg_stream_cmd(
    item: Item, p: Probe, mode: Mode, audio_index: int | None, start: float
) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    vaapi = mode == "transcode" and Path(settings.vaapi_device).exists()
    if vaapi:
        cmd += ["-init_hw_device", f"vaapi=va:{settings.vaapi_device}", "-filter_hw_device", "va"]
        # The zscale fallback is software-only and can't take VAAPI frames.
        if p.color_transfer not in HDR_TRANSFERS or gpu_tonemap():
            cmd += ["-hwaccel", "vaapi", "-hwaccel_device", "va",
                    "-hwaccel_output_format", "vaapi"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(item.path), "-map", "0:V:0"]

    chosen = next((a for a in p.audio if a.index == audio_index), None)
    if chosen is None and p.audio:
        chosen = next((a for a in p.audio if a.default), p.audio[0])
    if chosen:
        cmd += ["-map", f"0:{chosen.index}"]

    if mode == "transcode":
        cmd += _transcode_video(p, vaapi)
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
    if not _SUB_KEY.fullmatch(key):
        return None
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if key.startswith("e"):
        cmd += ["-i", str(item.path), "-map", f"0:{int(key[1:])}"]
    else:
        src = sidecar(item, int(key[1:]))
        if src is None:
            return None
        cmd += ["-i", str(src), "-map", "0:s:0"]
    return cmd + ["-f", "webvtt", "pipe:1"]


def probe_dict(p: Probe) -> dict:
    return asdict(p)
