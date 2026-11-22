import asyncio
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from tv_cast import app, media, progress
from tv_cast.config import settings


class _Request:
    def __init__(self, disconnect_after: int | None = None):
        self.calls, self.disconnect_after = 0, disconnect_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        if self.disconnect_after is None:
            return False
        # Gives the child time to fill the pipe so the reader pauses, as with a slow TV.
        await asyncio.sleep(0.3)
        return self.calls >= self.disconnect_after


def _collect(cmd, key=("tv", "film"), request=None) -> bytes:
    async def run():
        return b"".join([c async for c in app._pipe(cmd, request or _Request(), key)])

    # A hung pipe also hangs its own cleanup (proc.wait() waits for every pipe to close),
    # so asyncio timeouts can't end the test; a daemon thread with a join timeout can.
    out: list[bytes] = []
    worker = threading.Thread(target=lambda: out.append(asyncio.run(run())), daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert out, "stream hung"
    return out[0]


def test_pipe_survives_stderr_flood():
    # Far more than a pipe buffer holds: if stderr isn't drained, the child blocks forever.
    script = "import sys; sys.stderr.write('e' * 2_000_000); sys.stderr.flush(); print('done')"
    assert _collect([sys.executable, "-c", script]).strip() == b"done"
    assert app._streams == {}


def test_new_stream_for_same_client_and_file_kills_the_old_one():
    async def run():
        sleeper = "import time; print('a', flush=True); time.sleep(30)"
        slow = app._pipe([sys.executable, "-c", sleeper], _Request(), ("tv", "film"))
        first = await slow.__anext__()
        out = b"".join([c async for c in app._pipe(
            [sys.executable, "-c", "print('b')"], _Request(), ("tv", "film"))])
        rest = b"".join([c async for c in slow])
        return first, out, rest
    first, out, rest = asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert (first.strip(), out.strip(), rest) == (b"a", b"b", b"")
    assert app._streams == {}


def test_disconnect_with_unread_output_does_not_hang_cleanup():
    flood = "import sys\nwhile True: sys.stdout.buffer.write(b'x' * 65536)"
    started = time.monotonic()
    out = _collect([sys.executable, "-c", flood], request=_Request(disconnect_after=1))
    # Without draining stdout, cleanup only ends by the 10 s safety timeout.
    assert time.monotonic() - started < 5
    assert out
    assert app._streams == {}


def test_stream_answers_422_when_probe_fails(monkeypatch):
    item = media.Item("abc", Path("/m/broken.mkv"), "broken.mkv", 1, 0.0)
    monkeypatch.setattr(media, "get", lambda item_id: item if item_id == "abc" else None)

    def broken(_item):
        raise RuntimeError("moov atom not found")

    monkeypatch.setattr(media, "probe", broken)
    client = TestClient(app.app)
    assert client.get("/stream/abc.mp4").status_code == 422
    assert client.get("/stream/zzz.mp4").status_code == 404
    assert client.get("/stream/abc.mp4?mode=bogus").status_code == 400


def test_library_and_progress_api(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "settings", replace(settings, media_dir=tmp_path))
    monkeypatch.setattr(app, "settings", replace(settings, media_dir=tmp_path))
    (tmp_path / "Dark.S01E02.mkv").touch()
    client = TestClient(app.app)
    (item,) = client.get("/api/library").json()["items"]
    assert (item["series"], item["season"], item["episode"], item["progress"]) == ("Dark", 1, 2, {})


    def post(**body):
        return client.post("/api/progress", json={"item": item["id"], **body})

    assert post(pos=754).json()["pos"] == 754
    assert post(watched=True).json()["watched"] is True
    assert progress.store.path.exists()
    (item,) = client.get("/api/library").json()["items"]
    assert item["progress"]["watched"] is True
    assert client.post("/api/progress", json={"item": "nope", "pos": 100}).status_code == 404
    assert post(pos=-5).status_code == 422
    # A browser's leftover position never rolls back what the server already knows.
    assert post(pos=300, only_if_new=True).json()["watched"] is True
    # Both at once: the explicit mark wins over the position's implied "still watching".
    assert post(pos=300, watched=True).json() == {**post().json(), "watched": True}
