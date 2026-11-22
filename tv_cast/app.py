import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import media
from .cast import local_ip_for, manager
from .config import settings

log = logging.getLogger("tvcast")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    manager.start()
    yield
    manager.stop()


app = FastAPI(title="tv-cast", lifespan=lifespan)
# The Cast receiver fetches WebVTT via XHR from its own origin and drops the track
# without a CORS header.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])


def _item(item_id: str) -> media.Item:
    item = media.get(item_id)
    if item is None:
        raise HTTPException(404, "файл не найден")
    return item


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/devices")
async def devices():
    return manager.devices()


@app.get("/api/library")
async def library():
    items = await run_in_threadpool(media.scan)
    return {
        "root": str(settings.media_dir),
        "items": [
            {"id": i.id, "title": i.title, "rel": i.rel, "size": i.size, "mtime": i.mtime}
            for i in items
        ],
    }


@app.get("/api/item/{item_id}")
async def item_info(item_id: str, audio: int | None = None, transcode: bool = False):
    item = _item(item_id)
    try:
        probe = await run_in_threadpool(media.probe, item)
    except Exception as e:
        raise HTTPException(422, f"ffprobe не смог прочитать файл: {e}") from e
    mode, reason = media.plan(probe, audio, transcode)
    return {"id": item.id, "title": item.title, "rel": item.rel,
            "probe": media.probe_dict(probe), "mode": mode, "reason": reason}


class PlayRequest(BaseModel):
    device: str
    item: str
    audio: int | None = None
    sub: str | None = None
    start: float = 0.0
    transcode: bool = False


@app.post("/api/play")
async def play(req: PlayRequest):
    item = _item(req.item)
    try:
        s = await run_in_threadpool(
            manager.play, req.device, item, req.audio, req.sub, req.start, req.transcode
        )
    except KeyError as e:
        raise HTTPException(404, "телевизор не найден") from e
    return {"mode": s.mode, "reason": s.reason}


class ControlRequest(BaseModel):
    device: str
    action: str
    value: float | None = None


@app.post("/api/control")
async def control(req: ControlRequest):
    try:
        await run_in_threadpool(manager.control, req.device, req.action, req.value)
    except KeyError as e:
        raise HTTPException(404, "телевизор не найден") from e
    except ValueError as e:
        raise HTTPException(400, f"неизвестная команда {req.action}") from e
    return {"ok": True}


@app.get("/api/status/{device}")
async def status(device: str):
    try:
        return await run_in_threadpool(manager.status, device)
    except KeyError as e:
        raise HTTPException(404, "телевизор не найден") from e


@app.get("/media/{item_id}.{ext}")
async def direct(item_id: str, ext: str):
    item = _item(item_id)
    # Starlette's FileResponse answers Range requests, which the TV needs for seeking.
    return FileResponse(item.path, media_type="video/webm" if ext == "webm" else "video/mp4")


async def _pipe(cmd: list[str], request: Request, chunk: int = 256 * 1024):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        while True:
            data = await proc.stdout.read(chunk)
            if not data:
                break
            yield data
            if await request.is_disconnected():
                break
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        err = (await proc.stderr.read()).decode(errors="ignore").strip()
        if err and proc.returncode not in (0, -9):
            log.warning("ffmpeg (%s): %s", proc.returncode, err[-500:])


@app.get("/stream/{item_id}.mp4")
async def stream(request: Request, item_id: str, mode: str = "remux",
                 audio: int | None = None, start: float = 0.0):
    if mode not in ("remux", "transcode"):
        raise HTTPException(400, "mode")
    item = _item(item_id)
    probe = await run_in_threadpool(media.probe, item)
    cmd = media.ffmpeg_stream_cmd(item, probe, mode, audio, start)
    log.info("ffmpeg: %s", " ".join(cmd))
    return StreamingResponse(_pipe(cmd, request), media_type="video/mp4")


@app.get("/subs/{item_id}/{key}.vtt")
async def subs(request: Request, item_id: str, key: str, start: float = 0.0):
    item = _item(item_id)
    cmd = media.ffmpeg_subs_cmd(item, key)
    if cmd is None:
        raise HTTPException(404, "субтитры не найдены")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(422, f"ffmpeg: {err.decode(errors='ignore')[-300:]}")
    vtt = media.shift_vtt(out.decode("utf-8", errors="replace"), start)
    return Response(vtt, media_type="text/vtt; charset=utf-8")


def _lan_ip() -> str | None:
    # Routing toward a known TV rather than the internet keeps the VPN tunnel address
    # out of the answer: only the LAN address is reachable from the phone and the TV.
    for host in [*settings.known_hosts, "192.168.0.1"]:
        try:
            return local_ip_for(host)
        except OSError:
            continue
    return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log.info("library: %s", settings.media_dir)
    log.info("open http://localhost:%d", settings.port)
    if ip := _lan_ip():
        log.info("in LAN: http://%s:%d", ip, settings.port)
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
