"""本ツールのエラー（5.7.1 のエラーの応答の元）。"""

from __future__ import annotations


class AppError(Exception):
    """画面に表示できるエラー。

    - code: エラーコード（`SAS_EXPIRED` など）
    - message: 画面に表示する文（秘密情報を含めない）
    - detail: 問い合わせ用の詳細（秘密情報を含めない）
    - status: HTTP の状態コード
    """

    def __init__(self, code: str, message: str, *, detail: str = "", status: int = 400,
                 extra: dict | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail
        self.status = status
        self.extra = extra or {}

    def to_json(self) -> dict:
        """API の応答の形にします。"""
        err = {"code": self.code, "message": self.message}
        if self.detail:
            err["detail"] = self.detail
        if self.extra:
            err.update(self.extra)
        return {"error": err}


class NotFound(AppError):
    """見つからない。"""

    def __init__(self, message: str = "見つかりません。", code: str = "NOT_FOUND") -> None:
        super().__init__(code, message, status=404)


class Conflict(AppError):
    """状態が合わない。"""

    def __init__(self, message: str, code: str = "CONFLICT", **kw) -> None:
        super().__init__(code, message, status=409, **kw)


def bad_request(message: str, code: str = "BAD_REQUEST", **kw) -> AppError:
    """入力の誤り。"""
    return AppError(code, message, status=400, **kw)
