"""プロキシと HTTP のセッション（3.1.11）。

Azure SDK・MSAL・ARM・料金の取得のすべてに、同じ設定の `requests.Session` を渡します。
- 自動: 環境変数（大文字・小文字）、なければ Windows のインターネット設定（手動で指定したプロキシ）
- 指定する: プロキシの URL と使わない宛先、ID とパスワード（Basic 認証）
- 使わない: 直接接続
`127.0.0.1`・`localhost` への接続は、常にプロキシを使いません。
"""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
import os
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter

from ..core.secret import Secret, redact

log = logging.getLogger(__name__)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass
class ProxyConfig:
    """プロキシの設定。"""

    mode: str = "auto"  # auto / manual / none
    url: str = ""
    bypass: str = ""
    username: str = ""
    password: Secret | None = None
    ca_bundle: str = ""
    env: dict[str, str] | None = None  # テスト用（None なら os.environ）
    registry: Callable[[], tuple[dict, Callable[[str], bool]]] | None = None  # テスト用

    def describe(self) -> str:
        return {"auto": "自動", "manual": "指定する", "none": "使わない"}.get(self.mode, self.mode)


def _env_lookup(env: dict[str, str], name: str) -> str | None:
    # 小文字を優先します（curl・requests と同じ）
    return env.get(name.lower()) or env.get(name.upper()) or None


def _host_matches(host: str, entries: list[str]) -> bool:
    host = host.lower().strip("[]")
    for raw in entries:
        e = raw.strip().lower()
        if not e:
            continue
        if e == "*":
            return True
        if "/" in e:
            try:
                if ipaddress.ip_address(host) in ipaddress.ip_network(e, strict=False):
                    return True
            except ValueError:
                pass
            continue
        e2 = e.lstrip("*").lstrip(".")
        if host == e2 or host.endswith("." + e2) or fnmatch.fnmatch(host, e):
            return True
    return False


def _registry_proxies() -> tuple[dict, Callable[[str], bool]]:
    getreg = getattr(urllib.request, "getproxies_registry", None)
    bypass = getattr(urllib.request, "proxy_bypass_registry", None)
    if getreg is None:
        return {}, lambda h: False
    try:
        return getreg(), (lambda h: bool(bypass(h)) if bypass else False)
    except Exception:  # noqa: BLE001
        return {}, lambda h: False


def resolve_proxy(cfg: ProxyConfig, url: str) -> str | None:
    """宛先の URL に使うプロキシの URL（直接なら None）。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    scheme = parts.scheme.lower() or "https"
    if host in LOCAL_HOSTS or cfg.mode == "none":
        return None
    if cfg.mode == "manual":
        if not cfg.url:
            return None
        if _host_matches(host, cfg.bypass.replace(";", ",").split(",")):
            return None
        return with_credentials(cfg.url, cfg.username, cfg.password)
    env = cfg.env if cfg.env is not None else dict(os.environ)
    has_env = any(_env_lookup(env, n) for n in ("https_proxy", "http_proxy", "all_proxy"))
    if has_env:
        no_proxy = _env_lookup(env, "no_proxy") or ""
        if _host_matches(host, no_proxy.split(",")):
            return None
        return _env_lookup(env, f"{scheme}_proxy") or _env_lookup(env, "all_proxy") or \
            (_env_lookup(env, "http_proxy") if scheme == "https" else None)
    proxies, bypass = (cfg.registry or _registry_proxies)()
    if not proxies:
        return None
    if bypass(host):
        return None
    return proxies.get(scheme) or proxies.get("https") or proxies.get("http")


def with_credentials(url: str, username: str, password: Secret | None) -> str:
    """プロキシの URL に ID とパスワードを付けます（Basic 認証）。"""
    if not username:
        return url
    parts = urlsplit(url if "://" in url else "http://" + url)
    pw = quote(password.reveal(), safe="") if password else ""
    netloc = f"{quote(username, safe='')}:{pw}@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{netloc}{parts.path}"


def proxy_label(proxy_url: str | None) -> str:
    """表示用（ID とパスワードを出しません）。"""
    if not proxy_url:
        return "直接"
    p = urlsplit(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    return f"プロキシ {p.hostname}" + (f":{p.port}" if p.port else "")


def build_ca_bundle(extra_file: str, out_dir: Path) -> str | bool:
    """既定の証明書（certifi）に、追加の CA 証明書（PEM）を加えたファイルを作り、そのパスを返します。"""
    import certifi
    if not extra_file:
        return certifi.where()
    p = Path(os.path.expandvars(extra_file))
    text = p.read_text(encoding="ascii", errors="strict")
    if "-----BEGIN CERTIFICATE-----" not in text:
        raise ValueError("追加の CA 証明書のファイルが PEM 形式ではありません。")
    out = Path(out_dir) / "ca-bundle.pem"
    out.write_text(Path(certifi.where()).read_text(encoding="ascii") + "\n" + text, encoding="ascii")
    return str(out)


class ProxySession(requests.Session):
    """設定のプロキシと証明書を、すべての要求に使うセッション（環境変数の REQUESTS_CA_BUNDLE なども反映します）。"""

    def __init__(self, cfg: ProxyConfig, verify: str | bool, pool_size: int = 16) -> None:
        super().__init__()
        self.cfg = cfg
        self.trust_env = False
        self.verify = verify
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def merge_environment_settings(self, url, proxies, stream, verify, cert):
        proxy = resolve_proxy(self.cfg, url)
        proxies = {"http": proxy, "https": proxy} if proxy else {}
        return {"proxies": proxies, "stream": stream, "verify": self.verify if verify is None else verify,
                "cert": cert}


@dataclass
class NetworkManager:
    """設定から作ったセッションを持ちます。設定が変わったら作り直します。"""

    settings: object
    data_dir: Path
    _session: ProxySession | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _listeners: list = field(default_factory=list)
    error: str | None = None

    def config(self) -> ProxyConfig:
        s = self.settings
        env = dict(os.environ)
        ca = s.get("network.ca_bundle") or _env_lookup(env, "requests_ca_bundle") or ""
        pw = None
        try:
            pw = s.get_secret("network.proxy.password")
        except Exception:  # noqa: BLE001
            log.warning("プロキシのパスワードを復号できません")
        return ProxyConfig(mode=s.get("network.proxy.mode"), url=s.get("network.proxy.url"),
                           bypass=s.get("network.proxy.bypass"), username=s.get("network.proxy.username"),
                           password=pw, ca_bundle=ca)

    def session(self) -> ProxySession:
        with self._lock:
            if self._session is None:
                cfg = self.config()
                try:
                    verify = build_ca_bundle(cfg.ca_bundle, self.data_dir)
                    self.error = None
                except Exception as exc:  # noqa: BLE001
                    log.warning("追加の CA 証明書を読み込めません: %s", redact(str(exc)))
                    self.error = "追加の CA 証明書のファイルを読み込めません。設定の「追加の CA 証明書」を確認してください。"
                    import certifi
                    verify = certifi.where()
                pool = int(self.settings.get("transfer.max_connections")) + 8
                self._session = ProxySession(cfg, verify, pool)
            return self._session

    def reset(self) -> None:
        """設定の変更を反映します（次の要求から新しいセッションを使います）。"""
        with self._lock:
            self._session = None
        for cb in list(self._listeners):
            cb()

    def on_reset(self, cb) -> None:
        self._listeners.append(cb)
