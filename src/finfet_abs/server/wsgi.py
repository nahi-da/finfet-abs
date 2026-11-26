"""自前の WSGI アプリ（2.3）。安全対策（2.5）はルーターの前で1か所で行います。"""

from __future__ import annotations

import logging
import re
from typing import Callable

from ..core.errors import AppError
from .http import Request, Response, error_response, file_response, html_response, json_response, redirect, simple_page
from .router import Router
from .security import CSRF_HEADER, INSTANCE_HEADER, SECURITY_HEADERS, UNSAFE_METHODS, Security
from .static import StaticFiles

log = logging.getLogger(__name__)

#: ページの URL（4.2.2）。すべて index.html を返します。
PAGE_PATTERNS = [re.compile(p) for p in (
    r"^/$", r"^/search$", r"^/search/urls$", r"^/searches/[^/]+$",
    r"^/jobs$", r"^/jobs/ended$", r"^/jobs/[^/]+(?:/(?:condition|options|log))?$",
    r"^/history$", r"^/connections$", r"^/settings$",
)]

#: Cookie なしで返す静的ファイル（401 などの案内のページで使うもの）
PUBLIC_STATIC = {"/css/tokens.css", "/css/simple.css", "/favicon.svg"}


def is_page(path: str) -> bool:
    return any(p.match(path) for p in PAGE_PATTERNS)


class WsgiApp:
    """WSGI アプリ。"""

    def __init__(self, router: Router, security: Security, web: StaticFiles, docs: StaticFiles | None = None,
                 *, signin_return: Callable[[Request], Response] | None = None) -> None:
        self.router = router
        self.security = security
        self.web = web
        self.docs = docs
        self.signin_return = signin_return
        self.index = web.lookup("/index.html")

    def __call__(self, environ: dict, start_response):
        req = Request(environ)
        try:
            resp = self.handle(req)
        except AppError as err:
            resp = error_response(err)
        except Exception as exc:  # noqa: BLE001
            if _is_backend_error(exc):
                # Azure の操作の失敗（3.7）: 文とエラーコード、問い合わせ用の要求 ID を返します（詳細はログ）
                log.warning("Azure の操作に失敗しました: %s %s: %s（状態 %s、要求 ID %s）", req.method, req.path, exc.code,
                            exc.status, exc.request_id)
                # 画面の 401・403（本ツールの認証）と区別するため、状態コードは 502 にして、Azure の状態は detail に入れます
                resp = error_response(AppError(exc.code, exc.message, status=502,
                                               detail=f"Azure の状態コード: {exc.status}" if exc.status else "",
                                               extra={"request_id": exc.request_id} if exc.request_id else None))
            else:
                log.exception("要求の処理でエラーが発生しました: %s %s", req.method, req.path)
                resp = error_response(AppError("INTERNAL", "内部エラーが発生しました。詳細はログを確認してください。",
                                               status=500))
        for name, value in SECURITY_HEADERS:
            resp.set_header(name, value)
        if not req.path.startswith("/api/v1/events"):
            # パスだけを出します（クエリには秘密情報が含まれうるため出しません。3.6.2 の 6）
            log.debug("%s %s %s", req.method, req.path, resp.status)
        start_response(resp.status_line(), resp.headers)
        if req.method == "HEAD":
            return [b""]
        body = resp.body
        if isinstance(body, (bytes, bytearray)):
            return [bytes(body)]
        return body

    # ------------------------------------------------------------ 振り分け

    def handle(self, req: Request) -> Response:
        sec = self.security
        path = req.path
        is_api = path.startswith("/api/") or path.startswith("/internal/")

        # S5: DNS リバインディング
        if not sec.host_ok(req.header("host")):
            log.warning("Host が違うため拒否しました: %s %s（Host: %s）", req.method, path, req.header("host"))
            return Response(403, b"Forbidden", content_type="text/plain; charset=utf-8")

        # サインインの戻り（2.5 の例外。Cookie を求めない。S5〜S7 は行う）
        if req.method == "GET" and path == "/" and req.arg("code") is not None and req.arg("state") is not None:
            if self.signin_return is None:
                return simple_page("サインイン", "このサインインは取り消されたか、期限が切れています。", 400)
            return self.signin_return(req)

        # S4: ほかの Web サイトからの要求
        if not sec.origin_ok(req.header("origin"), req.header("sec-fetch-site")):
            # 拒否した理由を調べられるように、ヘッダーの値（秘密の値は含みません）を記録します
            log.warning("ほかのサイトからの要求として拒否しました: %s %s（Origin: %s、Sec-Fetch-Site: %s、Sec-Fetch-Mode: %s）",
                        req.method, path, req.header("origin"), req.header("sec-fetch-site"), req.header("sec-fetch-mode"))
            if is_api:
                return error_response(AppError("FORBIDDEN_ORIGIN", "この要求は受け付けられません。", status=403))
            return Response(403, b"Forbidden", content_type="text/plain; charset=utf-8")

        # 認証のいらないもの
        if path == "/api/v1/health" and req.method in ("GET", "HEAD"):
            return json_response({})
        if path == "/auth/launch" and req.method == "GET":
            return self._launch(req)
        if path == "/internal/launch-code":
            if req.method != "POST":
                return error_response(AppError("METHOD_NOT_ALLOWED", "許可されていない操作です。", status=405))
            if not sec.instance_ok(req.header(INSTANCE_HEADER)):
                return error_response(AppError("UNAUTHORIZED", "認証されていません。", status=401))
            return json_response({"code": sec.new_launch_code()})
        if path in PUBLIC_STATIC and req.method in ("GET", "HEAD"):
            found = self.web.lookup(path)
            if found:
                return file_response(*found)

        # S2: Cookie
        if not sec.session_ok(req.cookies.get(sec.cookie_name)):
            if is_api:
                return error_response(AppError("UNAUTHORIZED", "本ツールの画面を開き直してください。", status=401))
            return simple_page("FinFeT", "本ツールの画面を開くには、finfet-abs.bat を再度実行してください。", 401)

        # S4: CSRF
        if req.method in UNSAFE_METHODS and not sec.csrf_ok(req.header(CSRF_HEADER)):
            return error_response(AppError("CSRF", "この要求は受け付けられません（画面を再読み込みしてください）。", status=403))

        if is_api:
            route, params, path_matched = self.router.match(req.method, path)
            if route is None:
                if path_matched:
                    return error_response(AppError("METHOD_NOT_ALLOWED", "許可されていない操作です。", status=405))
                return error_response(AppError("NOT_FOUND", "見つかりません。", status=404))
            req.params = params
            return route.handler(req)

        if req.method not in ("GET", "HEAD"):
            return error_response(AppError("METHOD_NOT_ALLOWED", "許可されていない操作です。", status=405))
        found = self.web.lookup(path)
        if found is None and self.docs is not None:
            found = self.docs.lookup(path)
        if found is not None:
            return file_response(*found)
        if is_page(path) and self.index is not None:
            return file_response(*self.index)
        return simple_page("見つかりません", "このページは存在しません。", 404, ("/", "本ツールの画面を開く"))

    def _launch(self, req: Request) -> Response:
        """S3: 起動コードを Cookie に交換して `/` にリダイレクトします。"""
        if not self.security.consume_launch_code(req.arg("code")):
            return simple_page("FinFeT", "このページの URL は無効か、有効期限が切れています。finfet-abs.bat を再度実行してください。", 400)
        # next は決めた値のみ受け付けます（任意の URL へのリダイレクトはしません。チュートリアル 4.12）
        if req.arg("next") == "tutorial":
            # チュートリアルは、通常のモードの画面（別のポート）から開きます。Firefox 系のブラウザは、別のポートから開いた
            # URL の転送（302）の先を「ほかのサイトからの要求」として送るため、S4 で拒否されていました（v1.7.4）。
            # 転送せず、Cookie を渡すページから、同じサイトの中で移ります（meta refresh。スクリプトは使いません）
            r = html_response('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
                              '<meta http-equiv="refresh" content="0;url=/search?tutorial=1"><title>FinFeT</title>'
                              '<link rel="stylesheet" href="/css/tokens.css"><link rel="stylesheet" href="/css/simple.css"></head>'
                              '<body><main><p>チュートリアルを開いています…</p>'
                              '<p><a href="/search?tutorial=1">開かない場合は、ここをクリックしてください</a></p></main></body></html>')
            r.headers.append(self.security.session_cookie_header())
            return r
        return redirect("/", [self.security.session_cookie_header()])


def _is_backend_error(exc: Exception) -> bool:
    """azure.backend.BackendError か（server は azure に依存しないため、属性で判定します）。"""
    return all(hasattr(exc, a) for a in ("code", "message", "status", "request_id")) and isinstance(exc.code, str)
