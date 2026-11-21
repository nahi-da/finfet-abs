"""検索・パスパターン・既知の形式・URL で指定・候補の API（5.7.2）。"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from ..azure.backend import AccountRef, AuthRef, ContainerRef
from ..azure.sas import ParseError, split_blob_url
from ..core import recent
from ..core.clock import to_iso
from ..core.errors import AppError, NotFound, bad_request
from ..history.condition import clean_condition, targets_summary, variables_summary
from ..search import datetime_vars as dtv
from ..search import detect, formats
from ..search.pattern import PatternError, compile_path, tokenize
from ..search.prefix import compute as compute_prefix
from ..search.results import filters_from_request
from ..search.urls import parse_urls
from ..server.http import Request, Response, download_response, json_response

log = logging.getLogger(__name__)


def _fmt_ranges(ranges, gran: int) -> str:
    """区間の並びの表示（「11/20 00:00〜23:59」「11/19 15:00〜11/20 14:59」）。"""
    if not ranges:
        return ""
    s, e = ranges[0][0], ranges[-1][1]
    last = e - timedelta(seconds=1)
    year = s.year != last.year
    fmt = "%Y/%m/%d %H:%M" if year else "%m/%d %H:%M"
    a = s.strftime(fmt)
    b = last.strftime("%H:%M" if last.date() == s.date() else fmt)
    text = f"{a}〜{b}"
    if len(ranges) > 1:
        text += f" ほか {len(ranges) - 1} 件"
    return text


def register(router, app) -> None:
    s = app.services
    if "search" not in s:
        return

    def task(fn, kind: str):
        return json_response({"task_id": s["tasks"].submit(fn, kind)}, 202)

    # ------------------------------------------------------------ パスパターンの解析（通信なし）

    @router.post("/api/v1/pattern/analyze")
    def analyze(req: Request):
        body = req.json()
        pattern = str(body.get("path_pattern") or "")
        containers = [str(c) for c in body.get("containers") or []]
        variables = body.get("variables") or {}
        det = detect.detect(pattern, containers, variables)
        defs = {v["name"]: v["definition"] for v in det["variables"] if v["in_pattern"]}
        issues = list(det["issues"])
        prefixes: list[str] = []
        datetimes = []
        ctx = s["search"].ctx()
        if pattern and not issues:
            try:
                c = compile_path(pattern, defs, ignore_case=bool(body.get("ignore_case")),
                                 input_timezone=body.get("input_timezone") or "local", ctx=ctx)
                prefixes = c.prefixes
                for name, info in c.datetimes.items():
                    datetimes.append({"name": name, "mode": info.mode, "path_timezone": info.path_timezone,
                                      "input": _fmt_ranges(info.input_ranges, info.granularity),
                                      "path": _fmt_ranges(info.path_ranges, info.granularity),
                                      "same_zone": (body.get("input_timezone") or "local") == info.path_timezone,
                                      "ordered": info.ordered})
            except PatternError as exc:
                issues += [i.to_json() for i in exc.issues]
        url_hint = None
        if pattern.startswith("https://"):
            try:
                acct, suffix, cont, blob, query, _dfs = split_blob_url(pattern)
                url_hint = {"storage_account": acct, "endpoint_suffix": suffix, "container": cont, "path": blob or "",
                            "has_sas": "sig=" in query}
            except ParseError:
                url_hint = None
        return json_response({
            "variables": det["variables"], "issues": issues, "merge": det["merge"], "prefixes": prefixes,
            "datetimes": datetimes,
            "literal_hint": detect.escape_non_variables(pattern) if url_hint is None and detect.looks_literal(pattern) else None,
            "url_hint": url_hint,
            "formats": [{"container": cn, **fm} for cn in containers if (fm := formats.suggest(cn))],
        })

    @router.get("/api/v1/known-formats")
    def known_formats(req: Request):
        return json_response({"formats": formats.public_list()})

    @router.post("/api/v1/known-formats/{fid}/build")
    def known_build(req: Request):
        try:
            return json_response(formats.build(req.params["fid"], req.json().get("inputs") or {}))
        except formats.FormatInputError as exc:
            raise AppError("FORMAT_INPUT", str(exc), extra={"field": exc.field}) from None

    @router.post("/api/v1/known-formats/suggest-inputs")
    def known_suggest_inputs(req: Request):
        body = req.json()
        return json_response({"inputs": formats.suggest_inputs(body.get("id", ""), body.get("container", ""))})

    # ------------------------------------------------------------ URL で指定

    @router.post("/api/v1/urls/parse")
    def urls_parse(req: Request):
        try:
            rows, public = parse_urls(req.json().get("text", ""), app.clock.now())
        except ParseError as exc:
            raise bad_request(str(exc), "URLS_INVALID") from None
        token = s["url_sessions"].put(rows)
        accounts = {}
        for r in rows:
            if "error" in r or "sas" in r or r.get("duplicate"):
                continue
            key = (r["storage_account"], r["endpoint_suffix"])
            if key not in accounts:
                creds = s["credstore"].usable_for(r["storage_account"], r["endpoint_suffix"], None)
                accounts[key] = {"storage_account": key[0], "endpoint_suffix": key[1],
                                 "credentials": [c for c in creds if c["usable"]], "containers": set()}
            accounts[key]["containers"].add(r["container"])
        out_accounts = []
        for a in accounts.values():
            a["containers"] = sorted(a["containers"])
            out_accounts.append(a)
        signed = bool(s.get("auth") and s["auth"].signed_in)
        return json_response({"token": token, "rows": public, "accounts_without_sas": out_accounts, "signed_in": signed,
                              "count": sum(1 for r in rows if "error" not in r and not r.get("duplicate")),
                              "errors": sum(1 for r in rows if "error" in r),
                              "duplicates": sum(1 for r in rows if r.get("duplicate"))})

    # ------------------------------------------------------------ 候補

    @router.post("/api/v1/suggest/containers")
    def suggest_containers(req: Request):
        body = req.json()
        account = body.get("storage_account", "")
        suffix = body.get("endpoint_suffix") or "core.windows.net"
        recent_names = recent.get(app.db, "container", account)

        def run():
            names: list[str] = []
            err = None
            try:
                ref = AuthRef(body.get("auth_kind") or "entra_user", credential_id=body.get("credential_id"),
                              tenant_id=body.get("tenant"), account=body.get("account"))
                names = s["backend"].list_containers(AccountRef(account, suffix, ref))
            except Exception as exc:  # noqa: BLE001
                err = getattr(exc, "message", None) or "コンテナーの一覧を取得できません。"
            return {"recent": recent_names, "containers": names, "error": err}
        return task(run, "suggest-containers")

    @router.post("/api/v1/suggest/values")
    def suggest_values(req: Request):
        """[Azure から候補を取得]（3.2.3.1）: その変数より前の部分を固定し、その階層の名前の一覧を取得します。"""
        body = req.json()
        target = body.get("target") or {}
        name = body.get("name", "")
        pattern = str(body.get("path_pattern") or "")
        variables = body.get("variables") or {}
        tokens, issues = tokenize(pattern)
        if issues:
            raise bad_request("パスパターンに誤りがあります。")
        idx = next((i for i, t in enumerate(tokens) if t.kind == "var" and t.name == name), None)
        if idx is None:
            raise bad_request("変数がパスパターンにありません。")
        before = "".join(t.text for t in tokens[:idx])
        try:
            part = compile_path(before + "(?:.*)", variables, input_timezone=body.get("input_timezone") or "local",
                                ctx=s["search"].ctx())
            prefixes = part.prefixes[:8]
        except PatternError:
            prefixes = [compute_prefix(before)[0] if before else ""]

        def run():
            ref = AuthRef(target.get("auth_kind") or "entra_user", credential_id=target.get("credential_id"),
                          tenant_id=target.get("tenant"), account=target.get("account"))
            ct = ContainerRef(AccountRef(target.get("storage_account", ""), target.get("endpoint_suffix") or "core.windows.net",
                                         ref), target.get("container", ""))
            values: list[str] = []
            for p in prefixes:
                for item in s["backend"].walk_prefixes(ct, p, 1000):
                    v = item.rstrip("/")
                    if v and "/" not in v and v not in values:
                        values.append(v)
                if len(values) >= 1000:
                    break
            return {"values": values[:1000], "prefixes": prefixes}
        return task(run, "suggest-values")

    # ------------------------------------------------------------ Blob 名からパスパターンを作る（decisions.md）

    infer_cancels: dict[str, threading.Event] = {}
    from ..search.infer import NameCache
    name_cache = NameCache()

    @router.post("/api/v1/pattern/infer")
    def pattern_infer(req: Request):
        from ..search import infer
        body = req.json()
        target = body.get("target") or {}
        mode = body.get("mode") or "sample"
        if mode not in infer.MODES:
            raise AppError("BAD_MODE", "範囲の指定が正しくありません。")
        if not target.get("storage_account") or not target.get("container"):
            raise AppError("NO_TARGET", "接続先を選んでください。")
        try:
            limit = max(1, min(1_000_000, int(body.get("limit") or 10000)))
            per_level = max(1, min(100, int(body.get("per_level") or 10)))
        except (TypeError, ValueError):
            raise AppError("BAD_NUMBER", "件数は数で入力してください。") from None
        prefix = str(body.get("prefix") or "").lstrip("/")
        refresh = bool(body.get("refresh"))
        cancel = threading.Event()
        holder: dict = {}
        # 集めた名前の保持のキー（接続先と認証）。秘密情報は含めません
        tkey = (target["storage_account"].lower(), target.get("endpoint_suffix") or "core.windows.net", target["container"],
                target.get("auth_kind") or "entra_user", target.get("credential_id"), target.get("tenant"),
                (target.get("account") or "").lower())

        def run():
            ref = AuthRef(target.get("auth_kind") or "entra_user", credential_id=target.get("credential_id"),
                          tenant_id=target.get("tenant"), account=target.get("account"))
            ct = ContainerRef(AccountRef(target["storage_account"], target.get("endpoint_suffix") or "core.windows.net", ref),
                              target["container"])
            last = [time.monotonic()]  # 最初の知らせは 0.5 秒後（処理の ID が決まってから）

            def progress(n: int, requests: int) -> None:
                if time.monotonic() - last[0] >= 0.5:
                    last[0] = time.monotonic()
                    app.events.publish("infer.progress", {"id": holder.get("id"), "names": n, "requests": requests})
            if refresh:
                name_cache.clear_target(tkey)
            hit = None if refresh else name_cache.get(tkey, mode, prefix, limit)
            try:
                if hit is not None:
                    fetched, items, info = hit
                    info = dict(info, requests=0)
                else:
                    items, info = infer.collect(s["backend"], ct, mode=mode, prefix=prefix, limit=limit, per_level=per_level,
                                                cancel=cancel, progress=progress, connection=s["limits"].request
                                                if "limits" in s else None)
                    fetched = name_cache.put(tkey, mode, prefix, limit, items, info)
            except infer.Cancelled:
                raise AppError("CANCELLED", "中止しました。") from None
            finally:
                infer_cancels.pop(holder.get("id"), None)
            cands = infer.infer(items)
            return {"candidates": cands, "names": len(items), "requests": info["requests"],
                    "truncated": info["truncated"], "complete": info["complete"], "sizes_known": info["sizes_known"],
                    "from_cache": hit is not None, "fetched_at": to_iso(datetime.fromtimestamp(fetched, timezone.utc))}
        tid = s["tasks"].submit(run, "pattern-infer")
        holder["id"] = tid
        infer_cancels[tid] = cancel
        return json_response({"task_id": tid}, 202)

    @router.post("/api/v1/pattern/infer/{tid}/cancel")
    def pattern_infer_cancel(req: Request):
        ev = infer_cancels.get(req.params["tid"])
        if ev is not None:
            ev.set()
        return json_response({"ok": ev is not None})

    # ------------------------------------------------------------ 検索

    @router.post("/api/v1/searches")
    def start_search(req: Request):
        body = req.json()
        try:
            cond = clean_condition(body.get("condition") or body)
        except ValueError as exc:
            raise bad_request(str(exc), "CONDITION_INVALID") from None
        sid = s["search"].start(cond)
        for t in cond["targets"]:
            recent.add(app.db, "storage_account", t["storage_account"])
            if "{{" not in t["container"]:
                recent.add(app.db, "container", t["container"], t["storage_account"])
        return json_response({"search_id": sid}, 201)

    @router.get("/api/v1/searches")
    def list_searches(req: Request):
        rows = app.db.query("SELECT * FROM searches ORDER BY started_at DESC LIMIT 50")
        items = []
        for r in rows:
            cond = json.loads(r["condition_json"])
            items.append({"id": r["id"], "status": r["status"], "started_at": r["started_at"],
                          "matched_count": r["matched_count"], "targets_summary": targets_summary(cond),
                          "path_pattern": cond.get("path_pattern", ""), "variables_summary": variables_summary(cond)})
        return json_response({"total": len(items), "items": items})

    def search_info(sid: str) -> dict:
        r = app.db.query_one("SELECT * FROM searches WHERE id=?", (sid,))
        if r is None:
            raise NotFound("検索結果が見つかりません（古い検索結果は削除されます）。")
        cond = json.loads(r["condition_json"])
        targets = [{"no": t["target_no"], "storage_account": t["storage_account"], "endpoint_suffix": t["endpoint_suffix"],
                    "container": t["container"], "status": t["status"], "listed": t["listed_count"],
                    "matched": t["matched_count"], "requests": t["request_count"], "elapsed_ms": t["elapsed_ms"],
                    "error": json.loads(t["error_json"]) if t["error_json"] else None}
                   for t in app.db.query("SELECT * FROM search_targets WHERE search_id=? ORDER BY target_no", (sid,))]
        return {"id": sid, "status": "running" if s["search"].running(sid) else r["status"], "started_at": r["started_at"],
                "ended_at": r["ended_at"], "matched_count": r["matched_count"], "matched_bytes": r["matched_bytes"],
                "truncated": bool(r["truncated"]), "elapsed_ms": r["elapsed_ms"], "condition": cond,
                "targets_summary": targets_summary(cond), "variables_summary": variables_summary(cond), "targets": targets}

    @router.get("/api/v1/searches/{sid}")
    def get_search(req: Request):
        return json_response(search_info(req.params["sid"]))

    @router.get("/api/v1/searches/{sid}/results")
    def results(req: Request):
        f = filters_from_request(req)
        display = req.arg("display") or app.settings.get("ui.blob_name_display")
        return json_response(s["results"].list(req.params["sid"], f, req.arg("sort") or "name",
                                               req.int_arg("offset", 0, 0), req.int_arg("limit", 200, 1, 500), display))

    @router.get("/api/v1/searches/{sid}/summary")
    def summary(req: Request):
        return json_response(s["results"].summary(req.params["sid"], filters_from_request(req)))

    @router.post("/api/v1/searches/{sid}/results/select")
    def select(req: Request):
        return json_response(s["results"].select(req.params["sid"], req.json()))

    @router.post("/api/v1/searches/{sid}/cancel")
    def cancel(req: Request):
        s["search"].cancel(req.params["sid"])
        return json_response({"ok": True})

    @router.post("/api/v1/searches/{sid}/targets/{no}/retry")
    def retry(req: Request):
        s["search"].retry_target(req.params["sid"], int(req.params["no"]))
        return json_response({"ok": True})

    @router.post("/api/v1/searches/{sid}/targets/{no}/skip")
    def skip(req: Request):
        s["search"].skip_target(req.params["sid"], int(req.params["no"]))
        return json_response({"ok": True})

    @router.get("/api/v1/searches/{sid}/results.csv")
    def results_csv(req: Request) -> Response:
        data = s["results"].csv(req.params["sid"], filters_from_request(req), req.arg("selected") == "1",
                                req.arg("sort") or "name")
        return download_response(data, f"search-{req.params['sid'][:8]}.csv", "text/csv; charset=utf-8")
