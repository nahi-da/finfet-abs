"""検索・パスパターン・既知の形式・URL で指定・候補の API（5.7.2）。"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import nullcontext
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


def _fmt_range(s, e, year: bool) -> str:
    last = e - timedelta(seconds=1)
    fmt = "%Y/%m/%d %H:%M" if year else "%m/%d %H:%M"
    a = s.strftime(fmt)
    b = last.strftime("%H:%M" if last.date() == s.date() else fmt)
    return f"{a}〜{b}"


def _fmt_ranges(ranges, gran: int) -> str:
    """区間の並びの表示（「11/20 00:00〜23:59」「11/19 15:00〜11/20 14:59」）。
    区間が 2〜3 個（複数の期間。v1.5.0）なら、それぞれを「、」で並べます。それより多い（間隔の指定など）ときは、
    全体の範囲と「ほか n 件」にします。"""
    if not ranges:
        return ""
    s, e = ranges[0][0], ranges[-1][1]
    year = s.year != (e - timedelta(seconds=1)).year
    if 1 < len(ranges) <= 3:
        return "、".join(_fmt_range(a, b, year) for a, b in ranges)
    text = _fmt_range(s, e, year)
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

    def container_ref(target: dict) -> ContainerRef:
        ref = AuthRef(target.get("auth_kind") or "entra_user", credential_id=target.get("credential_id"),
                      tenant_id=target.get("tenant"), account=target.get("account"))
        return ContainerRef(AccountRef(target.get("storage_account", ""), target.get("endpoint_suffix") or "core.windows.net",
                                       ref), target.get("container", ""))

    def held_or_fetch(target: dict, prefixes: list[str], *, max_requests: int | None,
                      progress=None) -> tuple[list, bool, float | None]:
        """prefix の一覧を、保持した一覧から（なければ取得して）返します。

        戻り値は (BlobInfo の一覧, すべて取得したか, 保持した一覧の最も古い取得の時刻)。max_requests を超えたら、
        そこまでの一覧を返します（すべて取得したか = False）。
        """
        from ..search.listing import normalize
        ct = container_ref(target)
        listing = s["search"].shared_listing()
        lkey = ct.key + s["search"].listing_extra(target, False)
        out: list = []
        complete = True
        oldest = None
        requests = 0
        page_size = int(app.settings.get("search.page_size"))
        conn = s["limits"].request if "limits" in s else nullcontext
        for p in normalize(prefixes or [""]):
            hit = listing.lookup(lkey, p)
            if hit is not None:
                out.extend(hit[0])
                oldest = hit[1] if oldest is None else min(oldest, hit[1])
                continue
            got: list = []
            token = None
            while True:
                if max_requests is not None and requests >= max_requests:
                    complete = False
                    break
                with conn():
                    page = s["backend"].list_blobs_page(ct, p, token, page_size)
                requests += 1
                got.extend(page.items)
                if progress:
                    progress(len(out) + len(got), requests)
                token = page.continuation
                if not token:
                    break
            if complete:
                listing.put(lkey, p, got)
            elif got:
                listing.put(lkey, p, got, got[-1].name)
            out.extend(got)
            if not complete:
                break
        return out, complete, oldest

    def relaxed_compile(body: dict, names: set[str], *, any_datetime: bool):
        """names の変数を任意（日時の変数は any_datetime なら任意の日時）にしてコンパイルします。"""
        variables = {k: dict(v) for k, v in (body.get("variables") or {}).items()}
        for n in names:
            v = variables.get(n) or {}
            if v.get("type") == "datetime":
                if any_datetime:
                    variables[n] = {**v, "value": {"mode": "any"}}
            else:
                variables[n] = {"type": "string", "value": {"mode": "any"}}
        try:
            return compile_path(str(body.get("path_pattern") or ""), variables, ignore_case=bool(body.get("ignore_case")),
                                input_timezone=body.get("input_timezone") or "local", ctx=s["search"].ctx())
        except PatternError:
            raise bad_request("パスパターンまたは変数の指定に誤りがあります。") from None

    def iso_or_none(t: float | None) -> str | None:
        return to_iso(datetime.fromtimestamp(t, timezone.utc)) if t else None

    @router.post("/api/v1/suggest/values")
    def suggest_values(req: Request):
        """[Azure から候補を取得]（3.2.3.1。v1.6.0 で修正）: Blob 名をパスパターンに照らし、その変数の部分を取り出します。

        その変数は任意として照合します。保持した一覧を使い、なければ最大 20 回の要求で取得します。
        """
        body = req.json()
        target = body.get("target") or {}
        name = body.get("name", "")
        tokens, issues = tokenize(str(body.get("path_pattern") or ""))
        if issues:
            raise bad_request("パスパターンに誤りがあります。")
        if not any(t.kind == "var" and t.name == name for t in tokens):
            raise bad_request("変数がパスパターンに含まれていません。")
        compiled = relaxed_compile(body, {name}, any_datetime=False)

        def run():
            items, complete, oldest = held_or_fetch(target, compiled.prefixes, max_requests=20)
            values: dict[str, None] = {}
            for b in items:
                m = compiled.match(b.name)
                if m and isinstance(m.get(name), str):
                    values[m[name]] = None
                    if len(values) >= 1000:
                        complete = False
                        break
            return {"values": sorted(values), "complete": complete, "prefixes": compiled.prefixes[:8],
                    "fetched_at": iso_or_none(oldest)}
        return task(run, "suggest-values")

    @router.post("/api/v1/pattern/scan")
    def pattern_scan(req: Request):
        """[すべての Blob を確認]（v1.6.0）: パスパターンの範囲の一覧をすべて取得し（保持した一覧を使います）、
        選択肢の変数の値と、日時の変数の Blob が存在する期間を求め直します。"""
        body = req.json()
        target = body.get("target") or {}
        names = [str(n) for n in body.get("names") or []]
        variables = body.get("variables") or {}
        compiled = relaxed_compile(body, set(names), any_datetime=True)
        holder: dict = {}

        def run():
            last = [time.monotonic()]

            def progress(n: int, requests: int) -> None:
                if time.monotonic() - last[0] >= 0.5:
                    last[0] = time.monotonic()
                    app.events.publish("infer.progress", {"id": holder.get("id"), "names": n, "requests": requests})
            items, complete, oldest = held_or_fetch(target, compiled.prefixes, max_requests=None, progress=progress)
            is_dt = {n: (variables.get(n) or {}).get("type") == "datetime" for n in names}
            choices: dict[str, dict[str, None]] = {n: {} for n in names if not is_dt[n]}
            spans: dict[str, list] = {n: [None, None] for n in names if is_dt[n]}
            count = 0
            for b in items:
                m = compiled.match(b.name)
                if m is None:
                    continue
                count += 1
                for n, vals in choices.items():
                    if isinstance(m.get(n), str):
                        vals[m[n]] = None
                for n, sp in spans.items():
                    f = m.get(n)
                    if not isinstance(f, dict) or "year" not in f:
                        continue
                    try:
                        tup = []
                        for u in dtv.UNITS:
                            if u not in f:
                                break
                            tup.append(int(f[u]))
                        t0 = dtv.unit_start(tuple(tup))
                    except (KeyError, ValueError):
                        continue
                    sp[0] = t0 if sp[0] is None or t0 < sp[0] else sp[0]
                    sp[1] = t0 if sp[1] is None or t0 > sp[1] else sp[1]
            return {"choices": {n: sorted(v) for n, v in choices.items()},
                    "found": {n: {"first": sp[0].isoformat(), "last": sp[1].isoformat(), "complete": True}
                              for n, sp in spans.items() if sp[0] is not None},
                    "matched": count, "complete": complete, "fetched_at": iso_or_none(oldest)}
        tid = s["tasks"].submit(run, "pattern-scan")
        holder["id"] = tid
        return json_response({"task_id": tid}, 202)

    @router.post("/api/v1/listing/status")
    def listing_status(req: Request):
        """保持した一覧の状態と、件数の見込み（v1.6.0。Azure への要求はしません）。

        すべての接続先で、パスパターンの範囲の一覧が保持されている場合のみ、一致する件数とサイズを返します。
        """
        from ..search.listing import normalize
        body = req.json()
        cond = body.get("condition") or {}
        targets = cond.get("targets") or []
        listing = s["search"].shared_listing()
        if not listing.enabled or not targets:
            return json_response({"enabled": listing.enabled, "held": False})
        try:
            compiled = compile_path(str(cond.get("path_pattern") or ""), cond.get("variables") or {},
                                    ignore_case=bool(cond.get("ignore_case")),
                                    input_timezone=cond.get("input_timezone") or "local", ctx=s["search"].ctx())
        except PatternError:
            return json_response({"enabled": True, "held": False})
        oldest = None
        count = 0
        total = 0
        for t in targets:
            if not t.get("container") or "{{" in str(t.get("container")):
                return json_response({"enabled": True, "held": False})
            lkey = container_ref(t).key + s["search"].listing_extra(t, False)
            at = listing.coverage(lkey, compiled.prefixes)
            if at is None:
                return json_response({"enabled": True, "held": False})
            oldest = at if oldest is None else min(oldest, at)
            seen: set[str] = set()
            for p in normalize(compiled.prefixes or [""]):
                hit = listing.lookup(lkey, p)
                for b in hit[0] if hit else []:
                    if b.name in seen:
                        continue
                    seen.add(b.name)
                    if not b.is_directory and compiled.match(b.name) is not None:
                        count += 1
                        total += b.size or 0
        return json_response({"enabled": True, "held": True, "fetched_at": iso_or_none(oldest), "count": count, "bytes": total})

    # ------------------------------------------------------------ Blob 名からパスパターンを作る（decisions.md）

    infer_cancels: dict[str, threading.Event] = {}
    infer_controls: dict[str, dict] = {}
    from ..search.infer import NameCache
    name_cache = NameCache()
    #: フルスキャンで一時停止して確認する件数（v1.6.0）
    INFER_THRESHOLD = 100_000

    @router.post("/api/v1/pattern/infer")
    def pattern_infer(req: Request):
        import queue

        from ..search import infer
        body = req.json()
        target = body.get("target") or {}
        mode = body.get("mode") or "all"
        if mode not in infer.MODES:
            raise AppError("BAD_MODE", "範囲の指定が正しくありません。")
        if not target.get("storage_account") or not target.get("container"):
            raise AppError("NO_TARGET", "接続先を選択してください。")
        try:
            limit = max(1, min(1_000_000, int(body.get("limit") or 10000)))
            per_level = max(1, min(100, int(body.get("per_level") or 10)))
        except (TypeError, ValueError):
            raise AppError("BAD_NUMBER", "件数は数値で入力してください。") from None
        prefix = str(body.get("prefix") or "").lstrip("/")
        refresh = bool(body.get("refresh"))
        cancel = threading.Event()
        stop = threading.Event()
        decisions: queue.Queue = queue.Queue()
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
            listing = s["search"].shared_listing()
            lkey = ct.key + s["search"].listing_extra(target, False)
            last = [time.monotonic()]  # 最初の知らせは 0.5 秒後（処理の ID が決まってから）

            def progress(n: int, requests: int) -> None:
                if time.monotonic() - last[0] >= 0.5:
                    last[0] = time.monotonic()
                    app.events.publish("infer.progress", {"id": holder.get("id"), "phase": "fetch", "names": n, "requests": requests})

            def on_threshold(n: int, elapsed: float) -> str:
                rate = n / elapsed if elapsed > 0 else 0
                app.events.publish("infer.progress", {"id": holder.get("id"), "phase": "paused", "names": n, "paused": True,
                                                      "elapsed_s": round(elapsed, 1), "rate": round(rate),
                                                      "estimate_1m_s": round(1_000_000 / rate) if rate else None})
                while not cancel.is_set():
                    try:
                        return decisions.get(timeout=0.5)
                    except queue.Empty:
                        continue
                return "stop"

            def store(p: str, items: list, upper: str | None = None) -> None:
                listing.put(lkey, p, items, upper)

            if refresh:
                name_cache.clear_target(tkey)
                listing.clear(lkey)
            fetched = None
            hit = None if refresh else name_cache.get(tkey, mode, prefix, limit)
            held = None if refresh or hit is not None or mode == "first" else listing.lookup(lkey, prefix)
            switched = False
            try:
                if hit is not None:
                    fetched, items, info = hit
                    info = dict(info, requests=0)
                elif held is not None:
                    items = [(b.name, b.size) for b in held[0]]
                    info = {"requests": 0, "truncated": False, "sizes_known": True, "complete": True}
                    fetched = held[1]
                else:
                    common = dict(page_size=int(app.settings.get("search.page_size")), cancel=cancel, progress=progress,
                                  connection=s["limits"].request if "limits" in s else None, store=store)
                    try:
                        items, info = infer.collect(s["backend"], ct, mode=mode, prefix=prefix, limit=limit,
                                                    per_level=per_level, stop=stop, on_threshold=on_threshold,
                                                    threshold=INFER_THRESHOLD, **common)
                    except infer.SwitchToQuick:
                        switched = True
                        app.events.publish("infer.progress", {"id": holder.get("id"), "phase": "fetch", "mode": "sample",
                                                              "switched": True, "names": 0, "requests": 0})
                        items, info = infer.collect(s["backend"], ct, mode="sample", prefix=prefix, per_level=per_level,
                                                    **common)
                    if not info.get("stopped"):
                        fetched = name_cache.put(tkey, "sample" if switched else mode, prefix, limit, items, info)
                    else:
                        fetched = time.time()
            except infer.Cancelled:
                raise AppError("CANCELLED", "中止しました。") from None
            finally:
                infer_cancels.pop(holder.get("id"), None)
                infer_controls.pop(holder.get("id"), None)
            # 生成の段階（v1.7.2。件数が多いと時間がかかるため、画面に段階を示します）
            app.events.publish("infer.progress", {"id": holder.get("id"), "phase": "generate", "names": len(items)})
            cands = infer.infer(items)
            return {"candidates": cands, "names": len(items), "requests": info["requests"],
                    "truncated": info["truncated"], "complete": info["complete"], "sizes_known": info["sizes_known"],
                    "stopped": bool(info.get("stopped")), "switched": switched,
                    "mode": "sample" if switched else mode,
                    "from_cache": hit is not None or held is not None,
                    "fetched_at": to_iso(datetime.fromtimestamp(fetched, timezone.utc))}
        tid = s["tasks"].submit(run, "pattern-infer")
        holder["id"] = tid
        infer_cancels[tid] = cancel
        infer_controls[tid] = {"stop": stop, "decisions": decisions}
        return json_response({"task_id": tid}, 202)

    @router.post("/api/v1/pattern/infer/{tid}/cancel")
    def pattern_infer_cancel(req: Request):
        ev = infer_cancels.get(req.params["tid"])
        if ev is not None:
            ev.set()
        return json_response({"ok": ev is not None})

    @router.post("/api/v1/pattern/infer/{tid}/decide")
    def pattern_infer_decide(req: Request):
        """フルスキャンの [ここまでで生成]・一時停止の選択（continue・stop・quick。v1.6.0）。"""
        action = (req.json() or {}).get("action")
        if action not in ("continue", "stop", "quick"):
            raise bad_request("操作の指定が正しくありません。")
        ctl = infer_controls.get(req.params["tid"])
        if ctl is not None:
            if action == "stop":
                ctl["stop"].set()
            ctl["decisions"].put(action)
        return json_response({"ok": ctl is not None})

    # ------------------------------------------------------------ 検索

    @router.post("/api/v1/searches")
    def start_search(req: Request):
        body = req.json()
        try:
            cond = clean_condition(body.get("condition") or body)
        except ValueError as exc:
            raise bad_request(str(exc), "CONDITION_INVALID") from None
        sid = s["search"].start(cond, refresh=bool((body or {}).get("refresh")))
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
            raise NotFound("検索結果が見つかりません（古い検索結果は自動で削除されます）。")
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
