"""CRUD round-trip tests for data/watchlists.py.

Settings is a frozen dataclass (see config.py), so settings.watchlists_path
can't be monkeypatched directly (setattr on a frozen dataclass instance
raises FrozenInstanceError). Instead these tests monkeypatch the module's
_path() helper itself, redirecting every read/write to a tmp_path-backed
file without touching the real watchlists.json or the frozen Settings
instance at all.
"""
from __future__ import annotations

import pytest

import data.watchlists as wl_module
from data.watchlists import (
    create_watchlist,
    delete_watchlist,
    get_watchlist,
    load_watchlists,
    update_watchlist,
)


@pytest.fixture(autouse=True)
def _redirect_watchlists_path(tmp_path, monkeypatch):
    target = tmp_path / "watchlists.json"
    monkeypatch.setattr(wl_module, "_path", lambda: target)
    yield target


def test_load_watchlists_missing_file_returns_empty():
    assert load_watchlists() == []


def test_create_watchlist_persists_and_is_loadable():
    created = create_watchlist("Tech Leaders", "big tech", ["aapl", "MSFT", "aapl"])
    assert created.name == "Tech Leaders"
    assert created.note == "big tech"
    assert created.symbols == ["AAPL", "MSFT"]  # uppercased + deduped
    assert created.id  # a uuid-derived id was assigned

    loaded = load_watchlists()
    assert len(loaded) == 1
    assert loaded[0].id == created.id
    assert loaded[0].symbols == ["AAPL", "MSFT"]


def test_get_watchlist_by_id():
    created = create_watchlist("Growth", "", ["tqqq"])
    found = get_watchlist(created.id)
    assert found is not None
    assert found.name == "Growth"


def test_get_watchlist_unknown_id_returns_none():
    assert get_watchlist("does-not-exist") is None


def test_update_watchlist_replaces_fields():
    created = create_watchlist("Original", "note", ["spy"])
    updated = update_watchlist(created.id, "Renamed", "new note", ["qqq", "dia"])
    assert updated is not None
    assert updated.id == created.id
    assert updated.name == "Renamed"
    assert updated.note == "new note"
    assert updated.symbols == ["QQQ", "DIA"]

    reloaded = get_watchlist(created.id)
    assert reloaded.name == "Renamed"
    assert reloaded.symbols == ["QQQ", "DIA"]


def test_update_watchlist_unknown_id_returns_none_and_does_not_create():
    result = update_watchlist("nonexistent", "X", "", ["spy"])
    assert result is None
    assert load_watchlists() == []


def test_delete_watchlist_removes_it():
    created = create_watchlist("Temp", "", ["spy"])
    assert delete_watchlist(created.id) is True
    assert load_watchlists() == []
    assert get_watchlist(created.id) is None


def test_delete_watchlist_unknown_id_returns_false():
    create_watchlist("Keep", "", ["spy"])
    assert delete_watchlist("nonexistent") is False
    assert len(load_watchlists()) == 1  # untouched


def test_multiple_watchlists_independent_round_trip():
    a = create_watchlist("A", "", ["spy"])
    b = create_watchlist("B", "", ["qqq"])
    assert {w.id for w in load_watchlists()} == {a.id, b.id}

    delete_watchlist(a.id)
    remaining = load_watchlists()
    assert len(remaining) == 1
    assert remaining[0].id == b.id


def test_load_watchlists_malformed_json_returns_empty(_redirect_watchlists_path):
    _redirect_watchlists_path.parent.mkdir(parents=True, exist_ok=True)
    _redirect_watchlists_path.write_text("{not valid json")
    assert load_watchlists() == []
