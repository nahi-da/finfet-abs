"""サインイン・接続先の追加・保存した認証情報・プロキシの確認の API（5.7.2）。"""

from __future__ import annotations

import logging

from ..azure.credentials import InteractionRequired
from ..azure.sas import ParseError
from ..core.errors import AppError, bad_request
from ..server.http import Request, json_response, simple_page

log = logging.getLogger(__name__)


def register(router, app) -> None:
    s = app.services
    if "auth" not in s:
        return

    def auth():
        return s["auth"]

    def task(fn, kind: str):
        tid = s["tasks"].submit(fn, kind)
        return json_response({"task_id": tid}, 202)

    # ------------------------------------------------------------ サインインの戻り（2.5 の例外）

    def signin_return(req: Request):
        query = {k: req.arg(k) for k in ("code", "state", "session_state", "client_info", "error",
                                         "error_description") if req.arg(k) is not None}
        ok, message = auth().handle_return(query)
        link = ("/", "本ツールの画面を開く") if app.security.session_ok(req.cookies.get(app.security.cookie_name)) else None
        return simple_page("サインイン" if ok else "サインインできませんでした", message, 200 if ok else 400, link)

    s["signin_return"] = signin_return

    # ------------------------------------------------------------ サインイン

    @router.get("/api/v1/auth/status")
    def status(req: Request):
        st = auth().status()
        st["default_client_id"] = app.settings.get("auth.client_id") == "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
        return json_response(st)

    @router.post("/api/v1/auth/user/sign-in")
    def sign_in(req: Request):
        """サインイン。add: 別のアカウントを追加（アカウントの選択の画面）、account: そのアカウントで。"""
        body = req.json() or {}
        try:
            return json_response(auth().start_sign_in(None, account=body.get("account") or None, add=bool(body.get("add"))))
        except InteractionRequired as exc:
            raise AppError(exc.info.code, exc.info.message) from None

    @router.post("/api/v1/auth/tenants/{tenant_id}/sign-in")
    def sign_in_tenant(req: Request):
        body = req.json() or {}
        try:
            return json_response(auth().start_sign_in(req.params["tenant_id"], account=body.get("account") or None))
        except InteractionRequired as exc:
            raise AppError(exc.info.code, exc.info.message) from None

    @router.post("/api/v1/auth/default")
    def set_default(req: Request):
        """既定のアカウント（接続先でアカウントを指定しないときに使うもの）を変えます。"""
        try:
            auth().set_default(req.json().get("account") or "")
        except InteractionRequired as exc:
            raise AppError(exc.info.code, exc.info.message) from None
        if s.get("clients") is not None:
            s["clients"].reset()
        return json_response({"ok": True})

    @router.post("/api/v1/auth/sign-in/cancel")
    def cancel(req: Request):
        auth().cancel(req.json().get("flow_id"))
        return json_response({"ok": True})

    @router.post("/api/v1/auth/sign-out")
    def sign_out(req: Request):
        """サインアウト。account を指定すればそのアカウントだけ、指定しなければすべて。"""
        body = req.json() or {}
        account = body.get("account") or None
        auth().sign_out(account)
        if account and hasattr(s["arm"], "forget"):
            s["arm"].forget(account.lower())
        if s.get("clients") is not None:
            s["clients"].reset()
        return json_response({"ok": True})

    # ------------------------------------------------------------ 接続先の追加

    @router.post("/api/v1/secrets/parse")
    def parse_secret(req: Request):
        body = req.json()
        try:
            _c, info = s["targets"].parse_input(body)
        except AppError as err:
            return json_response({"ok": False, "error": err.message})
        return json_response({"ok": True, "info": info})

    @router.get("/api/v1/azure/storage-accounts")
    def storage_accounts(req: Request):
        q = (req.arg("q") or "").lower()
        refresh = req.arg("refresh") == "1"
        account = req.arg("account") or None

        holder: dict = {}

        def progress(info: dict) -> None:
            # 取得中の表示（v1.5.3）。処理の ID は、始めた直後に決まります
            app.events.publish("accounts.progress", {"id": holder.get("id"), **info})

        def run():
            from ..azure.storage import arm_for
            try:
                data = arm_for(s["arm"], account).list_storage_accounts(refresh=refresh, progress=progress)
            except InteractionRequired as exc:
                raise AppError(exc.info.code, exc.info.message) from None
            rows = data["accounts"]
            if q:
                rows = [r for r in rows if all(w in f"{r.get('name')} {r.get('subscriptionName')} {r.get('resourceGroup')} "
                                                     f"{r.get('location')}".lower() for w in q.split())]
            return {"accounts": [{"name": r["name"], "subscription": r.get("subscriptionName") or r.get("subscriptionId"),
                                  "resource_group": r.get("resourceGroup"), "location": r.get("location"),
                                  "tenant_id": r.get("tenantId"), "tenant_name": r.get("tenantName"),
                                  "allow_shared_key": r.get("allowSharedKeyAccess") is not False,
                                  "is_hns": bool(r.get("isHnsEnabled"))} for r in rows],
                    "unavailable": data["unavailable"], "multi_tenant": data.get("multi_tenant", False)}
        tid = s["tasks"].submit(run, "storage-accounts")
        holder["id"] = tid
        return json_response({"task_id": tid}, 202)

    @router.post("/api/v1/containers/list")
    def containers_list(req: Request):
        body = req.json()
        return task(lambda: s["targets"].list_containers(body.get("storage_account", ""), body.get("endpoint_suffix", ""),
                                                          body.get("auth") or {}), "containers")

    @router.post("/api/v1/targets/add")
    def targets_add(req: Request):
        body = req.json()
        try:
            res = s["targets"].add(body.get("storage_account", ""), body.get("endpoint_suffix", ""), body.get("auth") or {},
                                   body.get("containers") or [], replace=bool(body.get("replace")))
        except ParseError as exc:
            raise bad_request(str(exc), "INPUT_INVALID") from None
        return json_response(res)

    @router.post("/api/v1/targets/resolve")
    def targets_resolve(req: Request):
        targets = req.json().get("targets") or []
        return json_response({"targets": [s["targets"].resolve(t) for t in targets]})

    @router.post("/api/v1/targets/describe")
    def targets_describe(req: Request):
        targets = req.json().get("targets") or []
        return json_response({"targets": [s["targets"].describe(t) for t in targets]})

    @router.post("/api/v1/targets/test")
    def targets_test(req: Request):
        t = req.json().get("target") or {}
        if not t.get("storage_account"):
            raise bad_request("接続先を指定してください。")
        return task(lambda: s["targets"].test(t), "target-test")

    # ------------------------------------------------------------ 保存した認証情報

    @router.get("/api/v1/credentials")
    def credentials(req: Request):
        q = req.arg("q") or ""
        account = req.arg("account")
        if account:
            items = s["credstore"].usable_for(account, req.arg("suffix") or "core.windows.net", req.arg("container"))
        else:
            items = s["credstore"].list(q)
        return json_response({"total": len(items), "items": items})

    @router.delete("/api/v1/credentials/{cid}")
    def credential_delete(req: Request):
        res = s["credstore"].delete([req.params["cid"]], confirm=req.arg("confirm") == "true")
        return json_response(res, 409 if res["confirm_required"] else 200)

    @router.post("/api/v1/credentials/delete")
    def credentials_delete(req: Request):
        body = req.json()
        ids = None if body.get("all") else list(body.get("ids") or [])
        res = s["credstore"].delete(ids, confirm=bool(body.get("confirm")))
        return json_response(res, 409 if res["confirm_required"] else 200)

    @router.post("/api/v1/credentials/{cid}/test")
    def credential_test(req: Request):
        row = s["credstore"].get(req.params["cid"])
        if row is None:
            raise AppError("CREDENTIAL_NOT_FOUND", "認証情報がありません。", status=404)
        t = {"storage_account": row["storage_account"], "endpoint_suffix": row["endpoint_suffix"],
             "container": row["scope_container"], "auth_kind": row["auth_kind"], "credential_id": row["id"]}
        return task(lambda: s["targets"].test(t), "credential-test")

    # ------------------------------------------------------------ プロキシの確認（3.1.11）

    @router.post("/api/v1/settings/proxy/test")
    def proxy_test(req: Request):
        body = req.json()
        account = (body.get("storage_account") or "").strip()

        def run():
            net = s.get("network")
            if net is None:
                return {"results": [{"target": t, "via": "直接", "ok": True, "message": "デモモードのため、確認を省略します。"}
                                    for t in ("login.microsoftonline.com", "management.azure.com")]}
            from ..azure.errors import network_message
            from ..azure.proxy import proxy_label, resolve_proxy
            cfg = net.config()
            session = net.session()
            targets = ["https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration",
                       "https://management.azure.com/"]
            if account:
                targets.append(f"https://{account}.blob.core.windows.net/?comp=properties&restype=service")
            out = []
            for url in targets:
                host = url.split("/")[2]
                via = proxy_label(resolve_proxy(cfg, url))
                try:
                    r = session.get(url, timeout=15)
                    out.append({"target": host, "via": via, "ok": True, "message": f"HTTP {r.status_code}"})
                except Exception as exc:  # noqa: BLE001
                    info = network_message(exc, account=account or None, proxy=via.replace("プロキシ ", ""))
                    out.append({"target": host, "via": via, "ok": False,
                                "message": info.message if info else "接続できませんでした。"})
            if net.error:
                out.insert(0, {"target": "追加の CA 証明書", "via": "", "ok": False, "message": net.error})
            return {"results": out}
        return task(run, "proxy-test")
