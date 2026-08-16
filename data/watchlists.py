"""Named stock watchlists (name + note + symbol list each), shown in the
GUI's "Watchlists" category — distinct from `settings.watchlist`, which is
just the default set of symbols this app backfills/streams on startup.

Persisted as a single JSON file (see settings.watchlists_path, default
watchlists.json). This is simple, low-volume, single-user data — a plain
JSON file (read-modify-write on every change) is plenty; no need for
SQLite or a real database here.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import PROJECT_ROOT, settings

logger = logging.getLogger("data.watchlists")


@dataclass
class Watchlist:
    id: str
    name: str
    note: str = ""
    symbols: list[str] = field(default_factory=list)


def _path() -> Path:
    p = Path(settings.watchlists_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


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


def load_watchlists() -> list[Watchlist]:
    p = _path()
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


def save_watchlists(watchlists: list[Watchlist]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"watchlists": [asdict(w) for w in watchlists]}
    # Write to a temp file then rename, so a crash mid-write can't leave
    # watchlists.json truncated/corrupted.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(p)


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
