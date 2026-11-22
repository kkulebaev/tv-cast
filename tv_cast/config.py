import os
from dataclasses import dataclass, field
from pathlib import Path


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    media_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("TVCAST_MEDIA_DIR", Path.home() / "Видео" / "Фильмы")
        ).expanduser()
    )
    port: int = field(default_factory=lambda: int(os.environ.get("TVCAST_PORT", "8765")))
    # mDNS alone can miss the TV right after it wakes up; a known IP is probed directly.
    known_hosts: list[str] = field(
        default_factory=lambda: _list(os.environ.get("TVCAST_HOSTS"))
    )
    state_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("TVCAST_STATE_DIR")
            or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
            / "tvcast"
        ).expanduser()
    )
    autoplay: bool = field(
        default_factory=lambda: os.environ.get("TVCAST_AUTOPLAY", "1").lower()
        not in ("0", "false", "no", "off")
    )
    # Which TV `tvcast twitch` picks when several are on; matched against name, model or IP.
    tv: str = field(default_factory=lambda: os.environ.get("TVCAST_TV", ""))
    vaapi_device: str = field(
        default_factory=lambda: os.environ.get("TVCAST_VAAPI", "/dev/dri/renderD128")
    )


settings = Settings()
