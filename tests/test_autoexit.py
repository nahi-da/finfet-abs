"""画面をすべて閉じた後の自動の終了（v1.3.0）と、終了の確認（2.6）。"""

from __future__ import annotations

import unittest

from finfet_abs.core.autoexit import GRACE_SECONDS, AutoExit
from finfet_abs.services import setup_services

from .helpers import Client, TempDirMixin, make_app, quiet_logs


class FakeSettings:
    def __init__(self, **kw) -> None:
        self.v = {"exit.when_closed": True, "exit.wait_minutes": 1, **kw}

    def get(self, key):
        return self.v[key]


class AutoExitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        self.busy = False
        self.exits = 0
        self.settings = FakeSettings()
        self.ae = AutoExit(self.settings, busy=lambda: self.busy, on_exit=self.on_exit, clock=lambda: self.now)

    def on_exit(self) -> None:
        self.exits += 1

    def advance(self, seconds: float, step: float = 5.0) -> bool:
        """時間を進めながら、5 秒ごとに確かめます。"""
        fired = False
        end = self.now + seconds
        while self.now < end:
            self.now = min(end, self.now + step)
            fired = self.ae.check() or fired
        return fired

    def test_never_connected_does_not_exit(self) -> None:
        self.assertFalse(self.advance(600))
        self.assertEqual(self.exits, 0)

    def test_closed_tab_exits_after_wait(self) -> None:
        self.ae.poll_begin("a")
        self.advance(30)
        self.ae.poll_end("a")
        self.ae.set_state("a", "closed")
        self.assertFalse(self.advance(55))
        self.assertTrue(self.advance(10))
        self.assertEqual(self.exits, 1)
        # 一度だけ
        self.assertFalse(self.advance(120))
        self.assertEqual(self.exits, 1)

    def test_polling_keeps_running(self) -> None:
        for _ in range(20):
            self.ae.poll_begin("a")
            self.advance(25)
            self.ae.poll_end("a")
        self.assertEqual(self.exits, 0)
        self.assertTrue(self.ae.connected())

    def test_lost_without_close_uses_grace(self) -> None:
        self.ae.poll_begin("a")
        self.ae.poll_end("a")
        # 閉じた知らせがなくても、要求が来なくなったら猶予の後に数え始めます
        self.assertFalse(self.advance(GRACE_SECONDS + 55))
        self.assertTrue(self.advance(15))

    def test_frozen_tab_is_not_closed(self) -> None:
        self.ae.poll_begin("a")
        self.ae.poll_end("a")
        self.ae.set_state("a", "frozen")
        self.assertFalse(self.advance(3600))
        self.ae.set_state("a", "active")
        self.ae.set_state("a", "closed")
        self.assertTrue(self.advance(70))

    def test_other_tab_still_open(self) -> None:
        self.ae.poll_begin("a")
        self.ae.poll_begin("b")
        self.ae.set_state("a", "closed")
        self.assertFalse(self.advance(600))
        self.ae.poll_end("b")
        self.ae.set_state("b", "closed")
        self.assertTrue(self.advance(70))

    def test_busy_waits_until_done(self) -> None:
        self.ae.poll_begin("a")
        self.ae.set_state("a", "closed")
        self.busy = True
        self.assertFalse(self.advance(600))
        self.busy = False
        self.assertTrue(self.advance(5))

    def test_disabled_and_wait_minutes(self) -> None:
        self.settings.v["exit.when_closed"] = False
        self.ae.poll_begin("a")
        self.ae.set_state("a", "closed")
        self.assertFalse(self.advance(600))
        self.settings.v["exit.when_closed"] = True
        self.settings.v["exit.wait_minutes"] = 5
        self.assertFalse(self.advance(295))
        self.assertTrue(self.advance(10))

    def test_resume_from_sleep_waits_for_reconnect(self) -> None:
        self.ae.poll_begin("a")
        self.ae.poll_end("a")
        self.advance(10)
        # PC のスリープ（確かめる間隔が大きく空く）
        self.now += 3 * 3600
        self.assertFalse(self.ae.check())
        self.assertTrue(self.ae.connected())
        self.ae.poll_begin("a")
        self.assertFalse(self.advance(600))

    def test_reload_keeps_running(self) -> None:
        self.ae.poll_begin("a")
        self.ae.poll_end("a")
        self.ae.set_state("a", "closed")
        self.advance(2)
        self.ae.poll_begin("b")
        self.assertFalse(self.advance(600))


class ShutdownApiTest(TempDirMixin):
    def setUp(self) -> None:
        super().setUp()
        quiet_logs()
        self.app = make_app(self.tmp, demo=True)
        setup_services(self.app)
        self.c = Client(self.app.build_wsgi())
        self.c.login(self.app.security)

    def tearDown(self) -> None:
        self.app.stop()
        super().tearDown()

    def test_events_with_client_and_state(self) -> None:
        ae = self.app.services["auto_exit"]
        self.assertFalse(ae.connected())
        r = self.c.get("/api/v1/events?after=&client=tab-1")
        self.assertEqual(r.status, 200)
        self.assertTrue(ae.connected())
        r = self.c.post("/api/v1/app/client", {"client": "tab-1", "state": "closed"})
        self.assertEqual((r.status, r.json()), (200, {"ok": True}))
        self.assertFalse(ae.connected())
        # 知らない状態は無視します
        self.assertEqual(self.c.post("/api/v1/app/client", {"client": "tab-1", "state": "x"}).status, 200)
        self.assertFalse(ae.connected())
        # CSRF の確認は、ほかの操作と同じです
        self.assertEqual(self.c.post("/api/v1/app/client", {"client": "t", "state": "closed"}, csrf=False).status, 403)

    def test_shutdown_confirms_running_jobs(self) -> None:
        jobs = self.app.services["jobs"]
        self.assertEqual(jobs.running_job_names(), [])
        self.app.services["job_engine"].stop()
        self.app.db.execute("INSERT INTO jobs (id, name, source, status, queue_order, created_at, updated_at, options_json) "
                            "VALUES ('j1', 'テストのジョブ', 'search', 'running', 1, '2026-11-22T00:00:00Z', '2026-11-22T00:00:00Z', '{}')")
        self.assertEqual(jobs.running_job_names(), ["テストのジョブ"])
        r = self.c.post("/api/v1/app/shutdown", {})
        self.assertEqual(r.status, 409)
        self.assertEqual(r.json()["running"], ["テストのジョブ"])
        self.app.db.execute("UPDATE jobs SET status='paused' WHERE id='j1'")
        self.assertEqual(jobs.running_job_names(), [])


if __name__ == "__main__":
    unittest.main()
