"""Azure SDK・MSAL・ネットワークの例外を、日本語の文に変えます（3.7）。"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

from ..core.secret import redact
from .backend import BackendError

log = logging.getLogger(__name__)

MESSAGES = {
    "AuthorizationPermissionMismatch": "この Blob（コンテナー）を操作する権限がありません。Entra ID の場合は、ストレージ BLOB データ閲覧者（検索・ダウンロード）または共同作成者（リハイドレート）のロールが必要です。",
    "AuthorizationFailure": "ストレージアカウントのファイアウォールにより接続できない可能性があります（この PC の IP アドレスが許可されていない、プライベートエンドポイントのみ許可されている、など）。",
    "KeyBasedAuthenticationNotPermitted": "このストレージアカウントでは、アクセスキー・アカウント SAS による認証が無効にされています。Entra ID（ユーザーアカウント）を使用してください。",
    "AuthenticationFailed": "SAS またはアクセスキーが無効です（期限切れ、署名の誤り、許可されていない IP アドレスなど）。",
    "ContainerNotFound": "コンテナーが見つかりません。",
    "BlobNotFound": "Blob が見つかりません。",
    "ResourceNotFound": "ストレージアカウントまたはコンテナーが見つかりません。",
    "BlobArchived": "Archive 層のため、ダウンロードできません。リハイドレートが必要です。",
    "BlobBeingRehydrated": "すでにリハイドレート中です。",
    "ServerBusy": "Azure が混雑しています。自動で間隔を空けて再試行します。",
    "OperationTimedOut": "Azure が混雑しています。自動で間隔を空けて再試行します。",
    "TooManyRequests": "Azure が混雑しています。自動で間隔を空けて再試行します。",
    "ConditionNotMet": "ダウンロード中に Blob が更新されたため、最初からダウンロードし直します。",
    "AuthorizationFailed": "アクセスキーを取得する権限がありません。",
    "InvalidAuthenticationInfo": "認証の情報が正しくありません（テナントの違い、トークンの期限切れなど）。",
    "NoAuthenticationInformation": "認証の情報がありません。",
    "PublicAccessNotPermitted": "このストレージアカウントでは、認証なしのアクセスが許可されていません。",
}

AADSTS = {
    "53003": "条件付きアクセスによりブロックされています。",
    "50076": "多要素認証が必要です。[このテナントにサインイン] からサインインしてください。",
    "50079": "多要素認証が必要です。[このテナントにサインイン] からサインインしてください。",
    "50020": "このアカウントは、このテナントに登録されていません。",
    "65001": "このアプリの使用が許可されていません（管理者の同意が必要です）。",
    "700016": "アプリケーションが見つかりません。設定「クライアント ID」を確認してください。",
    "7000215": "クライアントシークレットが無効です（期限切れの可能性）。",
    "90002": "テナントが見つかりません。",
}


@dataclass
class ErrorInfo:
    code: str
    message: str
    status: int | None = None
    request_id: str | None = None
    detail: str = ""
    retry_after: float | None = None


def _walk_causes(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def network_message(exc: BaseException, *, account: str | None = None, proxy: str | None = None) -> ErrorInfo | None:
    """ネットワークの例外（名前解決・プロキシ・証明書・タイムアウト）の文。"""
    import requests
    for e in _walk_causes(exc):
        name = type(e).__name__
        text = str(e)
        if isinstance(e, requests.exceptions.ProxyError) or "ProxyError" in name:
            if "407" in text:
                return ErrorInfo("ProxyAuthenticationRequired", "プロキシの認証が必要です。設定の「プロキシ」で ID とパスワードを入力してください。")
            return ErrorInfo("ProxyConnectFailed", f"プロキシ「{proxy or '不明'}」に接続できません。設定の「プロキシ」を確認してください。")
        if isinstance(e, requests.exceptions.SSLError) or "SSLError" in name or "CERTIFICATE_VERIFY_FAILED" in text:
            return ErrorInfo("CertificateVerifyFailed", "通信の証明書を検証できません。通信を検査するプロキシを使っている場合は、組織の CA 証明書を設定の「追加の CA 証明書」に指定してください。")
        if isinstance(e, socket.gaierror) or "getaddrinfo failed" in text or "NameResolutionError" in name or \
                "Failed to resolve" in text:
            target = f"ストレージアカウント「{account}」" if account else "接続先"
            return ErrorInfo("NameResolutionFailed", f"{target}の名前を解決できません。名前の誤り、またはネットワーク（DNS・プロキシ）の問題の可能性があります。")
        if isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError, TimeoutError,
                          ConnectionRefusedError)) or "Timeout" in name:
            return ErrorInfo("ConnectionFailed", "接続できませんでした。ネットワーク、プロキシ、ファイアウォールの設定を確認してください。")
    return None


def from_exception(exc: BaseException, *, account: str | None = None, container: str | None = None,
                   proxy: str | None = None) -> BackendError:
    """例外を BackendError（日本語の文付き）に変えます。"""
    if isinstance(exc, BackendError):
        return exc
    from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError
    if isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "error_code", None) or ""
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", {}) or {}
        request_id = headers.get("x-ms-request-id") if hasattr(headers, "get") else None
        retry_after = None
        try:
            ra = headers.get("Retry-After") if hasattr(headers, "get") else None
            retry_after = float(ra) if ra else None
        except ValueError:
            pass
        if not code:
            code = {404: "ResourceNotFound", 403: "AuthorizationFailure", 409: "Conflict", 412: "ConditionNotMet",
                    429: "TooManyRequests", 503: "ServerBusy", 500: "InternalError"}.get(status or 0, f"HTTP{status}")
        msg = MESSAGES.get(code)
        if code == "ContainerNotFound" and container:
            msg = f"コンテナー「{container}」が見つかりません。"
        if code == "AuthenticationFailed" and "expired" in str(exc).lower():
            msg = "SAS の有効期限が切れています。"
        if msg is None:
            msg = f"Azure の要求が失敗しました（{code}）。"
        return BackendError(code, msg, status=status, retry_after=retry_after, request_id=request_id,
                            detail=redact(f"HTTP {status} {code} x-ms-request-id={request_id}"))
    info = network_message(exc, account=account, proxy=proxy)
    if info:
        return BackendError(info.code, info.message, detail=redact(f"{type(exc).__name__}"))
    if isinstance(exc, (ServiceRequestError, ServiceResponseError)):
        return BackendError("ConnectionFailed", "接続できませんでした。ネットワーク、プロキシ、ファイアウォールの設定を確認してください。",
                            detail=redact(f"{type(exc).__name__}"))
    return BackendError("UnknownError", "予期しないエラーが発生しました。詳しくはログを確認してください。",
                        detail=redact(f"{type(exc).__name__}: {exc}")[:500])


def msal_message(result: dict) -> ErrorInfo:
    """MSAL の結果（error、error_codes）の文。"""
    codes = [str(c) for c in (result.get("error_codes") or [])]
    for c in codes:
        if c in AADSTS:
            return ErrorInfo(f"AADSTS{c}", AADSTS[c])
    err = result.get("error") or "unknown"
    if err in ("interaction_required", "login_required", "consent_required"):
        return ErrorInfo(err, "このテナントへのサインインが必要です。")
    return ErrorInfo(err, "サインインに失敗しました。" + (f"（{err}）" if err else ""))
