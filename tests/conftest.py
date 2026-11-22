import pytest

from tv_cast import app, media, progress


@pytest.fixture(autouse=True)
def _fresh_globals(monkeypatch, tmp_path):
    # One failing test must not leave streams or library entries that fail the next one.
    monkeypatch.setattr(app, "_streams", {})
    monkeypatch.setattr(media, "_library", {})
    # Tests must never touch the real watch history in ~/.local/state.
    monkeypatch.setattr(progress, "store", progress.Progress(tmp_path / "progress.json"))
