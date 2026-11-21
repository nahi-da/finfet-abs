"""ジョブの Blob の表（4.6.2）: サーバーで絞り込み・並べ替え・ページ分け、1件の詳細、CSV（3.3.14）。"""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from ..core.errors import NotFound
from ..search.results import escape_like
from .service import ITEM_LABELS

SORTS = {
    "name": ("i.sort_key",), "status": ("i.status",), "tier": ("IFNULL(i.archive_status, i.tier)",), "size": ("i.size",),
    "rehydrate_requested": ("i.rehydrate_requested_at",), "rehydrate_completed": ("i.rehydrate_completed_detected_at",),
    "download_started": ("i.download_started_at",), "download_completed": ("i.download_completed_at",),
    "path": ("i.local_path",), "error": ("i.error_message",),
}


def where_of(job_id: str, f: dict) -> tuple[str, list]:
    where = ["i.job_id=?"]
    params: list = [job_id]
    for w in (f.get("q") or "").lower().split():
        where.append("i.blob_name_lower LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(w)}%")
    sts = [s for s in (f.get("status") or []) if s]
    if sts:
        where.append(f"i.status IN ({','.join('?' * len(sts))})")
        params += sts
    tiers = [t for t in (f.get("tier") or []) if t]
    if tiers:
        where.append(f"i.tier IN ({','.join('?' * len(tiers))})")
        params += tiers
    return " AND ".join(where), params


def url_of(r) -> str:
    """URL（SAS なし）は保存せず、ストレージアカウント・コンテナー・Blob 名から作ります（3.3.14）。"""
    return (f"https://{r['storage_account']}.blob.{r['endpoint_suffix']}/{quote(r['container'])}/"
            f"{quote(r['blob_name'], safe='/')}")


def public(r, progress: dict | None = None) -> dict:
    p = (progress or {}).get(r["id"])
    return {
        "id": r["id"], "storage_account": r["storage_account"], "endpoint_suffix": r["endpoint_suffix"],
        "container": r["container"], "blob_name": r["blob_name"], "url": url_of(r), "status": r["status"],
        "status_label": ITEM_LABELS.get(r["status"], r["status"]), "status_reason": r["status_reason"],
        "tier": r["tier"], "archive_status": r["archive_status"], "size": r["size"], "blob_type": r["blob_type"],
        "rehydrate_requested_at": r["rehydrate_requested_at"], "rehydrate_requested_known": r["rehydrate_requested_known"],
        "rehydrate_priority": r["rehydrate_priority"], "rehydrate_target_tier": r["rehydrate_target_tier"],
        "rehydrate_completed_detected_at": r["rehydrate_completed_detected_at"],
        "rehydrate_completed_azure_at": r["rehydrate_completed_azure_at"], "next_check_at": r["next_check_at"],
        "download_started_at": r["download_started_at"], "download_completed_at": r["download_completed_at"],
        "planned_path": r["planned_path"], "local_path": r["local_path"], "bytes_downloaded": r["bytes_downloaded"],
        "progress_bytes": p["bytes"] if p else None, "error_code": r["error_code"], "error_message": r["error_message"],
        "md5_result": r["md5_result"], "auth_kind": r["auth_kind"],
    }


def list_items(db, job_id: str, f: dict, sort: str, offset: int, limit: int, progress: dict | None = None) -> dict:
    where, params = where_of(job_id, f)
    desc = sort.startswith("-")
    cols = SORTS.get(sort.lstrip("-"), SORTS["name"])
    d = "DESC" if desc else "ASC"
    order = ", ".join(f"{c} {d}" for c in cols) + f", i.id {d}"
    total = db.scalar(f"SELECT COUNT(*) FROM job_items i WHERE {where}", params)
    rows = db.query(f"SELECT i.* FROM job_items i WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", params + [limit, offset])
    return {"total": total, "items": [public(r, progress) for r in rows]}


def detail(db, job_id: str, item_id: int) -> dict:
    r = db.query_one("SELECT * FROM job_items WHERE job_id=? AND id=?", (job_id, item_id))
    if r is None:
        raise NotFound("Blob が見つかりません。")
    out = {k: r[k] for k in r.keys() if k not in ("sort_key", "blob_name_lower", "ranges_done_json", "secret_id")}
    out["url"] = url_of(r)
    out["vars"] = json.loads(r["vars_json"]) if r["vars_json"] else None
    out["notes"] = json.loads(r["notes_json"]) if r["notes_json"] else []
    out.pop("vars_json", None)
    out.pop("notes_json", None)
    return out


def to_csv(db, job_id: str, f: dict) -> bytes:
    where, params = where_of(job_id, f)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    head = ["ストレージアカウント", "コンテナー", "Blob 名", "URL", "状態", "理由", "種類", "サイズ", "ETag", "Content-MD5",
            "作成日時", "最終更新日時", "検索時の層", "検索時のリハイドレートの状態", "層の変更日時",
            "リハイドレートの要求日時", "優先度", "復元先の層", "優先度を上げた日時", "完了を確認した日時", "完了時の層の変更日時（Azure）",
            "ダウンロードの開始日時", "最後に再開した日時", "ダウンロードの完了日時", "保存するパス", "保存したファイル",
            "取得したバイト数", "試行回数", "MD5 の照合", "エラーコード", "エラーの内容", "認証方式", "保存した認証情報の ID"]
    w.writerow(head)
    for r in db.query(f"SELECT i.* FROM job_items i WHERE {where} ORDER BY i.sort_key", params):
        requested = r["rehydrate_requested_at"] or ("不明（ジョブの開始前から要求済み）" if r["rehydrate_requested_known"] == 0 else "")
        w.writerow([r["storage_account"], r["container"], r["blob_name"], url_of(r), ITEM_LABELS.get(r["status"], r["status"]),
                    r["status_reason"] or "", r["blob_type"] or "", r["size"] if r["size"] is not None else "", r["etag"] or "",
                    r["content_md5"] or "", r["creation_time"] or "", r["last_modified"] or "", r["tier_at_search"] or "",
                    r["archive_status_at_search"] or "", r["access_tier_change_time"] or "", requested,
                    r["rehydrate_priority"] or "", r["rehydrate_target_tier"] or "", r["rehydrate_priority_raised_at"] or "",
                    r["rehydrate_completed_detected_at"] or "", r["rehydrate_completed_azure_at"] or "",
                    r["download_started_at"] or "", r["download_resumed_at"] or "", r["download_completed_at"] or "",
                    r["planned_path"] or "", r["local_path"] or "", r["bytes_downloaded"] or 0, r["attempts"] or 0,
                    r["md5_result"] or "", r["error_code"] or "", r["error_message"] or "", r["auth_kind"] or "",
                    r["credential_id"] or ""])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def read_log(path, tail: int = 2000, blob: str | None = None, level: str | None = None) -> list[dict]:
    """ジョブのログ（新しいものが下）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        parts = ln.rstrip("\n").split("\t", 3)
        if len(parts) != 4:
            continue
        at, lv, b, msg = parts
        if blob and blob.lower() not in b.lower():
            continue
        if level and lv != level:
            continue
        out.append({"at": at, "level": lv, "blob": b, "message": msg})
    return out[-tail:]
