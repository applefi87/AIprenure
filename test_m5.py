"""
test_m5.py — M5 驗收測試
==========================
E2E gate + Telegram 人類核准入口

涵蓋：
  1. Story 所有 task done → _check_story_completion() 推進到 e2e
  2. Story task 未全完成  → 保持 refined，不推進
  3. E2E 通過 → story done + Telegram 通知「/approve」
  4. E2E 失敗 → story paused + Telegram 通知失敗
  5. Telegram /approve → merge_dev_to_main → story delivered
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ── 確保可以 import 專案模組 ───────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

# ── 把 DB 導向 /tmp，不汙染正式資料庫 ──────────────────────────────────
_TMP = tempfile.mkdtemp(prefix="test_m5_")
import db as _db_module
_db_module.DB_PATH = Path(_TMP) / "test.db"
import db

import orchestrator


def _make_story(story_id="S-001", status="refined"):
    return {
        "id": story_id, "type": "story", "status": status,
        "title": "Test story", "body": "Test body",
        "owner": None, "parent_id": None, "branch": "feature/S-001",
        "pr_number": None, "loop_count": 0, "retry_count": 0,
    }


def _make_task(task_id, status="done", parent="S-001"):
    return {
        "id": task_id, "type": "task", "status": status,
        "title": "Task " + task_id, "body": "",
        "parent_id": parent, "owner": None, "branch": "card/" + task_id,
        "pr_number": None, "loop_count": 0, "retry_count": 0,
    }


# ══════════════════════════════════════════════════════════════════════
# 測試 1 & 2：_check_story_completion()
# ══════════════════════════════════════════════════════════════════════

class TestCheckStoryCompletion(unittest.TestCase):

    @patch("db.insert_event")
    @patch("db.update_card_status")
    @patch("db.get_tasks_for_story")
    @patch("db.list_stories_by_status")
    def test_all_tasks_done_advances_to_e2e(
        self, mock_list, mock_tasks, mock_update, mock_event
    ):
        """所有 task 狀態為 done → story 推進到 e2e"""
        mock_list.return_value = [_make_story("S-001", "refined")]
        mock_tasks.return_value = [
            _make_task("T-001-1", "done"),
            _make_task("T-001-2", "done"),
        ]

        orchestrator._check_story_completion()

        mock_update.assert_called_once_with("S-001", "e2e")
        mock_event.assert_called_once()
        args = mock_event.call_args
        self.assertEqual(args.kwargs.get("event_type") or args[0][0], "all_tasks_done")

    @patch("db.insert_event")
    @patch("db.update_card_status")
    @patch("db.get_tasks_for_story")
    @patch("db.list_stories_by_status")
    def test_pending_tasks_keep_story_refined(
        self, mock_list, mock_tasks, mock_update, mock_event
    ):
        """有 task 尚未完成 → story 維持 refined，不推進"""
        mock_list.return_value = [_make_story("S-001", "refined")]
        mock_tasks.return_value = [
            _make_task("T-001-1", "done"),
            _make_task("T-001-2", "coding"),   # 還沒完成
        ]

        orchestrator._check_story_completion()

        mock_update.assert_not_called()
        mock_event.assert_not_called()

    @patch("db.insert_event")
    @patch("db.update_card_status")
    @patch("db.get_tasks_for_story")
    @patch("db.list_stories_by_status")
    def test_no_tasks_keeps_story_refined(
        self, mock_list, mock_tasks, mock_update, mock_event
    ):
        """還沒有任何 task（Spec agent 尚未產生）→ 不推進"""
        mock_list.return_value = [_make_story("S-001", "refined")]
        mock_tasks.return_value = []

        orchestrator._check_story_completion()

        mock_update.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# 測試 3 & 4：_run_e2e_gate()
# ══════════════════════════════════════════════════════════════════════

class TestRunE2EGate(unittest.TestCase):

    @patch("telegram_bot.send_message")
    @patch("truth.teardown_story_e2e")
    @patch("truth.e2e_pass", return_value=True)
    @patch("truth.setup_story_e2e")
    @patch("db.insert_event")
    @patch("db.clear_card_owner")
    @patch("db.update_card_status")
    def test_e2e_pass_advances_to_done_and_notifies(
        self, mock_update, mock_clear, mock_event,
        mock_setup, mock_pass, mock_teardown, mock_send
    ):
        """E2E 通過 → story→done + Telegram 通知核准"""
        mock_setup.return_value = Path("/tmp/e2e_S-001")
        story = _make_story("S-001", "e2e")

        orchestrator._run_e2e_gate(story, {})

        mock_update.assert_called_once_with("S-001", "done")
        mock_clear.assert_called_once_with("S-001")
        # Telegram 應傳送含 /approve 的訊息
        self.assertTrue(mock_send.called)
        msg = mock_send.call_args[0][0]
        self.assertIn("approve", msg)
        self.assertIn("S-001", msg)
        mock_teardown.assert_called_once()

    @patch("telegram_bot.send_message")
    @patch("truth.teardown_story_e2e")
    @patch("truth.e2e_pass", return_value=False)
    @patch("truth.setup_story_e2e")
    @patch("db.insert_event")
    @patch("db.clear_card_owner")
    @patch("db.update_card_status")
    def test_e2e_fail_pauses_story_and_notifies(
        self, mock_update, mock_clear, mock_event,
        mock_setup, mock_pass, mock_teardown, mock_send
    ):
        """E2E 失敗 → story→paused + Telegram 通知失敗"""
        mock_setup.return_value = Path("/tmp/e2e_S-001")
        story = _make_story("S-001", "e2e")

        orchestrator._run_e2e_gate(story, {})

        # _pause_card 會呼叫 update_card_status("paused") 和 clear_card_owner
        mock_update.assert_any_call("S-001", "paused")
        self.assertTrue(mock_send.called)
        msg = mock_send.call_args[0][0]
        self.assertIn("FAIL", msg.upper())
        mock_teardown.assert_called_once()


# ══════════════════════════════════════════════════════════════════════
# 測試 5：_process_telegram_approvals()
# ══════════════════════════════════════════════════════════════════════

class TestProcessTelegramApprovals(unittest.TestCase):

    @patch("telegram_bot.send_message")
    @patch("github_api.merge_dev_to_main")
    @patch("db.insert_event")
    @patch("db.clear_card_owner")
    @patch("db.update_card_status")
    @patch("db.get_card")
    @patch("telegram_bot.get_pending_approvals", return_value=["S-001"])
    def test_approval_delivers_story(
        self, mock_approvals, mock_get, mock_update, mock_clear,
        mock_event, mock_merge, mock_send
    ):
        """Telegram /approve S-001 → merge_dev_to_main → story delivered"""
        mock_get.return_value = _make_story("S-001", "done")

        orchestrator._process_telegram_approvals({})

        mock_merge.assert_called_once_with("S-001")
        mock_update.assert_called_once_with("S-001", "delivered")
        mock_clear.assert_called_once_with("S-001")
        self.assertTrue(mock_send.called)
        msg = mock_send.call_args[0][0]
        self.assertIn("S-001", msg)

    @patch("telegram_bot.send_message")
    @patch("github_api.merge_dev_to_main")
    @patch("db.get_card")
    @patch("telegram_bot.get_pending_approvals", return_value=["S-001"])
    def test_approval_rejected_if_not_done(
        self, mock_approvals, mock_get, mock_merge, mock_send
    ):
        """Story 不是 done 狀態時，/approve 應被拒絕（不 merge）"""
        mock_get.return_value = _make_story("S-001", "e2e")  # 還沒跑完 E2E

        orchestrator._process_telegram_approvals({})

        mock_merge.assert_not_called()
        # 應傳送拒絕訊息
        self.assertTrue(mock_send.called)
        msg = mock_send.call_args[0][0]
        self.assertIn("e2e", msg)

    @patch("telegram_bot.send_message")
    @patch("github_api.merge_dev_to_main")
    @patch("db.get_card")
    @patch("telegram_bot.get_pending_approvals", return_value=["S-999"])
    def test_approval_for_unknown_story_is_ignored(
        self, mock_approvals, mock_get, mock_merge, mock_send
    ):
        """/approve 對不存在的 story → 傳送錯誤訊息，不 merge"""
        mock_get.return_value = None

        orchestrator._process_telegram_approvals({})

        mock_merge.assert_not_called()
        self.assertTrue(mock_send.called)


# ══════════════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════════════

def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestCheckStoryCompletion,
        TestRunE2EGate,
        TestProcessTelegramApprovals,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    passed = total - failed
    print(f"\n=== M5 Acceptance: {passed}/{total} passed ===")
    if failed == 0:
        print("PASS")
        sys.exit(0)
    else:
        print("FAIL")
        sys.exit(1)


if __name__ == "__main__":
    main()
