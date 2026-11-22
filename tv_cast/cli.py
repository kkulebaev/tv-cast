import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from . import live
from .config import settings


def _api(path: str, body: dict | None = None, timeout: float = 10):
    url = f"http://127.0.0.1:{settings.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("detail")
        except ValueError:
            detail = None
        sys.exit(detail if isinstance(detail, str) else f"сервер ответил {e.code}")
    except OSError:
        sys.exit(f"tv-cast не запущен на порту {settings.port}: запустите tvcast")


def pick_device(devices: list[dict], wanted: str) -> dict:
    if wanted:
        w = wanted.lower()
        found = [d for d in devices
                 if w in d["name"].lower() or w in d["model"].lower() or w == d["host"]]
        if len(found) == 1:
            return found[0]
        if not found:
            sys.exit(f"телевизор «{wanted}» не найден. Есть: " + _names(devices))
        sys.exit(f"под «{wanted}» подходит несколько телевизоров: " + _names(found))
    if len(devices) == 1:
        return devices[0]
    sys.exit("телевизоров несколько, выберите --tv или TVCAST_TV: " + _names(devices))


def _names(devices: list[dict]) -> str:
    return ", ".join(f"{d['name']} ({d['host']})" for d in devices) or "ни одного"


def twitch(argv: list[str]):
    p = argparse.ArgumentParser(prog="tvcast twitch",
                                description="Показать Twitch-канал на телевизоре.")
    p.add_argument("channel", help="логин канала или ссылка twitch.tv/…")
    p.add_argument("quality", nargs="?", default="best", help="качество streamlink (best)")
    p.add_argument("--tv", default=settings.tv, help="имя, модель или IP телевизора")
    args = p.parse_args(argv)
    try:
        channel = live.channel_name(args.channel)
    except ValueError as e:
        sys.exit(str(e))
    # Right after the server starts, discovery may not have answered yet.
    devices, deadline = [], time.monotonic() + 10
    while not devices and time.monotonic() < deadline:
        devices = _api("/api/devices")
        if not devices:
            time.sleep(1)
    tv = pick_device(devices, args.tv)
    print(f"twitch.tv/{channel} ({args.quality}) → {tv['name']}, жду поток…", flush=True)
    r = _api("/api/live", {"device": tv["id"], "channel": channel, "quality": args.quality},
             timeout=live.READY_TIMEOUT + 30)
    if not r.get("started"):
        sys.exit("запуск отменён: на телевизоре успели запустить другое")
    print(f"идёт на {tv['name']}. Остановить: пульт в веб-интерфейсе", flush=True)
