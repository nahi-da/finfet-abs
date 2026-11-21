"""要求と応答の型（2.3 の自前の WSGI アプリ）。"""

from __future__ import annotations

import json
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote

from ..core.errors import AppError, bad_request

MAX_JSON_BODY = 10 * 1024 * 1024

STATUS_TEXT = {
    200: "OK", 201: "Created", 202: "Accepted", 204: "No Content", 302: "Found", 304: "Not Modified",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    409: "Conflict", 413: "Payload Too Large", 415: "Unsupported Media Type", 500: "Internal Server Error",
    503: "Service Unavailable",
}


class Request:
    """1つの HTTP の要求。"""

    def __init__(self, environ: dict) -> None:
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        raw_path = environ.get("PATH_INFO", "") or "/"
        try:
            self.path = raw_path.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            self.path = raw_path
        self.query_string = environ.get("QUERY_STRING", "")
        self._query = parse_qs(self.query_string, keep_blank_values=True)
        self.headers: dict[str, str] = {}
        for k, v in environ.items():
            if k.startswith("HTTP_"):
                self.headers[k[5:].replace("_", "-").lower()] = v
        if "CONTENT_TYPE" in environ:
            self.headers["content-type"] = environ["CONTENT_TYPE"]
        if "CONTENT_LENGTH" in environ:
            self.headers["content-length"] = environ["CONTENT_LENGTH"]
        self.params: dict[str, str] = {}
        self._body: bytes | None = None
        self.cookies: dict[str, str] = {}
        cookie = self.headers.get("cookie")
        if cookie:
            try:
                sc = SimpleCookie()
                sc.load(cookie)
                self.cookies = {k: m.value for k, m in sc.items()}
            except CookieError:
                self.cookies = {}

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def arg(self, name: str, default: str | None = None) -> str | None:
        """クエリの値（最初のもの）。"""
        v = self._query.get(name)
        return v[0] if v else default

    def args(self, name: str) -> list[str]:
        return list(self._query.get(name, []))

    def int_arg(self, name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
        v = self.arg(name)
        if v is None or v == "":
            return default
        try:
            n = int(v)
        except ValueError:
            raise bad_request(f"{name} は整数で指定してください。") from None
        if lo is not None:
            n = max(lo, n)
        if hi is not None:
            n = min(hi, n)
        return n

    def body(self) -> bytes:
        if self._body is None:
            try:
                length = int(self.environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length > MAX_JSON_BODY:
                raise AppError("BODY_TOO_LARGE", "要求が大きすぎます。", status=413)
            self._body = self.environ["wsgi.input"].read(length) if length > 0 else b""
        return self._body

    def json(self) -> Any:
        """本文の JSON。空なら {}。"""
        data = self.body()
        if not data:
            return {}
        ctype = (self.header("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise AppError("UNSUPPORTED_MEDIA_TYPE", "JSON で送ってください。", status=415)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise bad_request("JSON の形式が正しくありません。") from None


class Response:
    """1つの HTTP の応答。"""

    def __init__(self, status: int = 200, body: bytes | Iterable[bytes] = b"",
                 headers: list[tuple[str, str]] | None = None, content_type: str | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = list(headers or [])
        if content_type:
            self.headers.append(("Content-Type", content_type))

    def set_header(self, name: str, value: str) -> None:
        self.headers = [(k, v) for k, v in self.headers if k.lower() != name.lower()]
        self.headers.append((name, value))

    def status_line(self) -> str:
        return f"{self.status} {STATUS_TEXT.get(self.status, 'Status')}"


def json_response(obj: Any, status: int = 200) -> Response:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(status, data, content_type="application/json; charset=utf-8")


def error_response(err: AppError) -> Response:
    return json_response(err.to_json(), err.status)


def html_response(text: str, status: int = 200) -> Response:
    return Response(status, text.encode("utf-8"), content_type="text/html; charset=utf-8")


def redirect(location: str, headers: list[tuple[str, str]] | None = None) -> Response:
    r = Response(302, b"", headers)
    r.set_header("Location", location)
    return r


def file_response(path: Path, content_type: str) -> Response:
    return Response(200, Path(path).read_bytes(), content_type=content_type)


def download_response(data: bytes, filename: str, content_type: str) -> Response:
    """ブラウザのダウンロードとして保存させる応答。"""
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(200, data, [("Content-Disposition", disp)], content_type=content_type)


def simple_page(title: str, message: str, status: int = 200, link: tuple[str, str] | None = None) -> Response:
    """スクリプトを使わない、静的な案内のページ。"""
    from html import escape
    link_html = f'<p><a href="{escape(link[0])}">{escape(link[1])}</a></p>' if link else ""
    text = (
        "<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
        f"<title>{escape(title)} - FinFeT</title>"
        "<link rel=\"stylesheet\" href=\"/css/tokens.css\"><link rel=\"stylesheet\" href=\"/css/simple.css\"></head>"
        f"<body><main><h1>{escape(title)}</h1><p>{escape(message)}</p>{link_html}</main></body></html>"
    )
    return html_response(text, status)
