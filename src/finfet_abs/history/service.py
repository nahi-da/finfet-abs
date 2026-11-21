"""検索履歴・ピン留め（3.5.1〜3.5.3）。"""

from __future__ import annotations

import json
import logging
import uuid

from ..core.clock import SYSTEM_CLOCK, Clock, to_iso
from ..core.db import Database
from ..core.errors import AppError, NotFound
from .condition import condition_hash, targets_summary, variables_summary

log = logging.getLogger(__name__)

MAX_PINNED = 10


class HistoryService:
    def __init__(self, db: Database, settings, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock

    def record(self, cond: dict) -> str:
        """検索を始めるたびに記録します。同じ条件なら日時と回数を更新します。"""
        h = condition_hash(cond)
        now = to_iso(self.clock.now())
        text = json.dumps(cond, ensure_ascii=False)
        new_id = str(uuid.uuid4())
        limit = int(self.settings.get("history.max_entries"))

        def fn(c):
            row = c.execute("SELECT id FROM search_history WHERE condition_hash=?", (h,)).fetchone()
            if row:
                c.execute("UPDATE search_history SET condition_json=?, last_used_at=?, use_count=use_count+1 WHERE id=?",
                          (text, now, row[0]))
                hid = row[0]
            else:
                c.execute("INSERT INTO search_history(id, condition_json, condition_hash, created_at, last_used_at, use_count) "
                          "VALUES(?,?,?,?,?,1)", (new_id, text, h, now, now))
                hid = new_id
            # 上限を超えたら、ピン留めしていない古いものから削除します
            c.execute("DELETE FROM search_history WHERE pinned_at IS NULL AND id NOT IN (SELECT id FROM search_history "
                      "WHERE pinned_at IS NULL ORDER BY last_used_at DESC LIMIT ?)", (limit,))
            return hid
        return self.db.write(fn)

    def set_result_count(self, hid: str, n: int) -> None:
        self.db.submit(lambda c: c.execute("UPDATE search_history SET last_result_count=? WHERE id=?", (n, hid)))

    def public(self, row) -> dict:
        cond = json.loads(row["condition_json"])
        return {"id": row["id"], "condition": cond, "name": cond.get("name"), "path_pattern": cond.get("path_pattern", ""),
                "variables_summary": variables_summary(cond), "targets_summary": targets_summary(cond),
                "last_used_at": row["last_used_at"], "use_count": row["use_count"],
                "last_result_count": row["last_result_count"], "pinned": row["pinned_at"] is not None}

    def get(self, hid: str) -> dict:
        row = self.db.query_one("SELECT * FROM search_history WHERE id=?", (hid,))
        if row is None:
            raise NotFound("検索履歴が見つかりません。")
        return self.public(row)

    def list(self, q: str = "", offset: int = 0, limit: int = 200, pinned_and_recent: bool = False) -> dict:
        rows = self.db.query("SELECT * FROM search_history ORDER BY (pinned_at IS NULL), "
                             "CASE WHEN pinned_at IS NULL THEN last_used_at ELSE pinned_at END DESC")
        items = [self.public(r) for r in rows]
        if q:
            words = q.lower().split()
            items = [i for i in items if all(w in (f"{i['path_pattern']} {i['variables_summary']} {i['targets_summary']} "
                                                  f"{i['name'] or ''}").lower() for w in words)]
        if pinned_and_recent:
            pinned = [i for i in items if i["pinned"]]
            recent = [i for i in items if not i["pinned"]][:10]
            items = pinned + recent
        return {"total": len(items), "items": items[offset:offset + limit]}

    def pin(self, hid: str, pinned: bool) -> dict:
        self.get(hid)
        if pinned:
            n = self.db.scalar("SELECT COUNT(*) FROM search_history WHERE pinned_at IS NOT NULL AND id<>?", (hid,))
            if n >= MAX_PINNED:
                raise AppError("PIN_LIMIT", f"ピン留めできるのは {MAX_PINNED} 件までです。ほかのピン留めを外してください。")
            self.db.execute("UPDATE search_history SET pinned_at=COALESCE(pinned_at, ?) WHERE id=?",
                            (to_iso(self.clock.now()), hid))
        else:
            self.db.execute("UPDATE search_history SET pinned_at=NULL WHERE id=?", (hid,))
        return self.get(hid)

    def delete(self, ids: list[str]) -> int:
        if not ids:
            return 0
        return self.db.execute(f"DELETE FROM search_history WHERE id IN ({','.join('?' * len(ids))})", ids)
