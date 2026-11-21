"""接続先の追加（3.1.3）、呼び出したときの認証（3.1.9）、接続テスト（3.1.10）。"""

from __future__ import annotations

import logging
import secrets
import threading
import time

from ..core.errors import AppError
from ..core.secret import Secret
from .backend import AccountRef, AuthRef, BackendError, ContainerRef
from .credentials import InteractionRequired, account_key
from .credstore import CredentialStore, NewCredential, new_from_input
from .sas import DEFAULT_SUFFIX, ParseError, validate_account, validate_suffix

log = logging.getLogger(__name__)

TMP_TTL = 1800.0


class TempSecrets:
    """接続先の追加のダイアログで入力した、まだ保存していない認証情報（メモリだけ、30 分）。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, NewCredential]] = {}
        self._lock = threading.Lock()

    def put(self, c: NewCredential) -> str:
        tid = "tmp:" + secrets.token_urlsafe(12)
        with self._lock:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if now - v[0] < TMP_TTL}
            self._items[tid] = (now, c)
        return tid

    def get(self, tid: str) -> NewCredential | None:
        with self._lock:
            v = self._items.get(tid)
        return v[1] if v else None


def _auth_error(exc: BackendError) -> bool:
    """保存せずに誤りを表示する認証の誤り（キーの誤り、署名の誤り、期限切れの SAS）か。"""
    return exc.code in ("AuthenticationFailed", "KeyBasedAuthenticationNotPermitted") or exc.status == 401


class TargetService:
    """接続先の追加・認証の決定・接続テスト。"""

    def __init__(self, *, backend, credstore: CredentialStore, auth=None, arm=None, settings=None, clock=None) -> None:
        self.backend = backend
        self.credstore = credstore
        self.auth = auth
        self.arm = arm
        self.settings = settings
        self.clock = clock
        self.temp = TempSecrets()

    # ------------------------------------------------------------ 認証の指定 → AuthRef

    def parse_input(self, spec: dict) -> tuple[NewCredential, dict]:
        try:
            return new_from_input(spec.get("source", ""), value=spec.get("value", ""), account=spec.get("account", ""),
                                  suffix=spec.get("endpoint_suffix") or DEFAULT_SUFFIX, key=spec.get("key", ""),
                                  allow_development=bool(self.settings and self.settings.get("dev.allow_development_storage")))
        except ParseError as exc:
            raise AppError("INPUT_INVALID", str(exc)) from None

    def auth_ref(self, auth: dict) -> tuple[AuthRef, NewCredential | None]:
        """API の auth（{kind} / {credential_id} / {input}）を AuthRef にします。"""
        if auth.get("input"):
            c, _info = self.parse_input(auth["input"])
            tid = self.temp.put(c)
            return AuthRef(c.auth_kind, credential_id=tid), c
        if auth.get("credential_id"):
            row = self.credstore.get(auth["credential_id"])
            if row is None:
                raise AppError("CREDENTIAL_NOT_FOUND", "認証情報がありません。", status=404)
            return AuthRef(row["auth_kind"], credential_id=row["id"]), None
        kind = auth.get("kind")
        if kind not in ("entra_user", "entra_key"):
            raise AppError("AUTH_REQUIRED", "認証方式を選択してください。")
        user = account_key(auth.get("account")) or None
        if self.auth is not None and not self._signed_in(user):
            raise AppError("NOT_SIGNED_IN", f"{user} でサインインしていません。" if user else "サインインしていません。")
        return AuthRef(kind, tenant_id=auth.get("tenant") or None, account=user), None

    def _signed_in(self, account: str | None) -> bool:
        """そのアカウント（None は既定のアカウント）でサインインしているか。"""
        if self.auth is None:
            return True
        f = getattr(self.auth, "is_signed_in", None)
        return f(account) if f is not None else self.auth.signed_in

    def resolve_secret(self, cred_id: str) -> tuple[str, Secret]:
        """ClientFactory から呼ばれます（保存した認証情報、または入力中の認証情報）。"""
        if cred_id.startswith("tmp:"):
            c = self.temp.get(cred_id)
            if c is None:
                raise BackendError("NO_CREDENTIAL", "入力した認証情報の有効時間が過ぎました。もう一度入力してください。")
            return c.source, c.secret
        row = self.credstore.get(cred_id)
        if row is None:
            raise BackendError("NO_CREDENTIAL", "認証情報がありません。")
        self.credstore.touch(cred_id)
        return row["source"], self.credstore.secret(cred_id)

    # ------------------------------------------------------------ コンテナーの一覧

    def list_containers(self, account: str, suffix: str, auth: dict) -> dict:
        """コンテナーの一覧（3.1.3.6）。認証の誤りは AppError、権限などで一覧を取得できないだけなら list_error。"""
        account = validate_account(account)
        suffix = validate_suffix(suffix or DEFAULT_SUFFIX)
        ref, new = self.auth_ref(auth)
        if new is not None and new.scope_container:
            # コンテナーの SAS: そのコンテナーの一覧で接続を確かめます
            target = ContainerRef(AccountRef(account, suffix, ref), new.scope_container)
            if (new.permissions or "").find("l") >= 0:
                try:
                    self.backend.list_blobs_page(target, "", None, 1)
                except BackendError as exc:
                    if _auth_error(exc):
                        raise AppError(exc.code, exc.message) from None
            return {"containers": [new.scope_container], "fixed_container": new.scope_container, "list_error": None,
                    "auth_kind": ref.kind}
        if new is not None and (new.account != account or new.suffix != suffix):
            raise AppError("ACCOUNT_MISMATCH", "入力した認証情報は、このストレージアカウントのものではありません。")
        a = AccountRef(account, suffix, ref)
        names: list[str] | None = None
        if ref.kind in ("entra_user", "entra_key") and self.arm is not None:
            try:
                from .storage import arm_for
                names = arm_for(self.arm, ref.account).list_containers(account)
            except Exception:  # noqa: BLE001
                names = None
        if names is None:
            try:
                names = self.backend.list_containers(a)
            except BackendError as exc:
                if new is not None and _auth_error(exc):
                    raise AppError(exc.code, exc.message) from None
                if ref.kind == "entra_key" and exc.code in ("KeyBasedAuthenticationNotPermitted", "StorageAccountNotFound",
                                                            "AuthorizationFailed"):
                    raise AppError(exc.code, exc.message) from None
                if exc.code == "INTERACTION_REQUIRED":
                    raise AppError(exc.code, exc.message) from None
                return {"containers": [], "list_error": exc.message, "error_code": exc.code, "auth_kind": ref.kind}
            except InteractionRequired as exc:
                raise AppError("INTERACTION_REQUIRED", exc.info.message, extra={"tenant_id": exc.tenant_id}) from None
        return {"containers": sorted(names), "list_error": None, "auth_kind": ref.kind}

    # ------------------------------------------------------------ 追加

    def add(self, account: str, suffix: str, auth: dict, containers: list[str], *, replace: bool = False) -> dict:
        """選んだ接続先を確定します。入力した認証情報があれば保存します（3.1.8）。"""
        account = validate_account(account)
        suffix = validate_suffix(suffix or DEFAULT_SUFFIX)
        containers = [c.strip() for c in containers if c and c.strip()]
        if not containers:
            raise AppError("NO_CONTAINER", "コンテナーを1つ以上選んでください。")
        cred_id = None
        if auth.get("input"):
            c, _info = self.parse_input(auth["input"])
            if self.clock is not None and c.expires_at:
                from ..core.clock import from_iso
                if from_iso(c.expires_at) <= self.clock.now():
                    raise AppError("SAS_EXPIRED", "SAS の有効期限が切れています。")
            cred_id, _replaced = self.credstore.add(c, replace=replace)
            kind = c.auth_kind
            if c.scope_container:
                containers = [c.scope_container]
        elif auth.get("credential_id"):
            row = self.credstore.get(auth["credential_id"])
            if row is None:
                raise AppError("CREDENTIAL_NOT_FOUND", "認証情報がありません。", status=404)
            cred_id = row["id"]
            kind = row["auth_kind"]
            if row["scope_container"]:
                containers = [row["scope_container"]]
        else:
            kind = auth.get("kind")
            if kind not in ("entra_user", "entra_key"):
                raise AppError("AUTH_REQUIRED", "認証方式を選択してください。")
        user = (account_key(auth.get("account")) or None) if kind in ("entra_user", "entra_key") and not cred_id else None
        targets = [{"storage_account": account, "endpoint_suffix": suffix, "container": cont, "auth_kind": kind,
                    "tenant": auth.get("tenant") or None, "credential_id": cred_id, "account": user}
                   for cont in dict.fromkeys(containers)]
        return {"targets": [self.describe(t) for t in targets], "credential_id": cred_id}

    # ------------------------------------------------------------ 認証の決定（3.1.9）

    def describe(self, t: dict) -> dict:
        """接続先の行の表示（認証のラベル、ホバーの詳細、権限・期限の警告）。"""
        out = dict(t)
        kind = t.get("auth_kind")
        out["auth_label"] = {"entra_user": "Entra ID", "entra_key": "キー", "account_key": "キー", "sas": "SAS"}.get(kind, "")
        warnings: list[str] = []
        detail = {"entra_user": "Entra ID（ユーザーアカウント）", "entra_key": "アクセスキー（Entra ID で取得）",
                  "account_key": "アクセスキー", "sas": "SAS"}.get(kind, "")
        if kind in ("entra_user", "entra_key") and not t.get("credential_id"):
            # 使うアカウント（指定がなければ既定のアカウント）。複数のアカウントでサインインしているときに見分けるため
            info = self.auth.account_info(t.get("account")) if self.auth is not None and hasattr(self.auth, "account_info") else None
            user = (info or {}).get("username") or t.get("account") or ""
            out["account_label"] = user
            if user:
                detail = detail.replace("）", f": {user}）") if kind == "entra_user" else f"{detail}（{user}）"
        if t.get("credential_id"):
            row = self.credstore.get(t["credential_id"])
            if row is not None:
                pub = self.credstore.public(row)
                out["scope_container"] = row["scope_container"]
                if kind == "sas":
                    from .sas import SasInfo
                    perms = row["permissions"] or ""
                    scope = "ストレージアカウント全体" if row["scope_container"] is None else f"コンテナー {row['scope_container']}"
                    detail = f"SAS（{scope}、権限 {perms or '不明'}" + (f"、{pub['expires_at']} まで）" if pub["expires_at"] else "）")
                    if pub["expired"]:
                        warnings.append("SAS の有効期限が切れています。")
                    elif pub["expires_at"] and self.clock is not None:
                        from ..core.clock import from_iso
                        from datetime import timedelta
                        hours = self.settings.get("auth.sas_expiry_warn_hours") if self.settings else 24
                        if from_iso(pub["expires_at"]) <= self.clock.now() + timedelta(hours=hours):
                            warnings.append("SAS の有効期限が近づいています。")
                    if "l" not in perms and perms:
                        warnings.append("この SAS には一覧（l）の権限がないため、検索できません。")
                if not pub["usable"]:
                    warnings.append(pub["decrypt_error"]["message"])
        out["auth_detail"] = detail
        out["warnings"] = warnings
        return out

    def resolve(self, t: dict) -> dict:
        """呼び出した検索条件の接続先の認証を決めます（3.1.9 の 1〜4）。"""
        kind = t.get("auth_kind")
        cid = t.get("credential_id")
        account, suffix, container = t.get("storage_account"), t.get("endpoint_suffix") or DEFAULT_SUFFIX, t.get("container")
        # 1. 記録した ID の保存した認証情報があり、復号できる
        if cid:
            row = self.credstore.get(cid)
            if row is not None:
                try:
                    self.credstore.secret(cid)
                    return {**self.describe({**t, "auth_kind": row["auth_kind"]}), "resolved": True}
                except Exception:  # noqa: BLE001
                    pass
        # 2. Entra ID の方式で、サインインしていれば、その方式
        if kind in ("entra_user", "entra_key"):
            user = account_key(t.get("account")) or None
            if self._signed_in(user):
                return {**self.describe({**t, "credential_id": None}), "resolved": True}
            reason = f"サインインが必要です（{user}）。" if user else "サインインが必要です。"
            return {**self.describe({**t, "credential_id": None}), "resolved": False, "reason": reason}
        # 3. 同じストレージアカウント・ドメインの保存した認証情報のうち、そのコンテナーに使えるもの
        for c in self.credstore.usable_for(account, suffix, container):
            if c["usable"]:
                return {**self.describe({**t, "auth_kind": c["auth_kind"], "credential_id": c["id"]}), "resolved": True}
        # 4. 決まらない
        return {**self.describe({**t, "credential_id": None}), "resolved": False, "reason": "認証方式を選択してください。"}

    # ------------------------------------------------------------ 接続テスト

    def test(self, t: dict) -> dict:
        ref = AuthRef(t.get("auth_kind") or "entra_user", credential_id=t.get("credential_id"), tenant_id=t.get("tenant"),
                      account=account_key(t.get("account")) or None)
        acct = AccountRef(t["storage_account"], t.get("endpoint_suffix") or DEFAULT_SUFFIX, ref)
        target = ContainerRef(acct, t["container"]) if t.get("container") else acct
        try:
            msg = self.backend.test_target(target)
            ok = True
        except BackendError as exc:
            msg = exc.message + (f"（{exc.code}）" if exc.code and exc.code.isascii() else "")
            ok = False
        except InteractionRequired as exc:
            msg, ok = exc.info.message, False
        if t.get("credential_id") and not str(t["credential_id"]).startswith("tmp:"):
            self.credstore.record_test(t["credential_id"], ok, msg)
        return {"ok": ok, "message": msg}
