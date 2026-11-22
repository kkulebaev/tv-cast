import pytest

from tv_cast import app, media


@pytest.fixture(autouse=True)
def _fresh_globals(monkeypatch):
    # One failing test must not leave streams or library entries that fail the next one.
    monkeypatch.setattr(app, "_streams", {})
    monkeypatch.setattr(media, "_library", {})
