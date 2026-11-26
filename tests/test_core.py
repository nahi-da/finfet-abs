"""設定（5.4）・イベント（5.8）・保存先（5.2）・時計の確認。"""

import json
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from finfet_abs.core import paths
from finfet_abs.core.clock import FakeClock, from_iso, to_iso
from finfet_abs.core.errors import AppError
from finfet_abs.core.events import EventBus
from finfet_abs.core.settings import FIELDS, Settings, defaults

from .helpers import TempDirMixin


class SettingsTest(TempDirMixin):
    def test_defaults_written(self) -> None:
        s = Settings(self.tmp / "settings.json")
        s.load()
        data = json.loads((self.tmp / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["queue"]["concurrent_jobs"], 1)
        self.assertEqual(data["rehydrate"]["poll_first_minutes"]["standard"], 60)
        self.assertEqual(s.get("transfer.max_connections"), 8)
        self.assertEqual(s.get("fetch.path_structure"), "preserve")

    def test_merge_and_invalid_values(self) -> None:
        p = self.tmp / "settings.json"
        p.write_text(json.dumps({"queue": {"concurrent_jobs": 3}, "transfer": {"max_connections": 999}}), encoding="utf-8")
        s = Settings(p)
        s.load()
        self.assertEqual(s.get("queue.concurrent_jobs"), 3)
        self.assertEqual(s.get("transfer.max_connections"), 8)
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("search", data)

    def test_exit_wait_minutes_migrates_to_seconds(self) -> None:
        # v1.7.4: 分で指定していた終了までの時間を、秒に移します。既定（1 分）のままなら、新しい既定（5 秒）にします
        for old, expect in ((1, 5), (10, 600), (None, 5)):
            p = self.tmp / f"s{old}.json"
            p.write_text(json.dumps({"exit": {"wait_minutes": old} if old is not None else {}}), encoding="utf-8")
            s = Settings(p)
            s.load()
            self.assertEqual(s.get("exit.wait_seconds"), expect, old)
        p = self.tmp / "s-new.json"
        p.write_text(json.dumps({"exit": {"wait_minutes": 10, "wait_seconds": 30}}), encoding="utf-8")
        s = Settings(p)
        s.load()
        self.assertEqual(s.get("exit.wait_seconds"), 30)

    def test_broken_file(self) -> None:
        p = self.tmp / "settings.json"
        p.write_text("{broken", encoding="utf-8")
        s = Settings(p)
        s.load()
        self.assertTrue((self.tmp / "settings.json.bak").exists())
        self.assertIsNotNone(s.load_warning)
        self.assertEqual(s.snapshot(), defaults())

    def test_update_validation(self) -> None:
        s = Settings(self.tmp / "settings.json")
        s.load()
        seen = []
        s.on_change(seen.append)
        with self.assertRaises(AppError) as cm:
            s.update({"queue.concurrent_jobs": 5, "ui.start_page": "x", "nope": 1})
        self.assertEqual(set(cm.exception.extra["fields"]), {"queue.concurrent_jobs", "ui.start_page", "nope"})
        self.assertEqual(s.get("queue.concurrent_jobs"), 1)
        changed = s.update({"queue.concurrent_jobs": "2", "transfer.bandwidth_mbps": 10.5})
        self.assertEqual(set(changed), {"queue.concurrent_jobs", "transfer.bandwidth_mbps"})
        self.assertEqual(seen, [changed])
        s2 = Settings(self.tmp / "settings.json")
        s2.load()
        self.assertEqual(s2.get("queue.concurrent_jobs"), 2)
        self.assertEqual(s2.get("transfer.bandwidth_mbps"), 10.5)
        s.reset()
        self.assertEqual(s.get("queue.concurrent_jobs"), 1)

    def test_schema_single_source(self) -> None:
        # 既定値はスキーマの1か所だけで定義します（I12）
        self.assertEqual(set(defaults()), set(FIELDS))


class EventsTest(unittest.TestCase):
    def test_publish_and_wait(self) -> None:
        bus = EventBus(keep=5)
        self.assertEqual(bus.wait(0, 0.01), {"last": 0, "events": []})
        bus.publish("a", {"x": 1})
        r = bus.wait(0, 0.01)
        self.assertEqual([e["type"] for e in r["events"]], ["a"])
        for i in range(10):
            bus.publish("b", {"i": i})
        r = bus.wait(1, 0.01)
        self.assertTrue(r["reset"])
        r = bus.wait(bus.last + 10, 0.01)
        self.assertTrue(r["reset"])

    def test_wait_wakes_up(self) -> None:
        bus = EventBus()
        threading.Timer(0.1, lambda: bus.publish("x")).start()
        t0 = time.monotonic()
        r = bus.wait(0, 5)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(r["events"][0]["type"], "x")

    def test_coalesce(self) -> None:
        bus = EventBus()
        for i in range(10):
            bus.publish_coalesced("job.progress", "j1", {"i": i})
        bus.publish_coalesced("job.progress", "j2", {"i": 0})
        bus.flush()
        r = bus.wait(0, 0.01)
        self.assertEqual([(e["type"], e["data"]["i"]) for e in r["events"]], [("job.progress", 9), ("job.progress", 0)])


class PathsTest(TempDirMixin):
    def test_safe_component(self) -> None:
        self.assertEqual(paths.safe_component("a<b>c"), "a_b_c")
        self.assertEqual(paths.safe_component("CON"), "_CON")
        self.assertEqual(paths.safe_component("com1.txt"), "_com1.txt")
        self.assertEqual(paths.safe_component("name. "), "name")
        self.assertEqual(paths.safe_component("..."), "_")
        self.assertEqual(paths.safe_component("."), "_")
        self.assertEqual(paths.safe_component(".."), "__")
        self.assertEqual(paths.safe_component("a\x01b"), "a_b")

    def test_resolve_modes(self) -> None:
        app_dir = self.tmp / "tool"
        app_dir.mkdir()
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": str(self.tmp / "local")}):
            loc = paths.resolve(None, app_dir=app_dir)
            self.assertEqual(loc.mode, "normal")
            self.assertEqual(loc.data_dir, self.tmp / "local" / "finfet-abs")
            (app_dir / "data").mkdir()
            with mock.patch.object(paths.winapi, "get_user_name_sam", return_value="CONTOSO\\sato"), \
                    mock.patch.object(paths.winapi, "restrict_to_current_user", return_value=True):
                loc = paths.resolve(None, app_dir=app_dir)
            self.assertEqual(loc.mode, "portable")
            self.assertEqual(loc.data_dir, app_dir / "data" / "CONTOSO_sato")
            self.assertTrue(loc.acl_ok)
            self.assertTrue(loc.data_dir.is_dir())
            loc = paths.resolve(str(self.tmp / "custom"), app_dir=app_dir)
            self.assertEqual(loc.mode, "custom")
            self.assertEqual(loc.local_dir, self.tmp / "local" / "finfet-abs")

    def test_is_inside(self) -> None:
        self.assertTrue(paths.is_inside(Path("C:/a/b/c"), Path("c:/A")))
        self.assertFalse(paths.is_inside(Path("C:/ab"), Path("C:/a")))
        self.assertFalse(paths.is_inside(Path("D:/a"), Path("C:/a")))


class InstanceTest(TempDirMixin):
    def test_secret_is_encrypted(self) -> None:
        from finfet_abs.core import dpapi, instance
        instance.write(self.tmp, port=1234, secret="instance-secret-value-xyz", version="0.0.0")
        raw = (self.tmp / "instance.json").read_text(encoding="utf-8")
        self.assertNotIn("instance-secret-value-xyz", raw)
        info = instance.read(self.tmp)
        self.assertEqual(info["port"], 1234)
        self.assertEqual(dpapi.unseal(info["secret"], "instance").reveal(), "instance-secret-value-xyz")
        instance.remove(self.tmp)
        self.assertIsNone(instance.read(self.tmp))


class ClockTest(unittest.TestCase):
    def test_iso(self) -> None:
        dt = datetime(2026, 11, 20, 1, 15, 2, tzinfo=timezone.utc)
        self.assertEqual(to_iso(dt), "2026-11-20T01:15:02Z")
        self.assertEqual(from_iso("2026-11-20T01:15:02Z"), dt)
        with self.assertRaises(ValueError):
            to_iso(datetime(2026, 1, 1))
        c = FakeClock(dt)
        c.advance(60)
        self.assertEqual(to_iso(c.now()), "2026-11-20T01:16:02Z")


if __name__ == "__main__":
    unittest.main()
