"""ジョブの API（5.7.2、3.3、4.4.7、4.5、4.6）。"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..core.errors import AppError, NotFound, bad_request
from ..jobs import items as items_mod
from ..jobs import paths
from ..server.http import Request, download_response, json_response


def _ids(body: dict) -> list:
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise bad_request("ids を指定してください。")
    return ids


def _item_filter(req: Request) -> dict:
    return {"q": req.arg("q", ""), "status": [x for x in (req.arg("status") or "").split(",") if x],
            "tier": [x for x in (req.arg("tier") or "").split(",") if x]}


def register(router, app) -> None:
    s = app.services
    if "jobs" not in s:
        return
    jobs = s["jobs"]
    engine = s["job_engine"]

    # ------------------------------------------------------------ 作成（4.4.7）

    @router.post("/api/v1/jobs/preview")
    def preview(req: Request):
        return json_response(jobs.preview(req.json().get("source") or {}))

    @router.post("/api/v1/jobs/path-check")
    def path_check(req: Request):
        body = req.json()
        return json_response(jobs.path_check(body.get("source") or {}, body.get("options") or {}))

    @router.post("/api/v1/jobs/path-check/groups")
    def path_groups(req: Request):
        body = req.json()
        return json_response(jobs.path_groups(body.get("source") or {}, body.get("options") or {},
                                              req.int_arg("offset", 0, 0), req.int_arg("limit", 200, 1, 1000)))

    @router.post("/api/v1/jobs")
    def create(req: Request):
        body = req.json()
        jid = jobs.create(body.get("source") or {}, body.get("options") or {}, name=body.get("name"),
                          start=body.get("start"), dup_resolution=body.get("dup_resolution"),
                          confirm_rehydrate=bool(body.get("confirm_rehydrate")))
        if body.get("start") is not None:
            app.settings.update({"queue.start_immediately": bool(body["start"])})
        return json_response({"id": jid}, 201)

    # ------------------------------------------------------------ 一覧・詳細

    @router.get("/api/v1/jobs")
    def list_jobs(req: Request):
        out = jobs.list(req.arg("view") or "queue", req.arg("q") or "", req.arg("result") or "",
                        req.int_arg("offset", 0, 0), req.int_arg("limit", 200, 1, 2000))
        running = set(engine.running_ids())
        for j in out["items"]:
            j["active"] = j["id"] in running
        return json_response(out)

    @router.get("/api/v1/jobs/bandwidth")
    def get_bandwidth(req: Request):
        lim = s["limits"]
        return json_response({"mbps": lim.bandwidth_mbps(), "speed": engine.speed.rate(), "history": engine.speed.history()})

    @router.put("/api/v1/jobs/bandwidth")
    def put_bandwidth(req: Request):
        try:
            v = float(req.json().get("mbps"))
        except (TypeError, ValueError):
            raise bad_request("帯域の上限を数値で指定してください。") from None
        if not 0 <= v <= 10000:
            raise bad_request("帯域の上限は 0〜10,000 MB/s にしてください。")
        s["limits"].set_bandwidth_mbps(v)
        app.events.publish("bandwidth.changed", {"mbps": v})
        return json_response({"mbps": v})

    @router.get("/api/v1/jobs/{jid}")
    def get_job(req: Request):
        out = jobs.get(req.params["jid"])
        out["active"] = req.params["jid"] in engine.running_ids()
        nxt = app.db.scalar("SELECT MIN(next_check_at) FROM job_items WHERE job_id=? AND status='rehydrating'",
                            (req.params["jid"],))
        out["next_check_at"] = nxt
        out["rehydrating_requested"] = jobs.rehydrate_requested_count([req.params["jid"]])
        out["log_dir"] = str(app.location.job_logs_dir)
        return json_response(out)

    @router.patch("/api/v1/jobs/{jid}")
    def patch_job(req: Request):
        body = req.json()
        out = None
        if "name" in body:
            out = jobs.rename(req.params["jid"], body["name"])
        if "options" in body:
            out = jobs.update_options(req.params["jid"], body["options"] or {})
        if out is None:
            raise bad_request("変更する項目を指定してください。")
        return json_response(out)

    # ------------------------------------------------------------ キューの操作（3.3.4）

    @router.post("/api/v1/jobs/actions/{action}")
    def bulk_action(req: Request):
        action = req.params["action"]
        body = req.json()
        if action == "start-all":
            return json_response({"changed": jobs.start_all()})
        ids = _ids(body)
        if action == "start":
            return json_response({"changed": jobs.start(ids)})
        if action == "pause":
            return json_response({"changed": jobs.pause(ids)})
        if action == "cancel":
            n = jobs.rehydrate_requested_count(ids)
            if n and not body.get("confirm"):
                raise AppError("CANCEL_CONFIRM", "リハイドレートを要求済みの Blob があります。", status=409,
                               extra={"rehydrating": n})
            return json_response({"changed": jobs.cancel(ids)})
        if action == "delete":
            return json_response({"deleted": jobs.delete(ids, remove_parts=bool(body.get("remove_parts")))})
        raise NotFound("操作が見つかりません。")

    @router.put("/api/v1/jobs/queue-order")
    def queue_order(req: Request):
        body = req.json()
        jobs.reorder(body.get("id") or "", before=body.get("before"), after=body.get("after"))
        return json_response({"ok": True})

    @router.post("/api/v1/jobs/{jid}/actions/{action}")
    def job_action(req: Request):
        jid, action = req.params["jid"], req.params["action"]
        body = req.json()
        if action in ("start", "pause"):
            n = (jobs.start if action == "start" else jobs.pause)([jid])
            return json_response({"changed": n})
        if action == "cancel":
            n = jobs.rehydrate_requested_count([jid])
            if n and not body.get("confirm"):
                raise AppError("CANCEL_CONFIRM", "リハイドレートを要求済みの Blob があります。", status=409,
                               extra={"rehydrating": n})
            return json_response({"changed": jobs.cancel([jid])})
        if action == "move-top-and-start":
            jobs.move_top_and_start(jid)
            return json_response({"ok": True})
        if action == "check-now":
            return json_response({"checked": engine.check_now(jid)})
        if action == "start-download":
            engine.start_download(jid, mode=body.get("mode") or "all", options=body.get("options"))
            return json_response({"ok": True})
        if action == "resume":
            return json_response({"resumed": engine.resume(jid)})
        if action == "rerun-all":
            new = jobs.create({"kind": "job", "job_id": jid}, body.get("options") or jobs.get(jid)["options"],
                              name=body.get("name"), start=body.get("start"), dup_resolution=body.get("dup_resolution"),
                              confirm_rehydrate=bool(body.get("confirm_rehydrate")))
            return json_response({"id": new}, 201)
        if action == "delete":
            return json_response({"deleted": jobs.delete([jid], remove_parts=bool(body.get("remove_parts")))})
        raise NotFound("操作が見つかりません。")

    # ------------------------------------------------------------ Blob（4.6.2）

    @router.get("/api/v1/jobs/{jid}/items")
    def list_items(req: Request):
        jobs.get(req.params["jid"])
        runner = engine._runners.get(req.params["jid"])
        progress = dict(runner._progress) if runner is not None else {}
        return json_response(items_mod.list_items(app.db, req.params["jid"], _item_filter(req), req.arg("sort") or "name",
                                                  req.int_arg("offset", 0, 0), req.int_arg("limit", 200, 1, 500), progress))

    @router.get("/api/v1/jobs/{jid}/items.csv")
    def items_csv(req: Request):
        j = jobs.get(req.params["jid"])
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", j["name"]).strip(" .")[:80] or "ジョブ"
        return download_response(items_mod.to_csv(app.db, req.params["jid"], _item_filter(req)), f"{name}.csv",
                                 "text/csv; charset=utf-8")

    @router.get("/api/v1/jobs/{jid}/items/{item_id}")
    def item_detail(req: Request):
        return json_response(items_mod.detail(app.db, req.params["jid"], int(req.params["item_id"])))

    @router.post("/api/v1/jobs/{jid}/items/actions/{action}")
    def items_action(req: Request):
        jid, action = req.params["jid"], req.params["action"]
        ids = _ids(req.json())
        if action in ("retry", "re-request"):
            return json_response({"changed": engine.retry_items(jid, ids, action)})
        if action == "raise-priority":
            return json_response(engine.raise_priority(jid, ids))
        if action == "rerun-selected":
            body = req.json()
            new = jobs.create({"kind": "job", "job_id": jid, "item_ids": ids}, body.get("options") or jobs.get(jid)["options"],
                              start=body.get("start"), dup_resolution=body.get("dup_resolution"),
                              confirm_rehydrate=bool(body.get("confirm_rehydrate")))
            return json_response({"id": new}, 201)
        raise NotFound("操作が見つかりません。")

    @router.post("/api/v1/jobs/{jid}/items/{item_id}/actions/{action}")
    def item_action(req: Request):
        jid, action = req.params["jid"], req.params["action"]
        it = items_mod.detail(app.db, jid, int(req.params["item_id"]))
        job = jobs.get(jid)
        root = job.get("job_folder")
        target = it.get("local_path")
        if not target or not root or not paths.inside(root, os.path.relpath(target, root)):
            raise AppError("FILE_NOT_FOUND", "ファイルがありません。", status=404)
        long = paths.long_path(target)
        if action == "open-file":
            if it["status"] != "completed" or not os.path.isfile(long):
                raise AppError("FILE_NOT_FOUND", "ファイルがありません。", status=404)
            os.startfile(long)  # noqa: S606（ジョブで保存したファイルだけを開く）
            return json_response({"ok": True})
        if action == "open-folder":
            folder = Path(target).parent
            if not os.path.isdir(paths.long_path(folder)):
                raise AppError("FILE_NOT_FOUND", "フォルダーがありません。", status=404)
            os.startfile(paths.long_path(folder))  # noqa: S606（フォルダーだけを開く）
            return json_response({"ok": True})
        raise NotFound("操作が見つかりません。")

    @router.get("/api/v1/jobs/{jid}/log")
    def job_log(req: Request):
        jobs.get(req.params["jid"])
        path = app.location.job_logs_dir / f"{req.params['jid']}.log"
        return json_response({"lines": items_mod.read_log(path, req.int_arg("tail", 2000, 1, 20000), req.arg("blob"),
                                                          req.arg("level"))})
