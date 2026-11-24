"""セッション・イベント・終了の API（5.7.2）。"""

from __future__ import annotations

import threading
from collections import deque

from ..core.settings import DEFAULT_CLIENT_ID
from ..server.http import Request, json_response
from ..version import APP_NAME, __version__


MAX_NOTICES = 50


def register(router, app) -> None:
    # お知らせ（4.3.5）: 起動している間だけ、最近の 50 件を覚えます（再読み込み・別のタブでも同じものを出すため）
    notices: deque = deque(maxlen=MAX_NOTICES)
    read = {"seq": 0}

    def on_event(ev: dict) -> None:
        if ev["type"] == "notify":
            notices.appendleft({"seq": ev["seq"], "at": ev["at"], **ev["data"]})
    app.events.subscribe(on_event)

    @router.post("/api/v1/notices/read")
    def notices_read(req: Request):
        read["seq"] = max(read["seq"], int((req.json() or {}).get("seq") or app.events.last))
        return json_response({"read_seq": read["seq"]})

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
            "tutorial_offered": bool(app.settings.get("tutorial.offered")),
            "start_page": app.settings.get("ui.start_page"),
            "default_client_id": app.settings.get("auth.client_id") == DEFAULT_CLIENT_ID,
            "acl_warning": loc.mode == "portable" and loc.acl_ok is False,
            "network_data_dir": loc.is_network,
            "warnings": warnings,
            "events_last": app.events.last,
            "notices": list(notices),
            "notices_read_seq": read["seq"],
            "jobs_attention": app.services["jobs"].attention_count() if "jobs" in (app.services or {}) else 0,
        })

    @router.get("/api/v1/events")
    def events(req: Request):
        # 画面の ID（client）が付いていれば、自動の終了のために、画面がつながっていることを記録します
        ae = app.services.get("auto_exit")
        client = (req.arg("client") or "")[:64]
        if ae is None or not client:
            return _events(req)
        ae.poll_begin(client)
        try:
            return _events(req)
        finally:
            ae.poll_end(client)

    def _events(req: Request):
        after = req.arg("after")
        if after is None or after == "":
            return json_response({"last": app.events.last, "events": []})
        try:
            n = int(after)
        except ValueError:
            n = -1
        return json_response(app.events.wait(n, 25.0))

    @router.post("/api/v1/app/client")
    def client_state(req: Request):
        # 画面の状態の知らせ（タブを閉じた・凍結された・戻った）。自動の終了に使います
        body = req.json() or {}
        client = str(body.get("client") or "")[:64]
        state = body.get("state")
        ae = app.services.get("auto_exit")
        if ae is not None and client and state in ("closed", "frozen", "active"):
            ae.set_state(client, state)
        return json_response({"ok": True})

    @router.post("/api/v1/app/launch-url")
    def launch_url(req: Request):
        """別のブラウザで開くための、起動コード付きの URL（v1.7.2）。

        起動コードは 1 回だけ・60 秒間使えます（S3）。この要求は、本ツールの画面（Cookie と CSRF のヘッダーがある）からだけ
        受け付けるため、URL を知ることができるのは、すでに画面を開いている利用者だけです。
        """
        from .tutorial_api import same_host
        return json_response({"url": same_host(app.security.launch_url(), req.header("host")), "expires_in": 60})

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
