"""ジョブの作成とキュー（3.3.1〜3.3.5、3.3.12、3.3.13、3.3.15、5.3.3、5.3.4）。

実行（確認・リハイドレート・ダウンロード）は engine.py が行います。ここでは、ジョブと Blob の記録の作成、
キューの状態の変更、集計を扱います。状態を変えたら on_change を呼び、実行の枠の割り当てを促します。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Callable

from ..core import dpapi
from ..core.clock import SYSTEM_CLOCK, Clock, from_iso, to_iso
from ..core.errors import AppError, NotFound
from ..history.condition import targets_summary, value_summary
from ..search.sortkey import sort_key
from . import paths

log = logging.getLogger(__name__)

#: 終了した状態（3.3.3）
ENDED = ("completed", "completed_with_errors", "cancelled")
#: キューのタブに出す状態
QUEUE = ("held", "queued", "running", "waiting_rehydrate", "waiting_download", "paused")
#: 未完了の Blob（3.3.6）
UNFINISHED = ("pending", "checking", "rehydrate_queued", "rehydrating", "ready", "downloading", "error", "cancelled")
ACTIVE_ITEMS = ("pending", "checking", "rehydrate_queued", "rehydrating", "ready", "downloading")
METHODS = ("rehydrate_download", "rehydrate_only", "download_only")
TIERS = ("Hot", "Cool", "Cold")
CONFLICTS = ("number", "skip", "overwrite")
DUP_RESOLUTIONS = ("suggest", "number", "skip")
EARLY_DELETE_DAYS = 180

STATUS_LABELS = {"held": "未開始", "queued": "開始待ち", "running": "実行中", "waiting_rehydrate": "リハイドレート待ち",
                 "waiting_download": "ダウンロード待ち", "paused": "一時停止", "completed": "完了",
                 "completed_with_errors": "一部失敗", "cancelled": "中止"}
ITEM_LABELS = {"pending": "待機中", "checking": "確認中", "rehydrate_queued": "リハイドレートの要求待ち",
               "rehydrating": "リハイドレート中", "ready": "ダウンロード待ち", "downloading": "ダウンロード中",
               "completed": "完了", "skipped": "スキップ", "error": "エラー", "cancelled": "中止"}

_ITEM_COLS = ("storage_account", "endpoint_suffix", "container", "blob_name", "blob_name_lower", "sort_key", "blob_type",
              "size", "etag", "content_md5", "creation_time", "last_modified", "access_tier_change_time", "tier_at_search",
              "archive_status_at_search", "rehydrate_priority_at_search", "tier", "archive_status", "notes_json",
              "vars_json", "auth_kind", "credential_id", "secret_id", "tenant_id", "status", "status_reason",
              "planned_path", "rehydrate_priority", "rehydrate_target_tier", "rehydrate_requested_known", "updated_at")


def default_options(settings) -> dict:
    """取得の設定の初期値（3.3.2。設定の値）。"""
    g = settings.get
    return {
        "method": g("fetch.method"), "target_tier": g("rehydrate.target_tier"), "priority": g("rehydrate.priority"),
        "directory": g("fetch.directory"), "job_folder": bool(g("fetch.job_folder")),
        "path_structure": g("fetch.path_structure"), "path_template": g("fetch.path_template"),
        "conflict": g("fetch.conflict"), "set_mtime": bool(g("fetch.set_mtime")), "verify_md5": bool(g("fetch.verify_md5")),
        "check_concurrency": g("transfer.check_concurrency"), "rehydrate_concurrency": g("transfer.rehydrate_concurrency"),
        "download_concurrency": g("transfer.download_concurrency"),
        "connections_per_blob": g("transfer.connections_per_blob"), "chunk_size_mib": g("transfer.chunk_size_mib"),
    }


def clean_options(raw: dict | None, settings) -> dict:
    """取得の設定を検査します（知らない項目は無視します）。"""
    o = default_options(settings)
    raw = raw or {}
    for k in o:
        if k in raw and raw[k] is not None:
            o[k] = raw[k]

    def choice(k, allowed):
        if o[k] not in allowed:
            raise AppError("OPTION_INVALID", f"取得の設定「{k}」の値が正しくありません。", extra={"field": k})
    choice("method", METHODS)
    choice("target_tier", TIERS)
    choice("priority", ("Standard", "High"))
    choice("path_structure", paths.STRUCTURES)
    choice("conflict", CONFLICTS)
    for k, lo, hi in (("check_concurrency", 1, 16), ("rehydrate_concurrency", 1, 16), ("download_concurrency", 1, 16),
                      ("connections_per_blob", 1, 8), ("chunk_size_mib", 1, 64)):
        try:
            o[k] = int(o[k])
        except (TypeError, ValueError):
            raise AppError("OPTION_INVALID", f"取得の設定「{k}」の値が正しくありません。", extra={"field": k}) from None
        if not lo <= o[k] <= hi:
            raise AppError("OPTION_INVALID", f"取得の設定「{k}」は {lo}〜{hi} にしてください。", extra={"field": k})
    for k in ("job_folder", "set_mtime", "verify_md5"):
        o[k] = bool(o[k])
    o["directory"] = str(o["directory"] or "").strip()
    o["path_template"] = str(o["path_template"] or "")
    return o


def expand_dir(directory: str) -> str:
    return os.path.expandvars(directory)


def windows_env() -> dict:
    """作成・中止した Windows ユーザーとコンピューター（ローカルの記録にだけ残します）。"""
    return {"user": os.environ.get("USERNAME", ""), "computer": os.environ.get("COMPUTERNAME", "")}


class JobService:
    def __init__(self, db, settings, events, *, clock: Clock = SYSTEM_CLOCK, url_sessions=None,
                 on_change: Callable[[], None] | None = None) -> None:
        self.db = db
        self.settings = settings
        self.events = events
        self.clock = clock
        self.url_sessions = url_sessions
        self.on_change = on_change or (lambda: None)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 対象の Blob

    def collect(self, source: dict) -> tuple[list[dict], dict]:
        """ジョブの対象の Blob と、元の情報（検索条件・URL の要約）を集めます。"""
        kind = source.get("kind")
        if kind == "search":
            return self._collect_search(source)
        if kind == "urls":
            return self._collect_urls(source)
        if kind == "job":
            return self._collect_job(source)
        raise AppError("SOURCE_INVALID", "ジョブの対象が正しくありません。")

    def _collect_search(self, source: dict) -> tuple[list[dict], dict]:
        from ..search.results import build_where
        sid = source.get("search_id") or ""
        srow = self.db.query_one("SELECT * FROM searches WHERE id=?", (sid,))
        if srow is None:
            raise NotFound("検索結果が見つかりません（古い検索結果は削除されます）。")
        targets = {r["target_no"]: r for r in self.db.query("SELECT * FROM search_targets WHERE search_id=?", (sid,))}
        sql = ("SELECT r.* FROM search_selection s JOIN search_results r ON r.id=s.result_id WHERE s.search_id=?")
        params: list = [sid]
        if source.get("only_visible") is not None:
            where, p2 = build_where(sid, source["only_visible"])
            sql += f" AND {where}"
            params += p2
        rows = self.db.query(sql + " ORDER BY r.sort_key", params)
        if not rows:
            raise AppError("NO_ITEMS", "Blob を選んでください。")
        out = []
        for r in rows:
            t = targets.get(r["target_no"])
            out.append({
                "storage_account": r["storage_account"], "endpoint_suffix": r["endpoint_suffix"], "container": r["container"],
                "blob_name": r["blob_name"], "sort_key": r["sort_key"], "blob_type": r["blob_type"], "size": r["size"],
                "etag": r["etag"], "content_md5": r["content_md5"], "creation_time": r["creation_time"],
                "last_modified": r["last_modified"], "access_tier_change_time": r["access_tier_change_time"],
                "tier": r["tier"], "archive_status": r["archive_status"], "rehydrate_priority": r["rehydrate_priority"],
                "notes_json": r["notes_json"], "vars": json.loads(r["vars_json"]) if r["vars_json"] else {},
                "auth_kind": t["auth_kind"] if t else None, "credential_id": t["credential_id"] if t else None,
                "tenant_id": t["tenant_id"] if t else None,
            })
        cond = json.loads(srow["condition_json"])
        return out, {"source": "search", "search_id": sid, "condition": cond, "searched_at": srow["started_at"]}

    def _collect_urls(self, source: dict) -> tuple[list[dict], dict]:
        rows = self.url_sessions.get(source.get("token") or "") if self.url_sessions else None
        if rows is None:
            raise AppError("URLS_EXPIRED", "URL の解析からの時間が経ちすぎました。もう一度 [解析] を押してください。")
        auth = source.get("auth") or {}
        out = []
        accounts: set[str] = set()
        for r in rows:
            if "error" in r or r.get("duplicate"):
                continue
            item = {"storage_account": r["storage_account"], "endpoint_suffix": r["endpoint_suffix"],
                    "container": r["container"], "blob_name": r["blob_name"], "sort_key": sort_key(r["blob_name"]),
                    "vars": {}, "notes_json": None}
            if r.get("sas_token"):
                item.update(auth_kind="sas", sas_token=r["sas_token"], sas=r.get("sas"))
            else:
                a = auth.get(f"{r['storage_account']}|{r['endpoint_suffix']}") or auth.get(r["storage_account"]) or {}
                if isinstance(a, str):
                    # 画面の選択の値（"entra_user" など、または "cred:<保存した認証情報の ID>"）
                    a = {"credential_id": a[5:]} if a.startswith("cred:") else {"auth_kind": a}
                if a.get("credential_id") and not a.get("auth_kind"):
                    kind = self.db.scalar("SELECT auth_kind FROM credentials WHERE id=?", (a["credential_id"],))
                    a = {**a, "auth_kind": kind}
                if not a.get("auth_kind"):
                    raise AppError("AUTH_REQUIRED", f"ストレージアカウント「{r['storage_account']}」の認証方式を選んでください。")
                item.update(auth_kind=a["auth_kind"], credential_id=a.get("credential_id"), tenant_id=a.get("tenant"))
            out.append(item)
            accounts.add(r["storage_account"])
        if not out:
            raise AppError("NO_ITEMS", "ジョブにできる URL がありません。")
        out.sort(key=lambda x: x["sort_key"])
        return out, {"source": "urls", "urls_summary": {"count": len(out), "accounts": sorted(accounts)}}

    def _collect_job(self, source: dict) -> tuple[list[dict], dict]:
        """すべて再実行・選択した Blob の再実行（3.3.13）。元のジョブの Blob の記録から作ります。"""
        job = self._job(source.get("job_id") or "")
        sql = "SELECT * FROM job_items WHERE job_id=?"
        params: list = [job["id"]]
        ids = source.get("item_ids")
        if ids:
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params += [int(i) for i in ids]
        rows = self.db.query(sql + " ORDER BY sort_key", params)
        if not rows:
            raise AppError("NO_ITEMS", "Blob を選んでください。")
        out = []
        for r in rows:
            out.append({
                "storage_account": r["storage_account"], "endpoint_suffix": r["endpoint_suffix"], "container": r["container"],
                "blob_name": r["blob_name"], "sort_key": r["sort_key"], "blob_type": r["blob_type"], "size": r["size"],
                "etag": r["etag"], "content_md5": r["content_md5"], "creation_time": r["creation_time"],
                "last_modified": r["last_modified"], "access_tier_change_time": r["access_tier_change_time"],
                "tier": r["tier"] or r["tier_at_search"], "archive_status": r["archive_status"],
                "rehydrate_priority": r["rehydrate_priority_at_search"], "notes_json": r["notes_json"],
                "vars": json.loads(r["vars_json"]) if r["vars_json"] else {}, "auth_kind": r["auth_kind"],
                "credential_id": r["credential_id"], "secret_id": r["secret_id"], "tenant_id": r["tenant_id"],
            })
        ctx = {"source": "rerun", "source_job_id": job["id"], "search_id": job["search_id"],
               "condition": json.loads(job["search_json"]) if job["search_json"] else None,
               "urls_summary": json.loads(job["urls_summary_json"]) if job["urls_summary_json"] else None}
        return out, ctx

    # ------------------------------------------------------------ ダイアログの表示（4.4.7）

    def preview(self, source: dict) -> dict:
        items, ctx = self.collect(source)
        tiers: dict[str, dict] = {}
        early = 0
        limit = self.clock.now() - timedelta(days=EARLY_DELETE_DAYS)
        for it in items:
            t = "rehydrating" if it.get("archive_status") else (it.get("tier") or "")
            d = tiers.setdefault(t, {"count": 0, "bytes": 0})
            d["count"] += 1
            d["bytes"] += it.get("size") or 0
            if it.get("tier") == "Archive" and not it.get("archive_status"):
                changed = from_iso(it.get("access_tier_change_time"))
                if changed is not None and changed > limit:
                    early += 1
        sas = self._sas_warnings(items)
        return {"count": len(items), "bytes": sum(i.get("size") or 0 for i in items), "tiers": tiers,
                "archive_count": tiers.get("Archive", {}).get("count", 0), "early_delete_count": early,
                "free_bytes": free_bytes(expand_dir(self.settings.get("fetch.directory"))),
                "default_name": self.default_name(items, ctx), "sas_warnings": sas,
                "recent_templates": [r[0] for r in self.db.query(
                    "SELECT value FROM recent_values WHERE kind='path_template' ORDER BY last_used_at DESC LIMIT 20")],
                "variables": sorted(paths.variables_of(ctx.get("condition")).keys()), "url_mode": ctx["source"] == "urls"
                or (ctx["source"] == "rerun" and ctx.get("condition") is None)}

    def _sas_warnings(self, items: list[dict]) -> list[dict]:
        out = {}
        hours = int(self.settings.get("auth.sas_expiry_warn_hours"))
        now = self.clock.now()
        for it in items:
            sas = it.get("sas") or {}
            exp = from_iso(sas.get("expiry")) if sas else None
            if exp is None:
                continue
            key = it["storage_account"]
            if exp <= now + timedelta(hours=hours) or exp <= now + timedelta(hours=15):
                cur = out.get(key)
                if cur is None or exp < from_iso(cur["expires_at"]):
                    out[key] = {"storage_account": key, "expires_at": to_iso(exp),
                                "within_hours": hours if exp <= now + timedelta(hours=hours) else None,
                                "before_rehydrate": exp <= now + timedelta(hours=15)}
        return list(out.values())

    def default_name(self, items: list[dict], ctx: dict) -> str:
        """ジョブ名の既定（検索条件の要約。例: 「stexample01/logs ほか1件・11/13〜11/20」）。"""
        if ctx.get("source") == "urls":
            return f"URL で指定 {len(items)} 件"
        cond = ctx.get("condition") or {}
        name = cond.get("name") or targets_summary(cond) or "ジョブ"
        for v in (cond.get("variables") or {}).values():
            if v.get("type") == "datetime":
                s = value_summary(v)
                if s:
                    return f"{name}・{s}"
        return name

    # ------------------------------------------------------------ 保存するパス（3.3.15）

    def plan(self, items: list[dict], ctx: dict, options: dict) -> tuple[list[str], paths.Template | None]:
        """Blob ごとの保存するパス（直した後）。指定の誤りは TemplateError。"""
        tmpl = None
        if options["path_structure"] == "template":
            variables = paths.variables_of(ctx.get("condition"))
            tmpl = paths.Template.parse(options["path_template"], variables, url_mode=ctx.get("condition") is None)
        planner = paths.PathPlanner(options["path_structure"], tmpl)
        return [planner.path(it) for it in items], tmpl

    def path_check(self, source: dict, options: dict) -> dict:
        """保存するパスの検査・例・重複の集計・提案（4.4.7）。"""
        options = clean_options(options, self.settings)
        items, ctx = self.collect(source)
        try:
            planned, _tmpl = self.plan(items, ctx, options)
        except paths.TemplateError as exc:
            return {"ok": False, "issues": [i.to_json() for i in exc.issues], "examples": [], "duplicates": None}
        examples = [{"blob": f"{it['storage_account']}/{it['container']}/{it['blob_name']}", "path": p}
                    for it, p in list(zip(items, planned))[:3]]
        groups = paths.find_duplicates(planned)
        suggestion = None
        if groups:
            base = options["path_template"] if options["path_structure"] == "template" else \
                paths.base_template(options["path_structure"])
            suggestion = paths.suggest(items, list(groups.values()), base, paths.variables_of(ctx.get("condition")))
        existing = 0
        if not options["job_folder"]:
            root = Path(expand_dir(options["directory"]))
            if root.is_dir():
                existing = sum(1 for p in planned if os.path.exists(paths.long_path(root / p)))
        sugg_examples = []
        if suggestion:
            try:
                t2 = paths.Template.parse(suggestion, paths.variables_of(ctx.get("condition")),
                                          url_mode=ctx.get("condition") is None)
                sugg_examples = [t2.render(items[g[i]]) for g in list(groups.values())[:1] for i in range(min(2, len(g)))]
            except paths.TemplateError:
                suggestion = None
        num_examples = []
        if groups:
            g = list(groups.values())[0]
            num_examples = [planned[g[0]], paths.numbered(planned[g[1]], 1)]
        return {"ok": True, "issues": [], "examples": examples,
                "duplicates": {"blobs": sum(len(g) for g in groups.values()), "places": len(groups),
                               "skip_count": sum(len(g) - 1 for g in groups.values())} if groups else None,
                "suggestion": suggestion, "suggestion_examples": sugg_examples, "number_examples": num_examples,
                "existing": existing}

    def path_groups(self, source: dict, options: dict, offset: int = 0, limit: int = 200) -> dict:
        """重複の組の一覧（4.4.8）。"""
        options = clean_options(options, self.settings)
        items, ctx = self.collect(source)
        try:
            planned, _ = self.plan(items, ctx, options)
        except paths.TemplateError as exc:
            raise AppError("PATH_INVALID", exc.issues[0].message) from None
        groups = list(paths.find_duplicates(planned).values())
        out = []
        for g in groups[offset:offset + limit]:
            out.append({"path": planned[g[0]], "blobs": [
                {"storage_account": items[i]["storage_account"], "container": items[i]["container"],
                 "blob_name": items[i]["blob_name"]} for i in g]})
        return {"total": len(groups), "items": out}

    # ------------------------------------------------------------ 作成（3.3.5）

    def create(self, source: dict, options: dict, *, name: str | None = None, start: bool | None = None,
               dup_resolution: str | None = None, confirm_rehydrate: bool = False) -> str:
        options = clean_options(options, self.settings)
        if not options["directory"]:
            raise AppError("DIRECTORY_REQUIRED", "保存先を指定してください。", extra={"field": "directory"})
        items, ctx = self.collect(source)
        has_archive = any(it.get("tier") == "Archive" and not it.get("archive_status") for it in items)
        if has_archive and options["method"] != "download_only" and not confirm_rehydrate:
            raise AppError("REHYDRATE_CONFIRM", "リハイドレートの内容を確認してください。")
        try:
            planned, _ = self.plan(items, ctx, options)
        except paths.TemplateError as exc:
            raise AppError("PATH_INVALID", exc.issues[0].message, extra={"issues": [i.to_json() for i in exc.issues]}) \
                from None
        groups = paths.find_duplicates(planned)
        skipped_dup: set[int] = set()
        if groups:
            if dup_resolution not in ("number", "skip"):
                raise AppError("PATH_DUPLICATE", f"保存するパスが重複する Blob が {sum(len(g) for g in groups.values()):,} 件"
                                                 f"（{len(groups):,} か所）あります。")
            if dup_resolution == "number":
                planned = paths.number_duplicates(planned, list(range(len(items))), groups.values())
            else:
                for g in groups.values():
                    skipped_dup.update(sorted(g)[1:])
        job_id = str(uuid.uuid4())
        now = to_iso(self.clock.now())
        start = self.settings.get("queue.start_immediately") if start is None else bool(start)
        status = "queued" if start else "held"
        name = (name or "").strip()[:200] or self.default_name(items, ctx)
        rows = []
        secrets_rows = []
        for i, it in enumerate(items):
            secret_id = it.get("secret_id")
            if it.get("sas_token"):
                secret_id = str(uuid.uuid4())
                sas = it.get("sas") or {}
                secrets_rows.append((secret_id, job_id, it["storage_account"], it["endpoint_suffix"], it["container"],
                                     it["blob_name"], dpapi.seal(it["sas_token"], "job-secret", clock=self.clock),
                                     sas.get("kind"), sas.get("permissions"), sas.get("expiry")))
            st, reason = ("skipped", "保存するパスが重複") if i in skipped_dup else ("pending", None)
            rows.append({
                "storage_account": it["storage_account"], "endpoint_suffix": it["endpoint_suffix"] or "core.windows.net",
                "container": it["container"], "blob_name": it["blob_name"], "blob_name_lower": it["blob_name"].lower(),
                "sort_key": it["sort_key"], "blob_type": it.get("blob_type"), "size": it.get("size"),
                "etag": it.get("etag"), "content_md5": it.get("content_md5"), "creation_time": it.get("creation_time"),
                "last_modified": it.get("last_modified"), "access_tier_change_time": it.get("access_tier_change_time"),
                "tier_at_search": it.get("tier"), "archive_status_at_search": it.get("archive_status"),
                "rehydrate_priority_at_search": it.get("rehydrate_priority"), "tier": it.get("tier"),
                "archive_status": it.get("archive_status"), "notes_json": it.get("notes_json"),
                "vars_json": json.dumps(it.get("vars"), ensure_ascii=False) if it.get("vars") else None,
                "auth_kind": it.get("auth_kind"), "credential_id": it.get("credential_id"), "secret_id": secret_id,
                "tenant_id": it.get("tenant_id"), "status": st, "status_reason": reason,
                "planned_path": None if i in skipped_dup else planned[i], "rehydrate_priority": None,
                "rehydrate_target_tier": None, "rehydrate_requested_known": None, "updated_at": now,
            })
        searched = ctx.get("searched_at")
        if searched and items:
            # 検索の直後なら、検索で得た情報を使って確認を省けるよう、検索の日時を残します（3.3.7）
            options["searched_at"] = searched

        def fn(c):
            order = c.execute("SELECT IFNULL(MAX(queue_order), 0) + 1 FROM jobs").fetchone()[0]
            c.execute("INSERT INTO jobs(id, name, source, search_id, source_job_id, status, queue_order, search_json, "
                      "urls_summary_json, options_json, created_at, updated_at, created_env_json) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (job_id, name, ctx["source"], ctx.get("search_id"), ctx.get("source_job_id"), status, order,
                       json.dumps(ctx["condition"], ensure_ascii=False) if ctx.get("condition") else None,
                       json.dumps(ctx["urls_summary"], ensure_ascii=False) if ctx.get("urls_summary") else None,
                       json.dumps(options, ensure_ascii=False), now, now, json.dumps(windows_env(), ensure_ascii=False)))
            if secrets_rows:
                c.executemany("INSERT INTO job_secrets(id, job_id, storage_account, endpoint_suffix, container, blob_name, "
                              "secret, sas_kind, permissions, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)", secrets_rows)
            cols = ", ".join(_ITEM_COLS)
            c.executemany(f"INSERT INTO job_items(job_id, {cols}) VALUES(?, {', '.join('?' * len(_ITEM_COLS))})",
                          [(job_id, *[r[k] for k in _ITEM_COLS]) for r in rows])
            self._recount(c, job_id)
        self.db.write(fn, timeout=300)
        if options["path_structure"] == "template" and options["path_template"]:
            from ..core import recent
            recent.add(self.db, "path_template", options["path_template"])
        log.info("ジョブを作成しました（%s 件、%s）", len(rows), STATUS_LABELS[status])
        self._publish(job_id)
        self.on_change()
        return job_id

    # ------------------------------------------------------------ 集計

    def _recount(self, c, job_id: str) -> dict:
        counts: dict[str, list[int]] = {}
        for st, n, b in c.execute("SELECT status, COUNT(*), IFNULL(SUM(size),0) FROM job_items WHERE job_id=? GROUP BY status",
                                  (job_id,)):
            counts[st] = [n, b]
        done = c.execute("SELECT IFNULL(SUM(bytes_downloaded),0) FROM job_items WHERE job_id=?", (job_id,)).fetchone()[0]
        data = {"by_status": counts, "total": sum(v[0] for v in counts.values()),
                "bytes": sum(v[1] for v in counts.values()), "bytes_downloaded": done}
        c.execute("UPDATE jobs SET counts_json=? WHERE id=?", (json.dumps(data), job_id))
        return data

    def recount(self, job_id: str) -> dict:
        return self.db.write(lambda c: self._recount(c, job_id))

    # ------------------------------------------------------------ 取得

    def _job(self, job_id: str):
        row = self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            raise NotFound("ジョブが見つかりません。")
        return row

    def public(self, row) -> dict:
        counts = json.loads(row["counts_json"]) if row["counts_json"] else {"by_status": {}, "total": 0, "bytes": 0}
        cond = json.loads(row["search_json"]) if row["search_json"] else None
        urls = json.loads(row["urls_summary_json"]) if row["urls_summary_json"] else None
        if cond:
            targets = targets_summary(cond)
        elif urls:
            accts = urls.get("accounts") or []
            targets = (accts[0] + (f" ほか{len(accts) - 1}件" if len(accts) > 1 else "")) if accts else ""
        else:
            targets = ""
        return {"id": row["id"], "name": row["name"], "source": row["source"], "status": row["status"],
                "status_label": STATUS_LABELS.get(row["status"], row["status"]), "queue_order": row["queue_order"],
                "options": json.loads(row["options_json"]), "counts": counts, "targets_summary": targets,
                "search_id": row["search_id"], "source_job_id": row["source_job_id"], "condition": cond,
                "urls_summary": urls, "job_folder": row["job_folder"], "created_at": row["created_at"],
                "started_at": row["started_at"], "updated_at": row["updated_at"], "ended_at": row["ended_at"],
                "last_error": json.loads(row["last_error_json"]) if row["last_error_json"] else None,
                "unfinished": sum((counts.get("by_status") or {}).get(s, [0, 0])[0] for s in UNFINISHED)}

    def get(self, job_id: str) -> dict:
        out = self.public(self._job(job_id))
        if out["source_job_id"]:
            src = self.db.query_one("SELECT name, created_at FROM jobs WHERE id=?", (out["source_job_id"],))
            out["source_job"] = {"id": out["source_job_id"], "name": src["name"], "created_at": src["created_at"]} if src \
                else None
        return out

    def list(self, view: str = "queue", q: str = "", result: str = "", offset: int = 0, limit: int = 200) -> dict:
        if view == "ended":
            sql, params = f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(ENDED))})", list(ENDED)
            if result in ENDED:
                sql, params = "SELECT * FROM jobs WHERE status=?", [result]
            rows = self.db.query(sql + " ORDER BY ended_at DESC", params)
        else:
            rows = self.db.query(f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(QUEUE))}) ORDER BY queue_order",
                                 list(QUEUE))
        items = [self.public(r) for r in rows]
        if q:
            words = q.lower().split()
            items = [i for i in items if all(w in f"{i['name']} {i['targets_summary']}".lower() for w in words)]
        counts = {
            "queue": self.db.scalar(f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(QUEUE))})", list(QUEUE)),
            "ended": self.db.scalar(f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(ENDED))})", list(ENDED)),
        }
        return {"total": len(items), "items": items[offset:offset + limit], "counts": counts}

    # ------------------------------------------------------------ キューの操作（3.3.4）

    def _set_status(self, job_ids: list[str], new: str, allowed: tuple[str, ...]) -> int:
        now = to_iso(self.clock.now())

        def fn(c):
            n = 0
            for jid in job_ids:
                n += c.execute(f"UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status IN ({','.join('?' * len(allowed))})",
                               (new, now, jid, *allowed)).rowcount
            return n
        n = self.db.write(fn)
        for jid in job_ids:
            self._publish(jid)
        self.on_change()
        return n

    def start(self, job_ids: list[str]) -> int:
        """未開始・一時停止のジョブを開始待ちにします。"""
        return self._set_status(job_ids, "queued", ("held", "paused"))

    def start_all(self) -> int:
        ids = [r[0] for r in self.db.query("SELECT id FROM jobs WHERE status='held'")]
        return self.start(ids)

    def pause(self, job_ids: list[str]) -> int:
        """一時停止（処理中の要求が終わりしだい止まります。engine が running を見て止めます）。"""
        return self._set_status(job_ids, "paused", ("held", "queued", "running", "waiting_rehydrate"))

    def cancel(self, job_ids: list[str]) -> int:
        """中止。未完了の Blob を中止にします（3.3.12）。実行中のものは engine が止めます。"""
        now = to_iso(self.clock.now())
        who = windows_env()["user"]

        def fn(c):
            n = 0
            for jid in job_ids:
                if c.execute(f"UPDATE jobs SET status='cancelled', ended_at=?, updated_at=?, cancelled_by=? WHERE id=? AND "
                             f"status IN ({','.join('?' * len(QUEUE))})", (now, now, who, jid, *QUEUE)).rowcount:
                    n += 1
                    c.execute(f"UPDATE job_items SET status='cancelled', updated_at=? WHERE job_id=? AND "
                              f"status IN ({','.join('?' * len(ACTIVE_ITEMS))})", (now, jid, *ACTIVE_ITEMS))
                    self._recount(c, jid)
            return n
        n = self.db.write(fn)
        for jid in job_ids:
            self._publish(jid)
        self.on_change()
        return n

    def rehydrate_requested_count(self, job_ids: list[str]) -> int:
        """中止の確認に出す、リハイドレートを要求済みの Blob の数。"""
        if not job_ids:
            return 0
        return self.db.scalar(f"SELECT COUNT(*) FROM job_items WHERE job_id IN ({','.join('?' * len(job_ids))}) AND "
                              "status='rehydrating'", job_ids)

    def move_top_and_start(self, job_id: str) -> None:
        """先頭に移動して開始（3.3.4）。"""
        self._job(job_id)

        def fn(c):
            top = c.execute(f"SELECT IFNULL(MIN(queue_order), 1) FROM jobs WHERE status IN ({','.join('?' * len(QUEUE))})",
                            list(QUEUE)).fetchone()[0]
            c.execute("UPDATE jobs SET queue_order=?, status=CASE WHEN status IN ('held','paused') THEN 'queued' ELSE status END, "
                      "updated_at=? WHERE id=?", (top - 1, to_iso(self.clock.now()), job_id))
        self.db.write(fn)
        self._publish(job_id)
        self.on_change()

    def reorder(self, job_id: str, before: str | None = None, after: str | None = None) -> None:
        """順番の変更。before（このジョブの前に置く）または after（このジョブの後に置く）を指定します。"""
        self._job(job_id)
        rows = [r for r in self.db.query(f"SELECT id, queue_order FROM jobs WHERE status IN ({','.join('?' * len(QUEUE))}) "
                                         "ORDER BY queue_order", list(QUEUE)) if r["id"] != job_id]
        ids = [r["id"] for r in rows]
        if before is not None and before in ids:
            pos = ids.index(before)
        elif after is not None and after in ids:
            pos = ids.index(after) + 1
        elif before is None and after is None:
            pos = len(ids)
        else:
            raise AppError("ORDER_INVALID", "移動先のジョブが見つかりません。")
        ids.insert(pos, job_id)

        def fn(c):
            for n, jid in enumerate(ids):
                c.execute("UPDATE jobs SET queue_order=? WHERE id=?", (float(n + 1), jid))
        self.db.write(fn)
        self.events.publish("jobs.order", {"ids": ids})
        self.on_change()

    def rename(self, job_id: str, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise AppError("NAME_REQUIRED", "ジョブ名を入力してください。")
        self._job(job_id)
        self.db.execute("UPDATE jobs SET name=?, updated_at=? WHERE id=?", (name[:200], to_iso(self.clock.now()), job_id))
        self._publish(job_id)
        return self.get(job_id)

    def update_options(self, job_id: str, changes: dict) -> dict:
        """取得の設定の変更（未開始・開始待ち・一時停止・ダウンロード待ちのとき。4.6.4）。"""
        row = self._job(job_id)
        if row["status"] not in ("held", "queued", "paused", "waiting_download"):
            raise AppError("JOB_BUSY", "実行中のジョブの取得の設定は変更できません（帯域の上限はジョブの詳細で変えられます）。",
                           status=409)
        cur = json.loads(row["options_json"])
        fixed = {k: cur[k] for k in ("path_structure", "path_template", "directory", "job_folder") if k in cur}
        new = clean_options({**cur, **(changes or {}), **fixed}, self.settings)
        new = {**cur, **new}
        self.db.execute("UPDATE jobs SET options_json=?, updated_at=? WHERE id=?",
                        (json.dumps(new, ensure_ascii=False), to_iso(self.clock.now()), job_id))
        self._publish(job_id)
        return self.get(job_id)

    def delete(self, job_ids: list[str], *, remove_parts: bool = False) -> int:
        """終了したジョブの記録を削除します（ダウンロードしたファイルは削除しません。3.3.12）。"""
        n = 0
        for jid in job_ids:
            row = self.db.query_one("SELECT status FROM jobs WHERE id=?", (jid,))
            if row is None:
                continue
            if row["status"] not in ENDED:
                raise AppError("JOB_NOT_ENDED", "キューのジョブは、先に中止してください。", status=409)
            if remove_parts:
                for r in self.db.query("SELECT part_path FROM job_items WHERE job_id=? AND part_path IS NOT NULL", (jid,)):
                    try:
                        os.remove(paths.long_path(r["part_path"]))
                    except OSError:
                        pass
            n += self.db.execute("DELETE FROM jobs WHERE id=?", (jid,))
        self.events.publish("jobs.deleted", {"ids": job_ids})
        return n

    # ------------------------------------------------------------ 起動時（3.3.4）

    def on_startup(self) -> None:
        """前回 running だったジョブを、設定に従って開始待ち・一時停止にします。途中の Blob の状態も戻します。"""
        new = "queued" if self.settings.get("queue.resume_on_startup") else "paused"
        now = to_iso(self.clock.now())

        def fn(c):
            c.execute("UPDATE jobs SET status=?, updated_at=?, lock_json=NULL, lock_heartbeat_at=NULL WHERE status='running'",
                      (new, now))
            c.execute("UPDATE job_items SET status='pending', updated_at=? WHERE status='checking'", (now,))
            c.execute("UPDATE job_items SET status='ready', updated_at=? WHERE status='downloading'", (now,))
        self.db.write(fn)

    # ------------------------------------------------------------ 補助

    def _publish(self, job_id: str) -> None:
        row = self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is not None:
            self.events.publish("job.changed", self.public(row))
        self.events.publish_coalesced("nav.counts", "jobs", {"jobs_attention": self.attention_count()})

    def attention_count(self) -> int:
        """利用者の操作を待っているジョブ（ダウンロード待ち、サインインが必要・認証情報がない）の件数（4.2）。"""
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM jobs WHERE status='waiting_download' OR (status IN ('held','queued','running',"
            "'waiting_rehydrate','paused') AND (last_error_json LIKE '%NOT_SIGNED_IN%' OR "
            "last_error_json LIKE '%INTERACTION_REQUIRED%' OR last_error_json LIKE '%NO_CREDENTIAL%'))") or 0)


def free_bytes(directory: str) -> int | None:
    """保存先の空き容量（フォルダーがなければ、あるところまで上にたどります）。"""
    p = Path(directory)
    for _ in range(64):
        try:
            return shutil.disk_usage(p).free
        except OSError:
            if p.parent == p:
                return None
            p = p.parent
    return None
