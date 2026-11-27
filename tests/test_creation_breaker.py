"""Offline regression tests: never contact game servers or account storage."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "web"), str(ROOT)]
import auto_rank as ar


class CreationBreakerTests(unittest.TestCase):
    def setUp(self):
        self.rank = ar.AutoRank(threading.RLock())
        self.rank.enabled = True
        self.rank.on_banned = Mock()
        self.account = {"uid": 1, "userName": "test", "passwd": "test"}

    def test_rejected_new_accounts_stop_anchor_batch_without_deletion(self):
        with patch.object(ar.m, "register", return_value=self.account) as register, \
                patch.object(ar.m, "login", side_effect=RuntimeError("登录被拒绝：帐号异常")), \
                patch.object(self.rank, "_anchor_accounts", return_value=[]), \
                patch.object(self.rank, "_refresh_anchor_state"), \
                patch.object(ar.time, "sleep"):
            self.rank._ensure_anchors()
            self.assertEqual(register.call_count, 3)
            self.assertTrue(self.rank.creation_paused)
            self.assertEqual(self.rank.anchor_next_try, 0)
            self.rank._ensure_anchors(force=True)
            self.rank._ensure_today_batch = Mock(wraps=self.rank._ensure_today_batch)
            with patch.object(self.rank, "_accounts_of_day", return_value=[]):
                self.rank._ensure_today_batch()
            self.assertEqual(register.call_count, 3)
        self.rank.on_banned.assert_not_called()
        self.assertFalse(self.rank._anchor_need_create())

    def test_registration_rejections_share_breaker(self):
        with patch.object(ar.m, "register", side_effect=RuntimeError("注册被拒：联网失败")) as register:
            for _ in range(4):
                with self.assertRaises(RuntimeError):
                    self.rank._create_account("test")
            self.assertEqual(register.call_count, 3)
            self.assertTrue(self.rank.creation_paused)

    def test_network_errors_do_not_trip_rejection_breaker(self):
        with patch.object(ar.m, "register", side_effect=TimeoutError("timeout")):
            for _ in range(4):
                with self.assertRaises(TimeoutError):
                    self.rank._create_account("test")
        self.assertFalse(self.rank.creation_paused)
        self.assertEqual(self.rank.creation_rejections, 0)

    def test_manual_button_does_not_implicitly_resume(self):
        self.rank.creation_paused = True
        with patch.object(self.rank, "status", return_value={}), \
                patch.object(ar.threading, "Thread") as thread:
            self.rank.create_anchors()
            thread.assert_not_called()
            self.rank.create_anchors(resume=True)
            thread.assert_called_once()
        self.assertFalse(self.rank.creation_paused)
        self.assertEqual(self.rank.creation_rejections, 2)

    def test_switch_off_cancels_manual_batch_even_when_already_off(self):
        self.rank.enabled = False
        with patch.object(self.rank, "status", return_value={}):
            self.rank.set_enabled(False)
        self.assertTrue(self.rank.creation_cancel.is_set())

    def test_cancel_stops_force_batch_before_registration(self):
        self.rank.creation_cancel.set()
        with patch.object(self.rank, "_anchor_accounts", return_value=[]), \
                patch.object(self.rank, "_refresh_anchor_state"), \
                patch.object(ar.m, "register") as register:
            self.rank._ensure_anchors_inner(force=True)
        register.assert_not_called()
        self.assertEqual(self.rank.anchor_next_try, 0)


if __name__ == "__main__":
    unittest.main()
