"""Named stock watchlists (name + note + symbol list each), shown in the
GUI's "Watchlists" category — distinct from `settings.watchlist`, which is
just the default set of symbols this app backfills/streams on startup.

Each account has its own private set of watchlists (see load_user_watchlists
et al. below) — editing, adding, or removing a watchlist only ever affects
the signed-in account. `watchlists.json` (settings.watchlists_path) is now
just the *default template*: the very first time an account touches its own
watchlists (a create/update/delete, or simply loading the page), its private
copy is seeded from whatever's in that file at that moment. After that, the
template and the account's copy are independent — later edits to one never
touch the other, and later edits to the template don't retroactively change
anyone who's already been seeded.

Both the template and each per-user copy are plain JSON files (read-modify-
write on every change) under data/user_watchlists/ — this is simple,
low-volume data; no need for SQLite or a real database here.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import PROJECT_ROOT, settings
from data.user_paths import user_slug

logger = logging.getLogger("data.watchlists")


@dataclass
class Watchlist:
    id: str
    name: str
    note: str = ""
    symbols: list[str] = field(default_factory=list)


def _path() -> Path:
    """Path to the shared default-template file (the original, single
    watchlists.json — unchanged from before per-user watchlists existed)."""
    p = Path(settings.watchlists_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def _user_path(email: str) -> Path:
    d = Path(settings.user_watchlists_dir)
    if not d.is_absolute():
        d = PROJECT_ROOT / d
    return d / f"{user_slug(email)}.json"


def _clean_symbols(symbols: list[str]) -> list[str]:
    """Uppercase, strip, dedupe-preserving-order, drop blanks — same
    cleanup rule used for monitor_list.txt (see
    alerts.kdj_monitor.save_monitor_symbols), so watchlists behave
    consistently with the other symbol-list editor in this app."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for s in symbols:
        sym = s.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            cleaned.append(sym)
    return cleaned


def _load_from_path(p: Path) -> list[Watchlist]:
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read/parse %s — treating as empty.", p, exc_info=True)
        return []
    items = raw.get("watchlists", []) if isinstance(raw, dict) else []
    result = []
    for item in items:
        try:
            result.append(
                Watchlist(
                    id=str(item["id"]),
                    name=str(item.get("name", "")),
                    note=str(item.get("note", "")),
                    symbols=[str(s) for s in item.get("symbols", [])],
                )
            )
        except (KeyError, TypeError):
            logger.warning("Skipping malformed watchlist entry: %r", item)
    return result


def _save_to_path(p: Path, watchlists: list[Watchlist]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"watchlists": [asdict(w) for w in watchlists]}
    # Write to a temp file then rename, so a crash mid-write can't leave
    # the file truncated/corrupted.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(p)


# --- Default template (shared, watchlists.json) ---
# Kept around unchanged so existing deployments/tests that call these
# directly keep working, and so it can still serve as the seed every new
# account's private copy starts from (see load_user_watchlists below).

def load_watchlists() -> list[Watchlist]:
    return _load_from_path(_path())


def save_watchlists(watchlists: list[Watchlist]) -> None:
    _save_to_path(_path(), watchlists)


def get_watchlist(watchlist_id: str) -> Watchlist | None:
    for w in load_watchlists():
        if w.id == watchlist_id:
            return w
    return None


def create_watchlist(name: str, note: str, symbols: list[str]) -> Watchlist:
    watchlists = load_watchlists()
    wl = Watchlist(id=uuid.uuid4().hex[:12], name=name.strip(), note=note.strip(), symbols=_clean_symbols(symbols))
    watchlists.append(wl)
    save_watchlists(watchlists)
    return wl


def update_watchlist(watchlist_id: str, name: str, note: str, symbols: list[str]) -> Watchlist | None:
    watchlists = load_watchlists()
    for i, w in enumerate(watchlists):
        if w.id == watchlist_id:
            updated = Watchlist(
                id=watchlist_id, name=name.strip(), note=note.strip(), symbols=_clean_symbols(symbols)
            )
            watchlists[i] = updated
            save_watchlists(watchlists)
            return updated
    return None


def delete_watchlist(watchlist_id: str) -> bool:
    watchlists = load_watchlists()
    remaining = [w for w in watchlists if w.id != watchlist_id]
    if len(remaining) == len(watchlists):
        return False
    save_watchlists(remaining)
    return True


# --- Per-user watchlists (what the GUI actually reads/writes now) ---

def load_user_watchlists(email: str) -> list[Watchlist]:
    """The signed-in account's own watchlists. If they haven't got a
    private copy yet (never created/edited/deleted one), this reads the
    shared default template directly — read-only, nothing is written to
    disk yet — so a brand-new account sees the same starting point as
    everyone else until they actually change something."""
    p = _user_path(email)
    if p.exists():
        return _load_from_path(p)
    return load_watchlists()


def _materialize_user_watchlists(email: str) -> list[Watchlist]:
    """Like load_user_watchlists, but if the account doesn't have a private
    copy yet, seeds one from the default template and persists it now.
    Called by every mutating operation below, so the very first create/
    edit/delete an account makes forks off a real, independent copy of the
    template rather than editing it in place for everyone."""
    p = _user_path(email)
    if p.exists():
        return _load_from_path(p)
    seeded = load_watchlists()
    _save_to_path(p, seeded)
    return seeded


def save_user_watchlists(email: str, watchlists: list[Watchlist]) -> None:
    _save_to_path(_user_path(email), watchlists)


def get_user_watchlist(email: str, watchlist_id: str) -> Watchlist | None:
    for w in load_user_watchlists(email):
        if w.id == watchlist_id:
            return w
    return None


def create_user_watchlist(email: str, name: str, note: str, symbols: list[str]) -> Watchlist:
    watchlists = _materialize_user_watchlists(email)
    wl = Watchlist(id=uuid.uuid4().hex[:12], name=name.strip(), note=note.strip(), symbols=_clean_symbols(symbols))
    watchlists.append(wl)
    save_user_watchlists(email, watchlists)
    return wl


def update_user_watchlist(
    email: str, watchlist_id: str, name: str, note: str, symbols: list[str]
) -> Watchlist | None:
    watchlists = _materialize_user_watchlists(email)
    for i, w in enumerate(watchlists):
        if w.id == watchlist_id:
            updated = Watchlist(
                id=watchlist_id, name=name.strip(), note=note.strip(), symbols=_clean_symbols(symbols)
            )
            watchlists[i] = updated
            save_user_watchlists(email, watchlists)
            return updated
    return None


def delete_user_watchlist(email: str, watchlist_id: str) -> bool:
    watchlists = _materialize_user_watchlists(email)
    remaining = [w for w in watchlists if w.id != watchlist_id]
    if len(remaining) == len(watchlists):
        return False
    save_user_watchlists(email, remaining)
    return True


def delete_user_watchlists_file(email: str) -> None:
    """Removes an account's private watchlists file entirely — called when
    an admin deletes that account (see web/app.py's admin delete route),
    so no orphaned per-user data is left behind."""
    p = _user_path(email)
    if p.exists():
        p.unlink()
