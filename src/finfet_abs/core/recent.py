"""最近使った値（`recent_values`。候補の表示に使う。5.3.5）。"""

from __future__ import annotations

from .clock import SYSTEM_CLOCK, Clock, to_iso
from .db import Database

KEEP_PER_KIND = 50
KINDS = {"storage_account", "container", "directory", "path_template"}


def add(db: Database, kind: str, value: str, context: str = "", *, clock: Clock = SYSTEM_CLOCK) -> None:
    """値を記録します（種類ごとに 50 件まで）。"""
    if kind not in KINDS or not value:
        return
    now = to_iso(clock.now())

    def fn(c):
        c.execute("INSERT INTO recent_values(kind, value, context, last_used_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(kind, value, context) DO UPDATE SET last_used_at=excluded.last_used_at",
                  (kind, value, context or "", now))
        c.execute("DELETE FROM recent_values WHERE kind=? AND rowid NOT IN ("
                  "SELECT rowid FROM recent_values WHERE kind=? ORDER BY last_used_at DESC LIMIT ?)",
                  (kind, kind, KEEP_PER_KIND))
    db.submit(fn)


def get(db: Database, kind: str, context: str | None = None, limit: int = 20) -> list[str]:
    """新しい順の値の一覧。"""
    if context is None:
        rows = db.query("SELECT value, MAX(last_used_at) AS t FROM recent_values WHERE kind=? GROUP BY value "
                        "ORDER BY t DESC LIMIT ?", (kind, limit))
    else:
        rows = db.query("SELECT value FROM recent_values WHERE kind=? AND context=? ORDER BY last_used_at DESC LIMIT ?",
                        (kind, context, limit))
    return [r["value"] for r in rows]
