"""保存した認証情報（3.1.8）と、呼び出したときの認証の決め方（3.1.9）。

秘密情報（接続文字列・SAS URL・アカウントキー）は DPAPI で暗号化して保存し、画面と API の応答には出しません。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from ..core import dpapi
from ..core.clock import SYSTEM_CLOCK, Clock, from_iso, to_iso
from ..core.db import Database
from ..core.errors import AppError
from ..core.secret import Secret
from .connstr import parse_connection_string
from .sas import parse_sas_url

log = logging.getLogger(__name__)

SOURCE_LABELS = {"connection_string": "接続文字列", "sas_url": "SAS URL", "account_key": "アカウント名とキー"}
AUTH_LABELS = {"account_key": "アクセスキー", "sas": "SAS", "entra_user": "Entra ID", "entra_key": "アクセスキー（Entra ID で取得）"}
ENDED = ("completed", "completed_with_errors", "cancelled")


@dataclass
class NewCredential:
    """追加する認証情報（解析・検査済み）。"""

    account: str
    suffix: str
    scope_container: str | None
    auth_kind: str  # account_key / sas
    source: str  # connection_string / sas_url / account_key
    secret: Secret
    sas_kind: str | None = None
    permissions: str | None = None
    expires_at: str | None = None


class CredentialStore:
    """保存した認証情報。"""

    def __init__(self, db: Database, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock

    # ------------------------------------------------------------ 参照

    def find_same_scope(self, account: str, suffix: str, container: str | None):
        return self.db.query_one("SELECT * FROM credentials WHERE storage_account=? AND endpoint_suffix=? AND "
                                 "IFNULL(scope_container,'')=?", (account, suffix, container or ""))

    def get(self, cred_id: str):
        return self.db.query_one("SELECT * FROM credentials WHERE id=?", (cred_id,))

    def secret(self, cred_id: str) -> Secret:
        """秘密情報を復号します。使う直前にだけ呼びます。"""
        row = self.get(cred_id)
        if row is None:
            raise AppError("CREDENTIAL_NOT_FOUND", "認証情報がありません。", status=404)
        return dpapi.unseal(row["secret"], "credential")

    def public(self, row) -> dict:
        """画面に返す形（秘密情報は返しません。2.5 の S8）。"""
        now = self.clock.now()
        exp = from_iso(row["expires_at"]) if row["expires_at"] else None
        usable = True
        reason = None
        try:
            dpapi.unseal(row["secret"], "credential")
        except dpapi.DecryptError as exc:
            usable = False
            reason = {"reason": exc.reason, "message": exc.message, "winerror": exc.winerror}
        return {
            "id": row["id"], "storage_account": row["storage_account"], "endpoint_suffix": row["endpoint_suffix"],
            "scope_container": row["scope_container"], "auth_kind": row["auth_kind"],
            "auth_label": AUTH_LABELS.get(row["auth_kind"], row["auth_kind"]),
            "source": row["source"], "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
            "sas_kind": row["sas_kind"], "permissions": row["permissions"], "expires_at": row["expires_at"],
            "expired": bool(exp and exp <= now), "last_test_at": row["last_test_at"],
            "last_test_ok": None if row["last_test_ok"] is None else bool(row["last_test_ok"]),
            "last_test_message": row["last_test_message"], "created_at": row["created_at"],
            "last_used_at": row["last_used_at"], "usable": usable, "decrypt_error": reason,
            "saved_env": {k: v for k, v in dpapi.stored_environment(row["secret"]).items() if k in ("user", "computer", "at")},
        }

    def list(self, q: str = "") -> list[dict]:
        rows = self.db.query("SELECT * FROM credentials ORDER BY storage_account, IFNULL(scope_container,'')")
        out = [self.public(r) for r in rows]
        if q:
            words = q.lower().split()
            out = [c for c in out if all(w in f"{c['storage_account']} {c['scope_container'] or ''}".lower() for w in words)]
        return out

    def usable_for(self, account: str, suffix: str, container: str | None) -> list[dict]:
        """その接続先に使える保存した認証情報（範囲がコンテナーのものを先に）。"""
        rows = self.db.query("SELECT * FROM credentials WHERE storage_account=? AND endpoint_suffix=?", (account, suffix))
        out = [r for r in rows if r["scope_container"] is None or (container and r["scope_container"] == container)]
        out.sort(key=lambda r: 0 if r["scope_container"] else 1)
        return [self.public(r) for r in out]

    # ------------------------------------------------------------ 追加・置き換え

    def add(self, c: NewCredential, *, replace: bool = False) -> tuple[str, bool]:
        """保存します。同じ範囲のものがあれば、replace=True のときだけ置き換えます。戻り値は (ID, 置き換えたか)。"""
        existing = self.find_same_scope(c.account, c.suffix, c.scope_container)
        if existing is not None and not replace:
            raise AppError("CREDENTIAL_EXISTS", "同じ範囲の認証情報が保存されています。上書きしますか。", status=409,
                           extra={"existing": self.public(existing)})
        now = to_iso(self.clock.now())
        sealed = dpapi.seal(c.secret, "credential")
        if existing is not None:
            cid = existing["id"]
            self.db.execute("UPDATE credentials SET auth_kind=?, source=?, secret=?, sas_kind=?, permissions=?, "
                            "expires_at=?, updated_at=?, last_test_at=NULL, last_test_ok=NULL, last_test_message=NULL "
                            "WHERE id=?", (c.auth_kind, c.source, sealed, c.sas_kind, c.permissions, c.expires_at, now, cid))
            log.info("認証情報を上書きしました: %s/%s", c.account, c.scope_container or "")
            return cid, True
        cid = str(uuid.uuid4())
        self.db.execute("INSERT INTO credentials(id, storage_account, endpoint_suffix, scope_container, auth_kind, source, "
                        "secret, sas_kind, permissions, expires_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, c.account, c.suffix, c.scope_container, c.auth_kind, c.source, sealed, c.sas_kind,
                         c.permissions, c.expires_at, now, now))
        log.info("認証情報を保存しました: %s/%s（%s）", c.account, c.scope_container or "", c.source)
        return cid, False

    def record_test(self, cred_id: str, ok: bool, message: str) -> None:
        self.db.execute("UPDATE credentials SET last_test_at=?, last_test_ok=?, last_test_message=? WHERE id=?",
                        (to_iso(self.clock.now()), 1 if ok else 0, message, cred_id))

    def touch(self, cred_id: str) -> None:
        self.db.submit(lambda conn: conn.execute("UPDATE credentials SET last_used_at=? WHERE id=?",
                                                 (to_iso(self.clock.now()), cred_id)))

    # ------------------------------------------------------------ 削除

    def usage(self, ids: list[str]) -> dict:
        """終了していないジョブと検索履歴での使用（3.1.8 の削除の確認）。"""
        if not ids:
            return {"jobs": [], "history_count": 0}
        marks = ",".join("?" * len(ids))
        jobs = self.db.query(
            f"SELECT DISTINCT j.id, j.name FROM jobs j JOIN job_items i ON i.job_id=j.id WHERE i.credential_id IN ({marks}) "
            f"AND j.status NOT IN ('completed','completed_with_errors','cancelled')", ids)
        count = 0
        for r in self.db.query("SELECT condition_json FROM search_history"):
            try:
                targets = json.loads(r["condition_json"]).get("targets") or []
            except ValueError:
                continue
            if any(t.get("credential_id") in ids for t in targets):
                count += 1
        return {"jobs": [{"id": j["id"], "name": j["name"]} for j in jobs], "history_count": count}

    def delete(self, ids: list[str] | None, *, confirm: bool = False) -> dict:
        """削除します。使っているジョブ・検索履歴があり、confirm でなければ、削除せずに使用の状況を返します。"""
        if ids is None:
            ids = [r["id"] for r in self.db.query("SELECT id FROM credentials")]
        use = self.usage(ids)
        if (use["jobs"] or use["history_count"]) and not confirm:
            return {"deleted": 0, "confirm_required": True, **use}
        marks = ",".join("?" * len(ids)) or "''"
        n = self.db.execute(f"DELETE FROM credentials WHERE id IN ({marks})", ids)
        log.info("認証情報を削除しました（%s 件）", n)
        return {"deleted": n, "confirm_required": False, **use}


def new_from_input(source: str, *, value: str = "", account: str = "", suffix: str = "", key: str = "",
                   container: str | None = None, allow_development: bool = False) -> tuple[NewCredential, dict]:
    """入力した認証情報を解析し、保存できる形にします（3.1.3.3〜3.1.3.5）。戻り値は (認証情報, 解析の結果)。"""
    from .connstr import validate_account_key
    from .sas import validate_account, validate_suffix
    if source == "connection_string":
        info = parse_connection_string(value, allow_development=allow_development)
        sas = info.sas
        return NewCredential(info.account, info.suffix, None, "sas" if sas else "account_key", source, Secret(value.strip()),
                             sas.kind if sas else None, sas.permissions if sas else None,
                             to_iso(sas.expiry) if sas and sas.expiry else None), info.public_json()
    if source == "sas_url":
        u = parse_sas_url(value)
        if u.level == "blob":
            from .sas import ParseError
            raise ParseError("Blob の SAS URL は、検索ページの「URL で指定」で使用してください。")
        scope = u.container if u.level == "container" else None
        # dfs の URL は blob の URL に読み替えて保存します
        url = value.strip()
        if u.from_dfs:
            url = url.replace(".dfs.", ".blob.", 1)
        return NewCredential(u.account, u.suffix, scope, "sas", source, Secret(url), u.info.kind, u.info.permissions,
                             to_iso(u.info.expiry) if u.info.expiry else None), u.public_json()
    if source == "account_key":
        acct = validate_account(account)
        sfx = validate_suffix(suffix or "core.windows.net")
        k = validate_account_key(key)
        return NewCredential(acct, sfx, None, "account_key", source, Secret(k)), {
            "kind": "account_key", "account": acct, "endpoint_suffix": sfx, "level": "service"}
    raise AppError("BAD_SOURCE", "追加の方法が正しくありません。")
