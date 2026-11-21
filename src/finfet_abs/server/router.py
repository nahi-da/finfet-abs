"""ルーター: `(メソッド, パスの正規表現) → 関数` の表で振り分けます（2.3）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .http import Request, Response

Handler = Callable[[Request], Response]


@dataclass
class Route:
    method: str
    pattern: re.Pattern
    handler: Handler
    template: str


class Router:
    """API の振り分けの表。"""

    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(self, method: str, template: str, handler: Handler) -> None:
        """`/api/v1/jobs/{id}` の形で登録します。`{名前}` は `/` を含まない1階層です。"""
        regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template) + "$"
        self.routes.append(Route(method.upper(), re.compile(regex), handler, template))

    def get(self, template: str):
        return self._decorator("GET", template)

    def post(self, template: str):
        return self._decorator("POST", template)

    def put(self, template: str):
        return self._decorator("PUT", template)

    def patch(self, template: str):
        return self._decorator("PATCH", template)

    def delete(self, template: str):
        return self._decorator("DELETE", template)

    def _decorator(self, method: str, template: str):
        def deco(fn: Handler) -> Handler:
            self.add(method, template, fn)
            return fn
        return deco

    def match(self, method: str, path: str) -> tuple[Route | None, dict[str, str], bool]:
        """(一致したルート, パス引数, パスだけは一致したか) を返します。"""
        path_matched = False
        for r in self.routes:
            m = r.pattern.match(path)
            if m:
                path_matched = True
                if r.method == method or (method == "HEAD" and r.method == "GET"):
                    # PATH_INFO はデコード済みのため、ここではデコードしません
                    return r, dict(m.groupdict()), True
        return None, {}, path_matched
