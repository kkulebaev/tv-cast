import json
import logging
import os
import threading
import time
from pathlib import Path

from .config import settings

log = logging.getLogger("tvcast.progress")

# Opening credits and accidental starts aren't worth a "continue from" offer.
MIN_POSITION = 60.0


def _is_finished(pos: float, duration: float) -> bool:
    # End credits run several minutes, so a film stopped during them counts as watched.
    return duration > 0 and duration - pos < max(120.0, duration * 0.05)


class Progress:
    """Watch positions by item id, persisted as JSON so every browser and phone shares them."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] | None = None
        self._dirty = False

    def _load(self) -> dict[str, dict]:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text())
            except FileNotFoundError:
                self._data = {}
            except ValueError:
                # Kept aside: the next flush would otherwise overwrite the whole history.
                bad = self.path.with_name(f"{self.path.name}.bad-{int(time.time())}")
                self.path.rename(bad)
                log.warning("unreadable %s moved to %s", self.path, bad)
                self._data = {}
        return self._data

    def get(self, item_id: str) -> dict:
        with self._lock:
            return dict(self._load().get(item_id, {}))

    def all(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._load().items()}

    def update(self, item_id: str, pos: float, duration: float = 0.0):
        with self._lock:
            data = self._load()
            rec = data.get(item_id, {})
            if _is_finished(pos, duration):
                rec.update(pos=0.0, watched=True)
            elif pos >= MIN_POSITION:
                rec.update(pos=round(pos, 1), watched=False)
            else:
                return
            if duration > 0:
                rec["duration"] = round(duration, 1)
            rec["at"] = time.time()
            data[item_id] = rec
            self._dirty = True

    def set_watched(self, item_id: str, watched: bool):
        with self._lock:
            data = self._load()
            rec = data.setdefault(item_id, {})
            rec.update(pos=0.0, watched=watched, at=time.time())
            self._dirty = True

    def flush(self):
        with self._lock:
            if not self._dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Written aside and renamed, so a crash mid-write can't wipe every position.
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1))
            os.replace(tmp, self.path)
            self._dirty = False


store = Progress(settings.state_dir / "progress.json")
