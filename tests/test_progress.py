import json

import pytest

from tv_cast.progress import Progress


@pytest.fixture
def store(tmp_path):
    return Progress(tmp_path / "state" / "progress.json")


def test_short_views_are_not_remembered(store):
    store.update("a", 30, 3600)
    assert store.get("a") == {}


def test_position_survives_a_restart(store):
    store.update("a", 754.36, 3600)
    store.flush()
    again = Progress(store.path)
    assert again.get("a")["pos"] == 754.4
    assert again.get("a")["duration"] == 3600
    assert again.get("a")["watched"] is False


def test_stopping_in_the_credits_marks_watched(store):
    store.update("a", 2000, 3600)
    store.update("a", 3550, 3600)
    assert store.get("a")["watched"] is True
    assert store.get("a")["pos"] == 0


def test_rewatching_clears_the_watched_mark(store):
    store.set_watched("a", True)
    store.update("a", 600, 3600)
    assert store.get("a")["watched"] is False


def test_unknown_duration_never_counts_as_finished(store):
    store.update("a", 9000)
    assert store.get("a") == {"pos": 9000, "watched": False, "at": store.get("a")["at"]}


def test_flush_writes_only_when_changed(store):
    store.flush()
    assert not store.path.exists()
    store.set_watched("a", True)
    store.flush()
    assert json.loads(store.path.read_text())["a"]["watched"] is True
    assert not store.path.with_suffix(".tmp").exists()


def test_corrupt_file_is_kept_aside_not_overwritten(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json")
    assert store.all() == {}
    (bad,) = store.path.parent.glob("progress.json.bad-*")
    assert bad.read_text() == "{not json"
