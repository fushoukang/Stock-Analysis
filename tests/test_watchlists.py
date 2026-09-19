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
    create_user_watchlist,
    create_watchlist,
    delete_user_watchlist,
    delete_user_watchlists_file,
    delete_watchlist,
    get_user_watchlist,
    get_watchlist,
    load_user_watchlists,
    load_watchlists,
    update_user_watchlist,
    update_watchlist,
)


@pytest.fixture(autouse=True)
def _redirect_watchlists_path(tmp_path, monkeypatch):
    target = tmp_path / "watchlists.json"
    monkeypatch.setattr(wl_module, "_path", lambda: target)
    yield target


@pytest.fixture(autouse=True)
def _redirect_user_watchlists_dir(tmp_path, monkeypatch):
    d = tmp_path / "user_watchlists"
    monkeypatch.setattr(wl_module, "_user_path", lambda email: d / f"{email.replace('@', '_at_')}.json")
    yield d


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


# --- Per-user watchlists ---

ALICE = "alice@example.com"
BOB = "bob@example.com"


def test_load_user_watchlists_falls_back_to_default_template_unmaterialized():
    create_watchlist("Default A", "", ["spy"])  # seeds the shared template
    loaded = load_user_watchlists(ALICE)
    assert len(loaded) == 1
    assert loaded[0].name == "Default A"
    # Reading never persists anything for the user — no per-user file yet.
    assert not (wl_module._user_path(ALICE)).exists()


def test_first_create_forks_off_the_default_template():
    create_watchlist("Default A", "", ["spy"])
    created = create_user_watchlist(ALICE, "Alice Extra", "", ["qqq"])
    alice_lists = load_user_watchlists(ALICE)
    names = {w.name for w in alice_lists}
    assert names == {"Default A", "Alice Extra"}
    assert created.name == "Alice Extra"
    # The shared template itself is untouched by Alice's create.
    assert [w.name for w in load_watchlists()] == ["Default A"]


def test_user_watchlists_are_independent_across_accounts():
    create_watchlist("Default A", "", ["spy"])
    create_user_watchlist(ALICE, "Alice Only", "", ["aapl"])

    alice_names = {w.name for w in load_user_watchlists(ALICE)}
    bob_names = {w.name for w in load_user_watchlists(BOB)}
    assert alice_names == {"Default A", "Alice Only"}
    # Bob never touched his own list, so he still just sees the template —
    # Alice's addition doesn't leak into his view.
    assert bob_names == {"Default A"}


def test_update_and_delete_user_watchlist_scoped_to_that_user():
    create_watchlist("Default A", "", ["spy"])
    created = create_user_watchlist(ALICE, "Alice Extra", "", ["qqq"])

    updated = update_user_watchlist(ALICE, created.id, "Renamed", "note", ["dia"])
    assert updated is not None
    assert updated.name == "Renamed"
    assert get_user_watchlist(ALICE, created.id).symbols == ["DIA"]

    # Bob has no such watchlist id at all (he never forked off a copy with it).
    assert get_user_watchlist(BOB, created.id) is None

    assert delete_user_watchlist(ALICE, created.id) is True
    assert get_user_watchlist(ALICE, created.id) is None
    # The default template still has just its original one watchlist.
    assert len(load_watchlists()) == 1


def test_delete_user_watchlist_unknown_id_returns_false_and_materializes_nothing_extra():
    create_watchlist("Default A", "", ["spy"])
    assert delete_user_watchlist(ALICE, "nonexistent") is False
    # Delete on an unknown id still forks/materializes the user's copy (same
    # as the shared version's behavior) but leaves its contents unchanged.
    assert [w.name for w in load_user_watchlists(ALICE)] == ["Default A"]


def test_delete_user_watchlists_file_resets_to_default_template():
    create_watchlist("Default A", "", ["spy"])
    create_user_watchlist(ALICE, "Alice Extra", "", ["qqq"])
    assert {w.name for w in load_user_watchlists(ALICE)} == {"Default A", "Alice Extra"}

    delete_user_watchlists_file(ALICE)
    # Back to reading the shared template fresh, as if Alice were brand new.
    assert [w.name for w in load_user_watchlists(ALICE)] == ["Default A"]


def test_delete_user_watchlists_file_missing_file_is_a_noop():
    delete_user_watchlists_file(ALICE)  # never created — should not raise

