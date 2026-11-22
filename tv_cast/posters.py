import hashlib
import os
import subprocess
import threading
from pathlib import Path

from . import media
from .config import settings

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
FOLDER_ART = ("poster", "folder", "cover")
# Wide enough for the details pane on a phone, small enough for a long list.
WIDTH = 480
# Part of every cache key: bump it when the output changes, so old pictures aren't served.
VERSION = 1

Source = tuple[str, Path | int | float]


def _image(folder: Path, names: list[str]) -> Path | None:
    # Case-blind: Windows tools write "Folder.jpg", Kodi scrapers "Poster.png".
    try:
        files = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
    except OSError:
        return None
    for name in names:
        for ext in IMAGE_EXT:
            if hit := files.get(f"{name}{ext}".lower()):
                return hit
    return None


def _art_folders(item: media.Item) -> list[Path]:
    """Folders whose poster.jpg belongs to this item: its own and, for a season folder, the
    show's. The library root is shared by everything and never counts."""
    folders = []
    folder = item.path.parent
    root = settings.media_dir
    while folder != root and root in folder.parents and len(folders) < 2:
        folders.append(folder)
        if not media._SEASON_DIR.match(folder.name):
            break
        folder = folder.parent
    return folders


def source(item: media.Item, probe: media.Probe, series: bool = False) -> Source:
    """Where the picture comes from, best first. series: art for the whole show."""
    stem = item.path.stem
    own = _image(item.path.parent, [stem, f"{stem}-poster", f"{stem}.poster"])
    folder_art = next((a for f in _art_folders(item) if (a := _image(f, list(FOLDER_ART)))),
                      None)
    if series and folder_art:
        return "image", folder_art
    if probe.cover is not None:
        return "cover", probe.cover
    if own:
        return "image", own
    # A folder poster stands for the film inside, but not for each episode of a show.
    if folder_art and not item.series:
        return "image", folder_art
    # 10% in skips the studio logos and dark openings, and stays clear of spoilers.
    return "frame", round(probe.duration * 0.1, 1) if probe.duration > 0 else 30.0


def ffmpeg_cmd(item: media.Item, probe: media.Probe, src: Source, out: Path) -> list[str]:
    kind, value = src
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    scale = f"scale={WIDTH}:-2"
    if kind == "image":
        # image2 forced: probing may pick jpeg_pipe, which lacks -pattern_type; without
        # "none", image2 reads a "%" in the name as a frame-number pattern.
        cmd += ["-f", "image2", "-pattern_type", "none", "-i", str(value), "-vf", scale]
    elif kind == "cover":
        cmd += ["-i", str(item.path), "-map", f"0:{value}", "-vf", scale]
    else:
        vf = scale
        if probe.color_transfer in media.HDR_TRANSFERS:
            # An untonemapped HDR frame comes out grey and washed out.
            vf = f"{media.tonemap_filter()},{scale}"
        cmd += ["-ss", f"{value:.1f}", "-i", str(item.path), "-map", "0:V:0", "-vf", vf]
    return cmd + ["-frames:v", "1", "-q:v", "4", "-f", "image2", str(out)]


class NoPoster(Exception):
    pass


def locate(item: media.Item, series: bool = False) -> tuple[Source, Path, media.Probe]:
    """The picture's source and its cache file; cheap, so cache hits skip the ffmpeg queue."""
    probe = media.probe(item)
    src = source(item, probe, series)
    origin = src[1] if src[0] == "image" else item.path
    stamp = f"{VERSION}|{WIDTH}|{src}|{origin}|{Path(origin).stat().st_mtime}"
    name = f"{item.id}-{'s' if series else 'i'}-{hashlib.sha1(stamp.encode()).hexdigest()[:12]}"
    return src, settings.state_dir / "posters" / f"{name}.jpg", probe


def render(item: media.Item, probe: media.Probe, src: Source, out: Path) -> Path:
    if out.exists():  # another request made it while this one waited its turn
        return out
    failed = out.with_suffix(".fail")
    if failed.exists():
        # A file ffmpeg can't take a picture from fails the same way every time; the marker
        # is keyed like the picture, so a new source gets a fresh try.
        raise NoPoster(item.rel)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Written aside and renamed: two requests for one poster must not serve a half file.
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.{threading.get_ident()}.tmp.jpg")
    try:
        subprocess.run(ffmpeg_cmd(item, probe, src, tmp), check=True, capture_output=True,
                       timeout=60)
        os.replace(tmp, out)  # FileNotFoundError when ffmpeg exited 0 without a picture
    except Exception:
        failed.touch()
        raise
    finally:
        tmp.unlink(missing_ok=True)
    # Older pictures of this item (a replaced poster.jpg, a re-encoded file) are dead weight.
    prefix = out.stem.rsplit("-", 1)[0]
    for old in out.parent.glob(f"{prefix}-*"):
        if old != out and old.suffix in (".jpg", ".fail") and ".tmp." not in old.name:
            old.unlink(missing_ok=True)
    return out


def get(item: media.Item, series: bool = False) -> Path:
    src, out, probe = locate(item, series)
    return out if out.exists() else render(item, probe, src, out)
