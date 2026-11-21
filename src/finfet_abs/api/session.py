"""セッション・イベント・終了の API（5.7.2）。"""

from __future__ import annotations

import threading

from ..core.settings import DEFAULT_CLIENT_ID
from ..server.http import Request, json_response
from ..version import APP_NAME, __version__


def register(router, app) -> None:
    @router.get("/api/v1/session")
    def session(req: Request):
        loc = app.location
        warnings = []
        if app.settings.load_warning:
            warnings.append(app.settings.load_warning)
        return json_response({
            "csrf": app.security.csrf_token,
            "app_name": APP_NAME,
            "version": __version__,
            "data_dir": str(loc.data_dir),
            "mode": loc.mode,
            "mode_label": loc.mode_label,
            "demo": app.demo,
            "start_page": app.settings.get("ui.start_page"),
            "default_client_id": app.settings.get("auth.client_id") == DEFAULT_CLIENT_ID,
            "acl_warning": loc.mode == "portable" and loc.acl_ok is False,
            "network_data_dir": loc.is_network,
            "warnings": warnings,
            "events_last": app.events.last,
        })

    @router.get("/api/v1/events")
    def events(req: Request):
        after = req.arg("after")
        if after is None or after == "":
            return json_response({"last": app.events.last, "events": []})
        try:
            n = int(after)
        except ValueError:
            n = -1
        return json_response(app.events.wait(n, 25.0))

    @router.post("/api/v1/app/shutdown")
    def shutdown(req: Request):
        body = req.json()
        jobs = app.services.get("jobs")
        if jobs is not None and not body.get("confirm"):
            running = jobs.running_job_names()
            if running:
                return json_response({"confirm_required": True, "running": running}, 409)
        threading.Timer(0.3, app.request_shutdown).start()
        return json_response({"ok": True})
