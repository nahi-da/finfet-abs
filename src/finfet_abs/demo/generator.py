"""デモモードの架空のストレージアカウント・コンテナー・Blob（8.3.3）。

7.2 の形式の Blob 名を作ります。名前はすべて架空のもの（contoso、stexample01 など）です。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from ..azure.backend import BlobInfo
from .memory import MemoryBackend

SUB = "00000000-0000-0000-0000-000000000000"
TENANT = "00000000-0000-0000-0000-000000000001"

#: (アカウント, ドメイン, サブスクリプションの名前, リソースグループ, リージョン)
ACCOUNTS = [
    ("stexample01", "core.windows.net", "Production", "rg-example", "japaneast"),
    ("stexample02", "core.windows.net", "Production", "rg-example", "japaneast"),
    ("stexample03", "core.windows.net", "Development", "rg-dev", "japanwest"),
    ("stexample04", "core.chinacloudapi.cn", "Contoso China", "rg-cn", "chinaeast2"),
]

GB = 1024 ** 3


def _tier(rnd: random.Random) -> str:
    r = rnd.random()
    if r < 0.6:
        return "Archive"
    if r < 0.8:
        return "Cool"
    if r < 0.95:
        return "Hot"
    return "Cold"


def _blob(rnd: random.Random, name: str, created: datetime, *, size: int | None = None, tier: str | None = None,
          writing: bool = False) -> BlobInfo:
    tier = tier or _tier(rnd)
    size = size if size is not None else int(min(200 * 1024 * 1024, max(200, rnd.lognormvariate(11, 2))))
    archive_status = None
    priority = None
    if tier == "Archive" and rnd.random() < 0.03:
        archive_status = "rehydrate-pending-to-hot"
        priority = rnd.choice(["Standard", "Standard", "High"])
    modified = datetime.now(timezone.utc) - timedelta(minutes=rnd.randint(1, 20)) if writing else \
        created + timedelta(minutes=59, seconds=rnd.randint(0, 600))
    return BlobInfo(
        name=name, size=size, blob_type="AppendBlob" if writing else "BlockBlob", tier=None if writing else tier,
        archive_status=archive_status, rehydrate_priority=priority, etag=f'"0x8DD{rnd.getrandbits(40):010X}"',
        content_md5=None, creation_time=created, last_modified=modified,
        access_tier_change_time=created + timedelta(days=rnd.randint(0, 3)) if tier != "Hot" else None)


def populate(backend: MemoryBackend, total: int = 30000, *, now: datetime | None = None, seed: int = 20261120) -> dict:
    """架空のデータを作ります。戻り値は概要（コンテナーごとの件数）。"""
    rnd = random.Random(seed)
    now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    counts: dict[str, int] = {}

    def add(account: str, container: str, blobs: list[BlobInfo], suffix: str = "core.windows.net") -> None:
        backend.add_blobs(account, container, blobs, suffix)
        key = f"{account}/{container}"
        counts[key] = counts.get(key, 0) + len(blobs)

    hours = max(48, int(total * 0.1))
    # Log Analytics のエクスポート（5 分ごと。件数の半分をここに置きます）
    blobs = []
    k = 0
    while len(blobs) < max(100, total // 2):
        t = now - timedelta(minutes=5 * k)
        for n in range(1 + k % 3):
            suffix = "" if n == 0 else f"_{n}"
            blobs.append(_blob(rnd, f"WorkspaceResourceId=/subscriptions/{SUB}/resourcegroups/rg-example/providers/"
                                    f"microsoft.operationalinsights/workspaces/law-example/y={t:%Y}/m={t:%m}/d={t:%d}/"
                                    f"h={t:%H}/m={t:%M}/PT05M{suffix}.json", t, writing=(k == 0)))
        k += 1
    add("stexample01", "am-securityevent", blobs)
    # リソースログ（Blob の読み取り）: ストレージアカウント3つ × 1時間ごと
    for acct in ("stexample01", "stexample02", "stexample03"):
        blobs = []
        for k in range(hours if acct != "stexample03" else hours // 2):
            t = now - timedelta(hours=k)
            name = (f"resourceId=/SUBSCRIPTIONS/{SUB}/RESOURCEGROUPS/RG-EXAMPLE/PROVIDERS/MICROSOFT.STORAGE/"
                    f"STORAGEACCOUNTS/{acct.upper()}/BLOBSERVICES/DEFAULT/y={t:%Y}/m={t:%m}/d={t:%d}/h={t:%H}/m=00/PT1H.json")
            blobs.append(_blob(rnd, name, t, writing=(k == 0)))
        add(acct, "insights-logs-storageread", blobs)
    # Key Vault の監査ログ
    blobs = []
    for k in range(hours // 2):
        t = now - timedelta(hours=k)
        name = (f"resourceId=/SUBSCRIPTIONS/{SUB}/RESOURCEGROUPS/RG-EXAMPLE/PROVIDERS/MICROSOFT.KEYVAULT/"
                f"VAULTS/KV-EXAMPLE/y={t:%Y}/m={t:%m}/d={t:%d}/h={t:%H}/m=00/PT1H.json")
        blobs.append(_blob(rnd, name, t, writing=(k == 0)))
    add("stexample01", "insights-logs-auditevent", blobs)
    # アクティビティログ
    blobs = []
    for k in range(min(hours, 24 * 120)):
        t = now - timedelta(hours=k)
        blobs.append(_blob(rnd, f"resourceId=/SUBSCRIPTIONS/{SUB}/y={t:%Y}/m={t:%m}/d={t:%d}/h={t:%H}/m=00/PT1H.json",
                           t, writing=(k == 0)))
    add("stexample01", "insights-activity-logs", blobs)
    # Storage Analytics（$logs）
    blobs = []
    for k in range(max(24, total // 20)):
        t = now - timedelta(hours=k // 3)
        svc = ("blob", "queue", "table")[k % 3]
        blobs.append(_blob(rnd, f"{svc}/{t:%Y/%m/%d/%H}00/{k % 7:06d}.log", t))
    add("stexample02", "$logs", blobs)
    # 仮想ネットワークのフローログ
    blobs = []
    for k in range(max(24, total // 30)):
        t = now - timedelta(hours=k)
        for mac in ("000D3A000001", "000D3A000002"):
            blobs.append(_blob(rnd, f"flowLogResourceID=/{SUB.upper()}_NETWORKWATCHERRG/NETWORKWATCHER_JAPANEAST_"
                                    f"vnet-example-flowlog/y={t:%Y}/m={t:%m}/d={t:%d}/h={t:%H}/m=00/macAddress={mac}/PT1H.json", t))
    add("stexample03", "insights-logs-flowlogflowevent", blobs)
    # バックアップ（日本語・記号を含む名前、大きな Blob）
    blobs = []
    depts = ["営業部", "開発部", "総務部"]
    for k in range(max(30, total // 50)):
        t = now - timedelta(days=k % 400, hours=k % 24)
        d = depts[k % 3]
        blobs.append(_blob(rnd, f"{d}/{t:%Y}/{t:%m}/報告書 #{k} (確定版).pdf", t))
    blobs.append(_blob(rnd, "large/disk-image-100GB.vhd", now - timedelta(days=200), size=100 * GB, tier="Archive"))
    blobs.append(_blob(rnd, "large/archive-2GB.zip", now - timedelta(days=30), size=2 * GB, tier="Cool"))
    add("stexample01", "backup", blobs)
    # BLOB インベントリ
    blobs = []
    for k in range(60):
        t = (now - timedelta(days=k)).replace(hour=1, minute=23, second=45)
        for rule in ("daily-rule",):
            blobs.append(_blob(rnd, f"{t:%Y/%m/%d/%H-%M-%S}/{rule}/{rule}_1.csv", t, tier="Hot"))
            blobs.append(_blob(rnd, f"{t:%Y/%m/%d/%H-%M-%S}/{rule}/{rule}-manifest.json", t, tier="Hot", size=800))
    add("stexample02", "inventory", blobs)
    # 別のドメインのストレージアカウント
    blobs = [_blob(rnd, f"logs/{(now - timedelta(days=k)):%Y%m%d}.log", now - timedelta(days=k)) for k in range(90)]
    add("stexample04", "logs", blobs, "core.chinacloudapi.cn")
    for acct in ("stexample01", "stexample02", "stexample03"):
        backend.add_container(acct, "empty")
    return counts
