from dataclasses import replace

import pytest

from tv_cast import app, cast, media, progress


@pytest.fixture(autouse=True)
def _fresh_globals(monkeypatch, tmp_path):
    # One failing test must not leave streams or library entries that fail the next one.
    monkeypatch.setattr(app, "_streams", {})
    monkeypatch.setattr(media, "_library", {})
    # Tests must never touch the real watch history in ~/.local/state.
    monkeypatch.setattr(progress, "store", progress.Progress(tmp_path / "progress.json"))
    # Autoplay starts a thread that scans the real library; tests that need it turn it on.
    monkeypatch.setattr(cast, "settings", replace(cast.settings, autoplay=False))
