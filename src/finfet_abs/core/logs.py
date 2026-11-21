"""ログと伏せ字（3.6）。"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .secret import redact

#: SDK などのロガー（WARNING 以上にする。3.6.2 の 2）
QUIET_LOGGERS = ("azure", "msal", "msal_extensions", "urllib3", "waitress", "requests", "charset_normalizer")

LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] [%(category)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class RedactingFilter(logging.Filter):
    """メッセージと例外の文字列を伏せ字にするフィルター（3.6.2 の 1・3）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        name = record.name
        if not hasattr(record, "category"):
            parts = name.split(".")
            record.category = parts[1] if parts[0] == "finfet_abs" and len(parts) > 1 else parts[0]
        return True


class DailyFileHandler(logging.Handler):
    """日ごとのファイル（`app-YYYYMMDD.log`）に書き込むハンドラー。古いファイルを削除します。"""

    def __init__(self, directory: Path, max_files: int = 30) -> None:
        super().__init__()
        self.directory = Path(directory)
        self.max_files = max_files
        self._day = ""
        self._stream = None
        self._lock2 = threading.Lock()

    def _open(self) -> None:
        day = time.strftime("%Y%m%d")
        if day == self._day and self._stream:
            return
        if self._stream:
            self._stream.close()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stream = open(self.directory / f"app-{day}.log", "a", encoding="utf-8", newline="\n")
        self._day = day
        self._cleanup()

    def _cleanup(self) -> None:
        files = sorted(p for p in self.directory.glob("app-*.log") if re.fullmatch(r"app-\d{8}\.log", p.name))
        for p in files[:-self.max_files] if len(files) > self.max_files else []:
            try:
                p.unlink()
            except OSError:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with self._lock2:
                self._open()
                self._stream.write(line + "\n")
                self._stream.flush()
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def close(self) -> None:
        with self._lock2:
            if self._stream:
                self._stream.close()
                self._stream = None
        super().close()


_file_handler: DailyFileHandler | None = None


def setup(logs_dir: Path | None, level: str = "INFO", max_files: int = 30) -> None:
    """ログを初期化します。logs_dir が None ならファイルに出しません（テスト用）。"""
    global _file_handler
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(logging.DEBUG)
    if logs_dir is None:
        root.addHandler(logging.NullHandler())
    else:
        handler = DailyFileHandler(logs_dir, max_files)
        handler.addFilter(RedactingFilter())
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        handler.setLevel(getattr(logging, level, logging.INFO))
        root.addHandler(handler)
        _file_handler = handler
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def set_level(level: str) -> None:
    """ログのレベルを変えます。"""
    if _file_handler is not None:
        _file_handler.setLevel(getattr(logging, level, logging.INFO))


def set_max_files(n: int) -> None:
    """ログを保存する数を変えます。"""
    if _file_handler is not None:
        _file_handler.max_files = n


class JobLog:
    """ジョブのログ（`logs\\jobs\\<ジョブID>.log`。3.6.1）。"""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
        self._filter = RedactingFilter()
        self._level = logging.INFO

    def set_level(self, level: str) -> None:
        self._level = getattr(logging, level, logging.INFO)

    def path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.log"

    def write(self, job_id: str, level: int, message: str) -> None:
        """ジョブのログに1行書きます。アプリのログにも DEBUG で出します。"""
        logging.getLogger("finfet_abs.jobs").log(logging.DEBUG, "[%s] %s", job_id[:8], message)
        if level < self._level:
            return
        record = logging.LogRecord("finfet_abs.jobs", level, "", 0, message, None, None)
        self._filter.filter(record)
        line = self._fmt.format(record)
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with open(self.path(job_id), "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")

    def read(self, job_id: str, tail: int = 2000) -> list[str]:
        """ジョブのログの末尾を返します。"""
        p = self.path(job_id)
        if not p.exists():
            return []
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return lines[-tail:]

    def delete(self, job_id: str) -> None:
        try:
            os.remove(self.path(job_id))
        except OSError:
            pass


def local_time_str(dt: datetime) -> str:
    """ログ用のローカル時刻の文字列。"""
    return dt.astimezone().strftime(DATE_FORMAT)
