"""設定の API（5.7.2）。"""

from __future__ import annotations

from ..core.errors import bad_request
from ..core.settings import SCHEMA
from ..server.http import Request, json_response


def _schema() -> list[dict]:
    out = []
    for f in SCHEMA:
        item = {"key": f.key, "kind": f.kind}
        if f.min is not None:
            item["min"] = f.min
        if f.max is not None:
            item["max"] = f.max
        if f.choices:
            item["choices"] = list(f.choices)
        if f.kind != "secret":
            item["default"] = f.default
        out.append(item)
    return out


def register(router, app) -> None:
    @router.get("/api/v1/settings")
    def get_settings(req: Request):
        return json_response({"values": app.settings.public(), "schema": _schema(),
                              "data_dir": str(app.location.data_dir), "mode_label": app.location.mode_label})

    @router.put("/api/v1/settings")
    def put_settings(req: Request):
        body = req.json()
        changes = body.get("changes")
        if not isinstance(changes, dict):
            raise bad_request("changes を指定してください。")
        changed = app.settings.update(changes)
        return json_response({"values": app.settings.public(), "changed": changed})

    @router.post("/api/v1/settings/reset")
    def reset(req: Request):
        changed = app.settings.reset()
        return json_response({"values": app.settings.public(), "changed": changed})
