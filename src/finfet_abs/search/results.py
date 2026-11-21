"""検索結果の表（3.2.10.3）: サーバーで絞り込み・並べ替え・ページ分け・選択・CSV。"""

from __future__ import annotations

import csv
import io
import json
import threading
from collections import OrderedDict
from urllib.parse import quote

from ..core.clock import from_iso
from ..core.errors import NotFound, bad_request
from .shortener import CommonShortener, tail_names

#: 並べ替えのキー → 式の並び（式の中にカンマを含むものがあるため、文字列で分けずに並びで持ちます）
SORTS: dict[str, tuple[str, ...]] = {
    "name": ("r.sort_key",), "size": ("r.size",), "last_modified": ("r.last_modified",),
    "creation_time": ("r.creation_time",), "tier_change": ("r.access_tier_change_time",),
    "tier": ("IFNULL(r.archive_status, r.tier)",), "type": ("r.blob_type",),
    "target": ("r.storage_account", "r.container", "r.sort_key"), "notes": ("r.notes_json",),
}


def order_by(sort: str) -> str:
    """並べ替えの ORDER BY の式（同じ値の行は ID の順。逆順でも ID は昇順にしません）。"""
    desc = sort.startswith("-")
    cols = SORTS.get(sort.lstrip("-"), SORTS["name"])
    d = "DESC" if desc else "ASC"
    return ", ".join(f"{c} {d}" for c in cols) + f", r.id {d}"
NOTE_LABELS = {"rehydrating": "すでにリハイドレート中です", "downloaded": "過去のジョブでダウンロード済みです",
               "writing": "更新中の可能性があります"}


def escape_like(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_where(sid: str, f: dict) -> tuple[str, list]:
    """絞り込みの条件（キーワードは空白で区切ると AND。大文字・小文字を区別しない）。"""
    where = ["r.search_id=?"]
    params: list = [sid]
    for w in (f.get("q") or "").lower().split():
        where.append("r.blob_name_lower LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(w)}%")
    tiers = [t for t in (f.get("tier") or []) if t]
    if tiers:
        parts = []
        for t in tiers:
            if t == "rehydrating":
                parts.append("r.archive_status IS NOT NULL")
            elif t == "none":
                parts.append("r.tier IS NULL")
            else:
                parts.append("(r.tier=? AND r.archive_status IS NULL)" if t == "Archive" else "r.tier=?")
                params.append(t)
        where.append("(" + " OR ".join(parts) + ")")
    types = [t for t in (f.get("type") or []) if t]
    if types:
        where.append(f"r.blob_type IN ({','.join('?' * len(types))})")
        params += types
    if f.get("modified_from"):
        where.append("r.last_modified >= ?")
        params.append(f["modified_from"])
    if f.get("modified_to"):
        where.append("r.last_modified <= ?")
        params.append(f["modified_to"])
    if f.get("notes") == "with":
        where.append("r.notes_json IS NOT NULL")
    elif f.get("notes") == "without":
        where.append("r.notes_json IS NULL")
    if f.get("target") not in (None, ""):
        where.append("r.target_no=?")
        params.append(int(f["target"]))
    return " AND ".join(where), params


def filters_from_request(req) -> dict:
    return {"q": req.arg("q", ""), "tier": [x for x in (req.arg("tier") or "").split(",") if x],
            "type": [x for x in (req.arg("type") or "").split(",") if x], "modified_from": req.arg("from"),
            "modified_to": req.arg("to"), "notes": req.arg("notes"), "target": req.arg("target")}


class ResultsService:
    def __init__(self, db) -> None:
        self.db = db
        self._display: dict[tuple, tuple[int, object]] = {}
        self._lock = threading.Lock()
        #: 絞り込みごとの件数・合計サイズ（検索結果の行が変わらない間だけ使います）
        self._aggr: OrderedDict[tuple, tuple] = OrderedDict()

    def _check(self, sid: str):
        row = self.db.query_one("SELECT * FROM searches WHERE id=?", (sid,))
        if row is None:
            raise NotFound("検索結果が見つかりません（古い検索結果は削除されます）。")
        return row

    def _shortener(self, sid: str, mode: str):
        count = self.db.scalar("SELECT COUNT(*) FROM search_results WHERE search_id=?", (sid,))
        key = (sid, mode)
        with self._lock:
            cached = self._display.get(key)
            if cached and cached[0] == count:
                return cached[1]
        names = [r[0] for r in self.db.query("SELECT blob_name FROM search_results WHERE search_id=?", (sid,))]
        obj = CommonShortener(names) if mode == "common" else tail_names(names)
        with self._lock:
            self._display = {k: v for k, v in self._display.items() if k[0] != sid or k == key}
            self._display[key] = (count, obj)
        return obj

    def list(self, sid: str, f: dict, sort: str, offset: int, limit: int, display: str = "full") -> dict:
        self._check(sid)
        where, params = build_where(sid, f)
        order = order_by(sort)
        total = self._count(sid, f, where, params)
        # 並べ替えは ID だけで行い、表示する行だけを後から読みます（30 万件でも並べ替えの作業の領域を小さくするため）
        ids = [r[0] for r in self.db.query(
            f"SELECT r.id FROM search_results r WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", params + [limit, offset])]
        rows = []
        if ids:
            by_id = {r["id"]: r for r in self.db.query(
                f"SELECT r.*, EXISTS(SELECT 1 FROM search_selection s WHERE s.search_id=r.search_id AND s.result_id=r.id) AS sel "
                f"FROM search_results r WHERE r.id IN ({','.join('?' * len(ids))})", ids)}
            rows = [by_id[i] for i in ids if i in by_id]
        short = None
        if display in ("common", "tail") and rows:
            short = self._shortener(sid, display)
        items = []
        for r in rows:
            name = r["blob_name"]
            if display == "common" and short is not None:
                disp = short.short(name)
            elif display == "tail" and short is not None:
                disp = short.get(name, name)
            else:
                disp = name
            items.append({
                "id": r["id"], "target_no": r["target_no"], "storage_account": r["storage_account"],
                "endpoint_suffix": r["endpoint_suffix"], "container": r["container"], "blob_name": name,
                "display_name": disp, "tier": r["tier"], "archive_status": r["archive_status"],
                "rehydrate_priority": r["rehydrate_priority"], "size": r["size"], "blob_type": r["blob_type"],
                "creation_time": r["creation_time"], "last_modified": r["last_modified"],
                "access_tier_change_time": r["access_tier_change_time"],
                "notes": json.loads(r["notes_json"]) if r["notes_json"] else [], "selected": bool(r["sel"]),
            })
        return {"total": total, "items": items}

    # ------------------------------------------------------------ 件数・合計の記憶

    def _stamp(self, sid: str) -> tuple:
        """検索結果の行の変化を表す値（行の追加・削除で変わります）。索引だけで求めます。"""
        r = self.db.query_one("SELECT COUNT(*) AS n, MAX(id) AS m FROM search_results WHERE search_id=?", (sid,))
        return (r["n"], r["m"])

    def _cached(self, key: tuple, fn):
        with self._lock:
            if key in self._aggr:
                self._aggr.move_to_end(key)
                return self._aggr[key]
        value = fn()
        with self._lock:
            self._aggr[key] = value
            while len(self._aggr) > 64:
                self._aggr.popitem(last=False)
        return value

    @staticmethod
    def _fkey(f: dict) -> str:
        return json.dumps({k: v for k, v in f.items() if v not in (None, "", [])}, sort_keys=True, ensure_ascii=False)

    def _aggregate(self, sid: str, f: dict, where: str, params: list, stamp: tuple | None = None) -> tuple[int, int]:
        """絞り込みに一致する件数と合計サイズ。"""
        stamp = stamp or self._stamp(sid)
        if self._fkey(f) == "{}":
            return stamp[0], self._cached((sid, stamp, "{}"), lambda: self.db.scalar(
                "SELECT IFNULL(SUM(size),0) FROM search_results WHERE search_id=?", (sid,)))
        return self._cached((sid, stamp, self._fkey(f)), lambda: tuple(self.db.query_one(
            f"SELECT COUNT(*), IFNULL(SUM(size),0) FROM search_results r WHERE {where}", params)))

    def _count(self, sid: str, f: dict, where: str, params: list) -> int:
        return self._aggregate(sid, f, where, params)[0]

    def summary(self, sid: str, f: dict) -> dict:
        self._check(sid)
        where, params = build_where(sid, f)
        stamp = self._stamp(sid)
        fn, fb = self._aggregate(sid, f, where, params, stamp)
        an, ab = self._aggregate(sid, {}, *build_where(sid, {}), stamp)
        filt = {"n": fn, "b": fb}
        allr = {"n": an, "b": ab}
        sel = self.db.query_one(
            "SELECT COUNT(*) AS n, IFNULL(SUM(r.size),0) AS b, IFNULL(SUM(r.tier='Archive'),0) AS an, "
            "IFNULL(SUM(CASE WHEN r.tier='Archive' THEN r.size ELSE 0 END),0) AS ab FROM search_selection s "
            "JOIN search_results r ON r.id=s.result_id WHERE s.search_id=?", (sid,))
        if self._fkey(f) == "{}" or sel["n"] == 0:
            visible_sel = sel["n"]
        else:
            visible_sel = self.db.scalar(
                f"SELECT COUNT(*) FROM search_selection s JOIN search_results r ON r.id=s.result_id WHERE s.search_id=? AND {where}",
                [sid] + params)
        tiers = self._cached((sid, stamp, "tiers"), lambda: {
            r["t"]: {"count": r["n"], "bytes": r["b"]} for r in self.db.query(
                "SELECT CASE WHEN archive_status IS NOT NULL THEN 'rehydrating' ELSE IFNULL(tier,'none') END AS t, "
                "COUNT(*) AS n, IFNULL(SUM(size),0) AS b FROM search_results WHERE search_id=? GROUP BY t", (sid,))})
        return {"filtered": {"count": filt["n"], "bytes": filt["b"]}, "all": {"count": allr["n"], "bytes": allr["b"]},
                "selected": {"count": sel["n"], "bytes": sel["b"], "archive_count": sel["an"], "archive_bytes": sel["ab"]},
                "hidden_selected": sel["n"] - visible_sel, "tiers": tiers}

    def select(self, sid: str, body: dict) -> dict:
        """選択の変更（結果の ID の一覧 / 絞り込みに一致するすべて / 並びの範囲 / すべて外す）。"""
        self._check(sid)
        selected = bool(body.get("selected", True))
        if body.get("clear"):
            self.db.execute("DELETE FROM search_selection WHERE search_id=?", (sid,))
            return {"ok": True}
        if body.get("ids") is not None:
            ids = [int(x) for x in body["ids"]][:100000]
            if selected:
                self.db.write(lambda c: c.executemany(
                    "INSERT OR IGNORE INTO search_selection(search_id, result_id) SELECT ?, id FROM search_results "
                    "WHERE id=? AND search_id=?", [(sid, i, sid) for i in ids]))
            else:
                self.db.write(lambda c: c.executemany(
                    "DELETE FROM search_selection WHERE search_id=? AND result_id=?", [(sid, i) for i in ids]))
            return {"ok": True}
        f = body.get("filter") or {}
        where, params = build_where(sid, f)
        if body.get("range"):
            rg = body["range"]
            start, end = sorted((int(rg.get("start", 0)), int(rg.get("end", 0))))
            order = order_by(body.get("sort") or "name")
            sub = f"SELECT r.id FROM search_results r WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?"
            params2 = params + [end - start + 1, start]
        elif body.get("all_matching"):
            sub = f"SELECT r.id FROM search_results r WHERE {where}"
            params2 = params
        else:
            raise bad_request("選択の指定が正しくありません。")
        if selected:
            n = self.db.execute(f"INSERT OR IGNORE INTO search_selection(search_id, result_id) SELECT ?, id FROM ({sub})",
                                [sid] + params2)
        else:
            n = self.db.execute(f"DELETE FROM search_selection WHERE search_id=? AND result_id IN ({sub})", [sid] + params2)
        return {"ok": True, "changed": n}

    def selected_rows(self, sid: str, only_visible: dict | None = None):
        """ジョブの作成に使う、選んだ行（only_visible を指定すると、その絞り込みに一致するものだけ）。"""
        if only_visible is not None:
            where, params = build_where(sid, only_visible)
            return self.db.query(f"SELECT r.* FROM search_selection s JOIN search_results r ON r.id=s.result_id "
                                 f"WHERE s.search_id=? AND {where} ORDER BY r.sort_key", [sid] + params)
        return self.db.query("SELECT r.* FROM search_selection s JOIN search_results r ON r.id=s.result_id "
                             "WHERE s.search_id=? ORDER BY r.sort_key", (sid,))

    def csv(self, sid: str, f: dict, selected_only: bool, sort: str = "name") -> bytes:
        """CSV（UTF-8・BOM 付き）。URL は SAS を含めません。"""
        self._check(sid)
        where, params = build_where(sid, f)
        if selected_only:
            where += " AND EXISTS(SELECT 1 FROM search_selection s WHERE s.search_id=r.search_id AND s.result_id=r.id)"
        desc = sort.startswith("-")
        col = SORTS.get(sort.lstrip("-"), "r.sort_key")
        order = ", ".join(f"{c.strip()} {'DESC' if desc else 'ASC'}" for c in col.split(",")) + ", r.id"
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(["ストレージアカウント", "コンテナー", "Blob 名", "層", "リハイドレートの状態", "サイズ", "最終更新日時",
                    "作成日時", "層の変更日時", "種類", "注意", "URL"])
        for r in self.db.query(f"SELECT r.* FROM search_results r WHERE {where} ORDER BY {order}", params):
            notes = "、".join(NOTE_LABELS.get(n.get("code"), "") for n in json.loads(r["notes_json"])) if r["notes_json"] else ""
            url = f"https://{r['storage_account']}.blob.{r['endpoint_suffix']}/{quote(r['container'])}/{quote(r['blob_name'])}"
            w.writerow([r["storage_account"], r["container"], r["blob_name"], r["tier"] or "", r["archive_status"] or "",
                        r["size"], _local(r["last_modified"]), _local(r["creation_time"]),
                        _local(r["access_tier_change_time"]), r["blob_type"] or "", notes, url])
        return ("﻿" + buf.getvalue()).encode("utf-8")


def _local(iso: str | None) -> str:
    if not iso:
        return ""
    return from_iso(iso).astimezone().strftime("%Y/%m/%d %H:%M:%S")
