"""検索履歴・エクスポート・インポートの API（5.7.2、3.5）。"""

from __future__ import annotations

import base64
import json
import re

from ..core.errors import AppError, bad_request
from ..history.condition import clean_condition
from ..history.exchange import commit_credentials, credential_counts, export_file, parse_import, preview_json
from ..server.http import Request, download_response, json_response


def _safe_name(name: str) -> str:
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return n[:80] or "検索条件"


def register(router, app) -> None:
    s = app.services
    if "history" not in s:
        return
    hist = s["history"]

    @router.get("/api/v1/history")
    def list_history(req: Request):
        return json_response(hist.list(req.arg("q") or "", req.int_arg("offset", 0, 0), req.int_arg("limit", 200, 1, 2000),
                                       pinned_and_recent=req.arg("menu") == "1"))

    @router.get("/api/v1/history/{hid}")
    def get_history(req: Request):
        return json_response(hist.get(req.params["hid"]))

    @router.patch("/api/v1/history/{hid}")
    def patch_history(req: Request):
        body = req.json()
        if "pinned" not in body:
            raise bad_request("pinned を指定してください。")
        return json_response(hist.pin(req.params["hid"], bool(body["pinned"])))

    @router.delete("/api/v1/history/{hid}")
    def delete_history(req: Request):
        return json_response({"deleted": hist.delete([req.params["hid"]])})

    @router.post("/api/v1/history/delete")
    def delete_many(req: Request):
        return json_response({"deleted": hist.delete(list(req.json().get("ids") or []))})

    # ------------------------------------------------------------ エクスポート（3.5.4.1）

    def conditions_from(body: dict) -> list[dict]:
        if body.get("history_ids"):
            return [hist.get(h)["condition"] for h in body["history_ids"]]
        if body.get("condition"):
            try:
                return [clean_condition(body["condition"])]
            except ValueError as exc:
                raise bad_request(str(exc)) from None
        raise bad_request("エクスポートする検索条件を指定してください。")

    @router.post("/api/v1/history/export/counts")
    def export_counts(req: Request):
        return json_response(credential_counts(conditions_from(req.json()), s["credstore"]))

    @router.post("/api/v1/history/export")
    def export(req: Request):
        body = req.json()
        conds = conditions_from(body)
        data, counts = export_file(conds, body.get("includes", "targets"), s["credstore"], clock=app.clock)
        name = conds[0].get("name") if len(conds) == 1 and conds[0].get("name") else (
            "検索条件" if len(conds) == 1 else f"検索条件 {len(conds)} 件")
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        resp = download_response(text.encode("utf-8"), f"{_safe_name(name)}.finfet-abs.json", "application/json; charset=utf-8")
        resp.set_header("X-FinFeT-Credentials", str(counts["included"]))
        return resp

    # ------------------------------------------------------------ インポート（3.5.4.2）

    @router.post("/api/v1/history/import/preview")
    def import_preview(req: Request):
        body = req.json()
        try:
            raw = base64.b64decode(body.get("content_base64") or "", validate=True)
        except ValueError:
            raise bad_request("ファイルを読み込めませんでした。") from None
        parsed = parse_import(raw, s["search"].ctx())
        token = s["import_sessions"].put(parsed)
        return json_response({"token": token, **preview_json(parsed)})

    @router.post("/api/v1/history/import/commit")
    def import_commit(req: Request):
        body = req.json()
        token = body.get("token", "")
        parsed = s["import_sessions"].get(token)
        mode = body.get("mode", "history")
        save = bool(body.get("save_credentials", True))
        if save and parsed["credentials"]:
            conds, conflicts, errors = commit_credentials(
                parsed, s["credstore"], decisions={k: bool(v) for k, v in (body.get("decisions") or {}).items()},
                allow_development=bool(app.settings.get("dev.allow_development_storage")))
            if conflicts:
                return json_response({"conflicts": conflicts}, 409)
        else:
            conds = []
            for it in parsed["items"]:
                if "condition" in it:
                    c = json.loads(json.dumps(it["condition"]))
                    for t in c["targets"]:
                        t.pop("credential", None)
                    conds.append(c)
            errors = []
        valid = [c for c, it in zip(conds, [i for i in parsed["items"] if "condition" in i]) if not it.get("error")]
        if mode == "load":
            if len(conds) != 1:
                raise AppError("IMPORT_LOAD_ONE", "検索ページに読み込めるのは、検索条件が 1 件のファイルのみです。")
            s["import_sessions"].drop(token)
            return json_response({"condition": conds[0], "credential_errors": errors,
                                  "had_credentials": parsed["credentials"] > 0})
        added = 0
        for c in valid:
            hist.record(c)
            added += 1
        s["import_sessions"].drop(token)
        return json_response({"added": added, "credential_errors": errors, "had_credentials": parsed["credentials"] > 0})
