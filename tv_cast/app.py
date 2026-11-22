import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import media, progress
from .cast import local_ip_for, manager
from .config import settings

log = logging.getLogger("tvcast")
STATIC = Path(__file__).parent / "static"
# The receiver may open a stream URL again (or a seek restarts it) before dropping the old
# connection; one ffmpeg per client and file keeps the GPU from encoding the same film twice.
_streams: dict[tuple[str, str], asyncio.subprocess.Process] = {}


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


@app.exception_handler(RequestValidationError)
async def _invalid_request(_request: Request, exc: RequestValidationError):
    # The default answer echoes the rejected value, and inf/nan can't be encoded as JSON,
    # which turns a plain 422 into a 500.
    errors = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


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
    seen = progress.store.all()
    return {
        "root": str(settings.media_dir),
        "items": [
            {"id": i.id, "title": i.title, "rel": i.rel, "size": i.size, "mtime": i.mtime,
             "series": i.series, "season": i.season, "episode": i.episode,
             "progress": seen.get(i.id, {})}
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
            "series": item.series, "season": item.season, "episode": item.episode,
            "progress": progress.store.get(item.id),
            "probe": media.probe_dict(probe), "mode": mode, "reason": reason}


class ProgressRequest(BaseModel):
    item: str
    # Infinity would be stored and then break every library response (JSON has no inf).
    pos: float | None = Field(None, ge=0, allow_inf_nan=False)
    watched: bool | None = None
    # Positions carried over from a browser must not roll back newer server history.
    only_if_new: bool = False


@app.post("/api/progress")
async def set_progress(req: ProgressRequest):
    _item(req.item)
    if req.only_if_new and progress.store.get(req.item):
        return progress.store.get(req.item)
    if req.pos is not None:
        progress.store.update(req.item, req.pos)
    if req.watched is not None:
        progress.store.set_watched(req.item, req.watched)
    await run_in_threadpool(progress.store.flush)
    return progress.store.get(req.item)


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


async def _tail(stream: asyncio.StreamReader, keep: int = 4096) -> bytes:
    # Left unread, a full stderr pipe blocks ffmpeg mid-stream on files with broken frames.
    tail = b""
    while chunk := await stream.read(4096):
        tail = (tail + chunk)[-keep:]
    return tail


async def _pipe(cmd: list[str], request: Request, key: tuple[str, str],
                chunk: int = 256 * 1024):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    err_task = asyncio.create_task(_tail(proc.stderr))
    prev = _streams.get(key)
    if prev is not None and prev.returncode is None:
        prev.kill()
    _streams[key] = proc
    try:
        while True:
            data = await proc.stdout.read(chunk)
            if not data:
                break
            yield data
            if await request.is_disconnected():
                break
    finally:
        if _streams.get(key) is proc:
            del _streams[key]
        # Shielded: a client disconnect cancels this generator, and an abandoned cleanup
        # would leak the process and its pipes.
        tail = b""
        with anyio.CancelScope(shield=True), anyio.move_on_after(10):
            if proc.returncode is None:
                proc.kill()
            # proc.wait() returns only after every pipe closes, and stdout paused by a slow
            # client never reaches EOF unless it is read out.
            while await proc.stdout.read(chunk):
                pass
            await proc.wait()
            tail = await err_task
        err = tail.decode(errors="ignore").strip()
        if err and proc.returncode not in (0, -9):
            log.warning("ffmpeg (%s): %s", proc.returncode, err[-500:])


@app.get("/stream/{item_id}.mp4")
async def stream(request: Request, item_id: str, mode: str = "remux",
                 audio: int | None = None, start: float = 0.0):
    if mode not in ("remux", "transcode"):
        raise HTTPException(400, "mode")
    item = _item(item_id)
    try:
        probe = await run_in_threadpool(media.probe, item)
    except Exception as e:
        raise HTTPException(422, f"ffprobe не смог прочитать файл: {e}") from e
    # May run a trial ffmpeg once (media.gpu_tonemap), which must not stall the event loop.
    cmd = await run_in_threadpool(media.ffmpeg_stream_cmd, item, probe, mode, audio, start)
    log.info("ffmpeg: %s", " ".join(cmd))
    client = request.client.host if request.client else ""
    return StreamingResponse(_pipe(cmd, request, (client, item.id)), media_type="video/mp4")


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
