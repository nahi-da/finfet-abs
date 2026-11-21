"""T-RERUN: 再開・すべて再実行・選択した Blob の再実行。認証が使えない場合の表示と探し直し（3.1.9）、お知らせ（4.3.5）。"""

from __future__ import annotations

import time
import unittest
from dataclasses import replace

from finfet_abs.azure.backend import BackendError
from finfet_abs.core.errors import AppError
from finfet_abs.demo.memory import MemoryBackend, blob_md5

from .test_jobs import KB, JobTestBase, blob


class CredBackend(MemoryBackend):
    """保存した認証情報「gone」が削除された状態をまねる架空の実装。"""

    def _check(self, target, op: str) -> None:
        if target.account.auth.credential_id == "gone":
            raise BackendError("NO_CREDENTIAL", "認証情報がありません。")
        super()._check(target, op)


class RerunTest(JobTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.notes: list[dict] = []
        orig = self.events.publish

        def publish(kind, data=None, *a, **kw):
            if kind == "notify":
                self.notes.append(data)
            return orig(kind, data, *a, **kw)
        self.events.publish = publish

    def test_resume_processes_only_unfinished(self) -> None:
        self.add("logs", [blob("r/ok.log"), replace(blob("r/bad.log"), content_md5=blob_md5("other", 10 * KB))])
        jid = self.create(self.make_search(), job_folder=False)
        self.run_until(lambda: self.status(jid) == "completed_with_errors")
        before = {r["blob_name"]: r for r in self.items(jid)}
        self.assertEqual(before["r/bad.log"]["status"], "error")
        self.assertTrue(any(n["level"] == "error" for n in self.notes))  # 一部失敗のお知らせ
        # 直してから再開: 未完了の Blob だけを処理します
        self.add("logs", [blob("r/bad.log")])
        self.be.requests.clear()
        self.assertEqual(self.engine.resume(jid), 1)
        self.run_until(lambda: self.status(jid) == "completed")
        after = {r["blob_name"]: r for r in self.items(jid)}
        self.assertEqual(after["r/bad.log"]["status"], "completed")
        self.assertEqual(after["r/ok.log"]["download_completed_at"], before["r/ok.log"]["download_completed_at"])
        self.assertFalse([r for r in self.be.requests if r[2] == "r/ok.log"])
        self.assertTrue(any(n["level"] == "ok" for n in self.notes))  # 完了のお知らせ
        with self.assertRaises(AppError):
            self.engine.resume(jid)  # 未完了の Blob がなければ再開できません

    def test_rerun_all_makes_new_job_and_keeps_original(self) -> None:
        self.add("logs", [blob("a/1.log"), blob("a/2.log")])
        jid = self.create(self.make_search(), job_folder=False)
        self.run_until(lambda: self.status(jid) == "completed")
        opts = self.job(jid)["options"]
        new = self.svc.create({"kind": "job", "job_id": jid}, {**opts, "conflict": "number"}, start=True,
                              confirm_rehydrate=True)
        self.assertNotEqual(new, jid)
        self.run_until(lambda: self.status(new) == "completed")
        self.assertEqual(self.status(jid), "completed")  # 元のジョブの記録は残ります
        self.assertEqual(len(self.items(jid)), 2)
        self.assertEqual(len(self.items(new)), 2)  # 完了済みの Blob も含めて処理し直します
        self.assertEqual(self.job(new)["source_job"]["id"], jid)
        names = sorted(r["local_path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for r in self.items(new))
        self.assertEqual(names, ["1(1).log", "2(1).log"])

    def test_rerun_selected(self) -> None:
        self.add("logs", [blob("s/1.log"), blob("s/2.log"), blob("s/3.log")])
        jid = self.create(self.make_search(), job_folder=False)
        self.run_until(lambda: self.status(jid) == "completed")
        pick = [r["id"] for r in self.items(jid) if r["blob_name"] == "s/2.log"]
        new = self.svc.create({"kind": "job", "job_id": jid, "item_ids": pick},
                              {**self.job(jid)["options"], "conflict": "overwrite"}, start=True, confirm_rehydrate=True)
        self.run_until(lambda: self.status(new) == "completed")
        self.assertEqual([r["blob_name"] for r in self.items(new)], ["s/2.log"])
        with self.assertRaises(AppError):
            self.svc.create({"kind": "job", "job_id": jid, "item_ids": [999999]}, self.job(jid)["options"])

    def test_requested_rehydration_not_requested_again(self) -> None:
        self.add("logs", [blob("z/1.log", tier="Archive"), blob("z/2.log")])
        sid = self.make_search()
        jid = self.create(sid, method="rehydrate_download")
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate")
        self.assertEqual(len([r for r in self.be.requests if r[0] == "tier"]), 1)
        self.svc.cancel([jid])
        # 再開: 完了の確認から続けます
        self.engine.resume(jid)
        it = {r["blob_name"]: r for r in self.items(jid)}["z/1.log"]
        self.assertEqual(it["status"], "rehydrating")
        # すべて再実行: リハイドレート中の Blob は要求し直さず、完了を待ちます
        self.clock.advance(3600)
        self.svc.cancel([jid])
        new = self.svc.create({"kind": "job", "job_id": jid}, {**self.job(jid)["options"], "conflict": "number"},
                              start=True, confirm_rehydrate=True)
        self.run_until(lambda: self.status(new) == "waiting_rehydrate")
        self.assertEqual(len([r for r in self.be.requests if r[0] == "tier"]), 1)
        it = {r["blob_name"]: r for r in self.items(new)}["z/1.log"]
        self.assertEqual((it["status"], it["rehydrate_requested_known"]), ("rehydrating", 0))
        # 完了すると、どちらのお知らせにも出ます
        self.be.complete_rehydration("stexample01", "logs", "z/1.log", when=self.clock.now())
        self.clock.advance(61 * 60)
        self.engine.poll()
        self.assertTrue(any("リハイドレートが完了しました" in n["text"] for n in self.notes))
        self.run_until(lambda: self.status(new) == "completed")


class AuthProblemTest(JobTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.be = CredBackend()
        self.resolved: dict = {"resolved": False}
        self.engine.resolve_auth = lambda t: dict(self.resolved)
        self.add("logs", [blob("k/1.log"), blob("k/2.log")])

    def _set_cred(self, jid: str, cred: str) -> None:
        self.db.execute("UPDATE job_items SET credential_id=? WHERE job_id=?", (cred, jid))

    def test_signin_required_pauses_and_shows(self) -> None:
        jid = self.create(self.make_search(), job_folder=False, start=False)
        self.clock.advance(24 * 3600)  # 検索の情報を使い回さず、確認で Azure に問い合わせます
        self.be.denied[("stexample01", "logs", "read")] = "NOT_SIGNED_IN"
        self.svc.start([jid])
        self.run_until(lambda: self.status(jid) == "paused")
        j = self.job(jid)
        self.assertEqual(j["last_error"]["code"], "NOT_SIGNED_IN")
        self.assertIn("サインインが必要です", j["last_error"]["message"])
        self.assertTrue(all(r["status"] == "pending" for r in self.items(jid)))  # Blob はエラーにしません
        # サインインの後に開始すると、続きから処理し、表示を消します
        self.be.denied.clear()
        self.svc.start([jid])
        self.run_until(lambda: self.status(jid) == "completed")
        self.assertIsNone(self.job(jid)["last_error"])

    def test_deleted_credential_is_replaced(self) -> None:
        jid = self.create(self.make_search(), job_folder=False, start=False)
        self._set_cred(jid, "gone")
        self.resolved = {"resolved": True, "auth_kind": "account_key", "credential_id": "cred-new"}
        self.svc.start([jid])
        self.run_until(lambda: self.status(jid) == "completed")
        self.assertEqual({r["credential_id"] for r in self.items(jid)}, {"cred-new"})

    def test_deleted_credential_not_found(self) -> None:
        jid = self.create(self.make_search(), job_folder=False, start=False)
        self._set_cred(jid, "gone")
        self.svc.start([jid])
        self.run_until(lambda: self.status(jid) == "paused")
        self.assertEqual(self.job(jid)["last_error"]["code"], "NO_CREDENTIAL")
        self.assertIn("認証情報がありません", self.job(jid)["last_error"]["message"])

    def test_poll_shows_problem(self) -> None:
        self.add("logs", [blob("k/arc.log", tier="Archive")])
        jid = self.create(self.make_search(pattern="k/arc.*"), method="rehydrate_only")
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate")
        self.be.denied[("stexample01", "logs", "read")] = "NOT_SIGNED_IN"
        self.clock.advance(61 * 60)
        self.engine.poll()
        self.assertEqual(self.job(jid)["last_error"]["code"], "NOT_SIGNED_IN")
        self.be.denied.clear()
        self.be.complete_rehydration("stexample01", "logs", "k/arc.log")
        self.engine.poll(force=True)
        self.assertIsNone(self.job(jid)["last_error"])
        self.assertEqual(self.status(jid), "waiting_download")

    def test_start_download_checks_auth(self) -> None:
        self.add("logs", [blob("k/arc.log", tier="Archive")])
        jid = self.create(self.make_search(pattern="k/arc.*"), method="rehydrate_only")
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate")
        self.be.complete_rehydration("stexample01", "logs", "k/arc.log")
        self.engine.poll(force=True)
        self._set_cred(jid, "gone")
        with self.assertRaises(AppError) as cm:
            self.engine.start_download(jid)
        self.assertEqual(cm.exception.code, "NO_CREDENTIAL")
        self.resolved = {"resolved": True, "auth_kind": "account_key", "credential_id": "cred-new"}
        self.engine.start_download(jid)
        self.run_until(lambda: self.status(jid) == "completed")


class StartupCheckTest(JobTestBase):
    def test_check_on_startup(self) -> None:
        self.add("logs", [blob("u/arc.log", tier="Archive")])
        jid = self.create(self.make_search(), method="rehydrate_only")
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate")
        self.be.complete_rehydration("stexample01", "logs", "u/arc.log")
        # 次の確認の時刻の前でも、起動時には確認します
        self.settings.update({"rehydrate.check_on_startup": True})
        self.engine.start()
        self.run_until(lambda: self.status(jid) == "waiting_download")

    def test_no_check_on_startup(self) -> None:
        self.add("logs", [blob("u/arc.log", tier="Archive")])
        jid = self.create(self.make_search(), method="rehydrate_only")
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate")
        self.be.complete_rehydration("stexample01", "logs", "u/arc.log")
        self.settings.update({"rehydrate.check_on_startup": False})
        self.engine.start()
        time.sleep(1.5)
        self.assertEqual(self.status(jid), "waiting_rehydrate")


class SleepTest(JobTestBase):
    """3.4.4 の L8: ダウンロード中・（設定で）リハイドレート待ちの間はスリープしない。"""

    def test_prevent_sleep(self) -> None:
        calls: list[bool] = []
        self.engine.set_sleep_state = calls.append
        self.add("logs", [blob("p/arc.log", tier="Archive")])
        self.settings.update({"load.prevent_sleep_downloading": True, "load.prevent_sleep_waiting": False})
        self.assertFalse(self.engine.wants_no_sleep())
        jid = self.create(self.make_search(), method="rehydrate_only")
        self.engine.schedule()
        self.assertTrue(self.engine.wants_no_sleep())  # 実行中
        self.engine.update_sleep()
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate" and not self.engine.running_ids())
        self.engine.update_sleep()
        self.assertEqual(calls, [True, False])
        # リハイドレート待ちの間もスリープしない設定
        self.settings.update({"load.prevent_sleep_waiting": True})
        self.engine.update_sleep()
        self.engine.update_sleep()
        self.assertEqual(calls, [True, False, True])
        self.settings.update({"load.prevent_sleep_downloading": False, "load.prevent_sleep_waiting": False})
        self.engine.update_sleep()
        self.assertEqual(calls[-1], False)


if __name__ == "__main__":
    unittest.main()
