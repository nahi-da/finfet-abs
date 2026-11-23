"""設定（5.4）。既定値は、このファイルのスキーマの1か所だけで定義します（7.4 の I12）。"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import dpapi
from .errors import AppError
from .secret import Secret

log = logging.getLogger(__name__)

#: 既定のクライアント ID（Azure CLI の公開クライアント。3.1.2.1）
DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


@dataclass(frozen=True)
class Field:
    """設定の1項目。"""

    key: str
    kind: str  # bool / int / float / str / choice / secret / dict
    default: Any
    min: float | None = None
    max: float | None = None
    choices: tuple = field(default_factory=tuple)
    max_len: int = 2048


def _b(key: str, default: bool) -> Field:
    return Field(key, "bool", default)


def _i(key: str, default: int, lo: int, hi: int) -> Field:
    return Field(key, "int", default, lo, hi)


def _c(key: str, default: str, *choices: str) -> Field:
    return Field(key, "choice", default, choices=tuple(choices))


def _s(key: str, default: str = "", max_len: int = 2048) -> Field:
    return Field(key, "str", default, max_len=max_len)


SCHEMA: list[Field] = [
    # 画面
    _c("ui.start_page", "jobs", "jobs", "search"),
    _b("ui.browser_notifications", False),
    _c("ui.blob_name_display", "full", "full", "common", "tail"),
    # v1.4.0: テーマ（dark・light・system）と文字の大きさ
    _c("ui.theme", "dark", "dark", "light", "system"),
    _c("ui.font_size", "normal", "normal", "large"),
    Field("ui.tables", "dict", {}),
    # 検索
    _c("search.input_timezone", "local", "local", "utc"),
    _i("search.merge_trial_pages", 1, 1, 10),
    _i("search.max_prefixes", 32, 1, 256),
    _i("search.max_results", 300000, 1, 300000),
    _i("search.list_concurrency", 4, 1, 16),
    _b("search.exclude_adls_directories", True),
    _i("search.keep_recent", 10, 1, 100),
    # 取得
    _c("fetch.method", "rehydrate_download", "rehydrate_download", "rehydrate_only", "download_only"),
    _s("fetch.directory", "%USERPROFILE%\\Downloads"),
    _b("fetch.job_folder", True),
    _c("fetch.path_structure", "preserve", "preserve", "flatten", "name_only", "template"),
    _c("fetch.conflict", "number", "number", "skip", "overwrite"),
    _b("fetch.set_mtime", True),
    _b("fetch.verify_md5", True),
    # ダウンロードの後の圧縮ファイルの展開（依頼者の意見。v1.3.0）
    _b("fetch.extract_archives", False),
    _i("fetch.extract_max_gib", 10, 1, 1024),
    _s("fetch.path_template", ""),
    _i("fetch.reuse_search_info_minutes", 10, 0, 1440),
    # リハイドレート
    _c("rehydrate.target_tier", "Hot", "Hot", "Cool", "Cold"),
    _c("rehydrate.priority", "Standard", "Standard", "High"),
    _i("rehydrate.poll_first_minutes.high", 15, 1, 1440),
    _i("rehydrate.poll_first_minutes.standard", 60, 1, 1440),
    _i("rehydrate.poll_interval_minutes.high", 10, 1, 1440),
    _i("rehydrate.poll_interval_minutes.standard", 30, 1, 1440),
    _b("rehydrate.check_on_startup", True),
    # キュー
    _i("queue.concurrent_jobs", 1, 1, 4),
    _b("queue.start_immediately", True),
    _b("queue.resume_on_startup", True),
    # 画面をすべて閉じた後の自動の終了（依頼者の意見。v1.3.0）
    _b("exit.when_closed", True),
    _i("exit.wait_minutes", 1, 1, 1440),
    # 負荷（3.4.5）
    _i("transfer.max_connections", 8, 1, 32),
    _i("transfer.check_concurrency", 4, 1, 16),
    _i("transfer.rehydrate_concurrency", 4, 1, 16),
    _i("transfer.download_concurrency", 3, 1, 16),
    _i("transfer.connections_per_blob", 2, 1, 8),
    _i("transfer.chunk_size_mib", 8, 1, 64),
    Field("transfer.bandwidth_mbps", "float", 0.0, 0, 10000),
    _i("transfer.request_rate", 50, 1, 1000),
    _i("transfer.retry_total", 5, 0, 10),
    _i("transfer.connect_timeout", 20, 1, 300),
    _i("transfer.read_timeout", 120, 1, 3600),
    _c("load.process_priority", "below_normal", "normal", "below_normal", "background"),
    _b("load.prevent_sleep_downloading", True),
    _b("load.prevent_sleep_waiting", False),
    # 認証
    _s("auth.client_id", DEFAULT_CLIENT_ID, 64),
    _s("auth.default_tenant", "organizations", 128),
    _c("auth.default_entra_mode", "user_account", "user_account", "access_key"),
    _i("auth.sas_expiry_warn_hours", 24, 1, 720),
    # プロキシ（3.1.11）
    _c("network.proxy.mode", "auto", "auto", "manual", "none"),
    _s("network.proxy.url", ""),
    _s("network.proxy.bypass", ""),
    _s("network.proxy.username", "", 256),
    Field("network.proxy.password", "secret", ""),
    _s("network.ca_bundle", ""),
    # 料金（3.8。M9 で画面に出します）
    _b("pricing.online", True),
    _c("pricing.currency", "JPY", "JPY", "USD"),
    # データとログ
    _i("history.max_entries", 200, 10, 2000),
    _c("log.level", "INFO", "DEBUG", "INFO", "WARNING", "ERROR"),
    _i("log.max_files", 30, 1, 365),
    # コンソール（本ツールを起動したウィンドウ）へのログの表示（依頼者の依頼。v1.5.0）
    _b("log.console", True),
    _c("log.console_level", "INFO", "DEBUG", "INFO", "WARNING", "ERROR"),
    # デモモード（8.3.3。デモモードのときだけ画面に出します）
    _i("demo.blob_count", 30000, 1000, 300000),
    _i("demo.time_scale", 60, 1, 3600),
    _i("demo.download_mbps", 50, 1, 10000),
    _i("demo.error_percent", 0, 0, 50),
    # 画面には出さない項目
    _i("server.port", 0, 0, 65535),
    _b("dev.allow_development_storage", False),
]

FIELDS: dict[str, Field] = {f.key: f for f in SCHEMA}


def defaults() -> dict[str, Any]:
    """既定値（平らなキー）。"""
    return {f.key: copy.deepcopy(f.default) for f in SCHEMA}


def _validate(f: Field, value: Any) -> Any:
    """値を検査して正規化します。誤りなら ValueError。"""
    if f.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("オン・オフで指定してください。")
        return value
    if f.kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("数値で指定してください。")
        try:
            v = int(str(value).strip())
        except ValueError:
            raise ValueError("整数で指定してください。") from None
        if (f.min is not None and v < f.min) or (f.max is not None and v > f.max):
            raise ValueError(f"{int(f.min):,}〜{int(f.max):,} の範囲で指定してください。")
        return v
    if f.kind == "float":
        if isinstance(value, bool):
            raise ValueError("数値で指定してください。")
        try:
            v = float(str(value).strip())
        except ValueError:
            raise ValueError("数値で指定してください。") from None
        if v != v or (f.min is not None and v < f.min) or (f.max is not None and v > f.max):
            raise ValueError(f"{f.min:g}〜{f.max:,g} の範囲で指定してください。")
        return v
    if f.kind == "choice":
        if value not in f.choices:
            raise ValueError("選択肢から選んでください。")
        return value
    if f.kind in ("str", "secret"):
        if not isinstance(value, str):
            raise ValueError("文字列で指定してください。")
        if len(value) > (f.max_len if f.kind == "str" else 8192):
            raise ValueError("長すぎます。")
        return value
    if f.kind == "dict":
        if not isinstance(value, dict):
            raise ValueError("形式が正しくありません。")
        if len(json.dumps(value)) > 64 * 1024:
            raise ValueError("大きすぎます。")
        return value
    raise ValueError("unknown kind")


def _flatten(obj: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if key in FIELDS:
            out[key] = v
        elif isinstance(v, dict):
            out.update(_flatten(v, key + "."))
    return out


def _nest(flat: dict[str, Any]) -> dict:
    out: dict = {}
    for key in (f.key for f in SCHEMA):
        if key not in flat:
            continue
        parts = key.split(".")
        d = out
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = flat[key]
    return out


class Settings:
    """設定の読み込み・検査・保存。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._values = defaults()
        self.load_warning: str | None = None
        self._listeners: list[Callable[[list[str]], None]] = []

    # ------------------------------------------------------------ 読み込み・保存

    def load(self) -> None:
        """ファイルを読み込み、既定値とマージします。読み込めない場合は退避して既定値で起動します。"""
        with self._lock:
            values = defaults()
            need_write = True
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict):
                        raise ValueError("not an object")
                    flat = _flatten(raw)
                    need_write = False
                    for key, f in FIELDS.items():
                        if key not in flat:
                            need_write = True
                            continue
                        try:
                            values[key] = _validate(f, flat[key])
                        except ValueError:
                            log.warning("設定の値が正しくないため、既定値を使います: %s", key)
                            need_write = True
                except Exception:  # noqa: BLE001
                    backup = self.path.with_name(self.path.name + ".bak")
                    try:
                        os.replace(self.path, backup)
                    except OSError:
                        pass
                    self.load_warning = ("設定のファイルを読み込めなかったため、既定値で起動しました。"
                                         f"元のファイルは {backup.name} に退避しました。")
                    log.warning("設定のファイルを読み込めないため、退避して既定値で起動します")
                    values = defaults()
                    need_write = True
            self._values = values
            if need_write:
                self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(_nest(self._values), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ 参照

    def get(self, key: str) -> Any:
        """値を返します（秘密の項目は保存の形式の文字列のまま）。"""
        with self._lock:
            return copy.deepcopy(self._values[key])

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def snapshot(self) -> dict[str, Any]:
        """すべての値の写し（平らなキー）。"""
        with self._lock:
            return copy.deepcopy(self._values)

    def public(self) -> dict[str, Any]:
        """API で返す値。秘密の項目は値を返さず、設定済みかどうかだけを返します（2.5 の S8）。"""
        out = {}
        for key, f in FIELDS.items():
            if f.kind == "secret":
                out[key + "_set"] = bool(self._values[key])
            else:
                out[key] = copy.deepcopy(self._values[key])
        return out

    def get_secret(self, key: str) -> Secret | None:
        """秘密の項目を復号して返します。設定されていなければ None。"""
        text = self.get(key)
        if not text:
            return None
        return dpapi.unseal(text, "proxy")

    def fetch_directory(self) -> str:
        """保存先（環境変数を展開したもの）。"""
        return os.path.expandvars(self.get("fetch.directory"))

    # ------------------------------------------------------------ 変更

    def on_change(self, callback: Callable[[list[str]], None]) -> None:
        """設定が変わったときに呼ぶ関数を登録します。"""
        self._listeners.append(callback)

    def update(self, changes: dict[str, Any]) -> list[str]:
        """値を検査して保存します。誤りがあれば何も変えずに AppError（項目ごとの誤り）を出します。"""
        errors: dict[str, str] = {}
        new: dict[str, Any] = {}
        for key, value in changes.items():
            f = FIELDS.get(key)
            if f is None:
                errors[key] = "知らない項目です。"
                continue
            try:
                v = _validate(f, value)
            except ValueError as exc:
                errors[key] = str(exc)
                continue
            if f.kind == "secret":
                v = dpapi.seal(Secret(v), "proxy") if v else ""
            new[key] = v
        if errors:
            raise AppError("SETTINGS_INVALID", "設定の値に誤りがあります。", extra={"fields": errors})
        with self._lock:
            changed = [k for k, v in new.items() if self._values.get(k) != v]
            self._values.update(new)
            if changed:
                self._save()
        if changed:
            self._notify(changed)
        return changed

    def reset(self) -> list[str]:
        """すべての項目を既定値に戻します。"""
        with self._lock:
            old = self._values
            self._values = defaults()
            changed = [k for k in FIELDS if old.get(k) != self._values[k]]
            self._save()
        if changed:
            self._notify(changed)
        return changed

    def _notify(self, changed: list[str]) -> None:
        for cb in list(self._listeners):
            try:
                cb(changed)
            except Exception:  # noqa: BLE001
                log.exception("設定の変更の通知でエラーが発生しました")
