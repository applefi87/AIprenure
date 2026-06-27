"""
test_m4.py -- M4 acceptance test
=================================
Verifies Review agent state machine WITHOUT calling real Gemini API.

Strategy: mock agents.call_reasoning_agent, call orchestrator._handle_in_review
directly, assert DB state transitions are correct.

Run:
    python test_m4.py
"""

import os
import sys
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Bootstrap: isolated DB per test
# ---------------------------------------------------------------------------

_TMP = Path(tempfile.mkdtemp(prefix="m4_test_"))

import db as _db_mod
_db_mod.DB_PATH = _TMP / "test.db"

sys.path.insert(0, str(Path(__file__).parent))

import db
import orchestrator
import truth
import agents

db.DB_PATH = _TMP / "test.db"


def _setup_db():
    if db.DB_PATH.exists():
        db.DB_PATH.unlink()
    db.init_db()


def _seed_in_review(task_id="T-M4-1", story_id="S-M4"):
    try:
        db.insert_card(card_id=story_id, card_type="story",
                       title="M4 story", body="", status="developing",
                       branch="feature/" + story_id)
    except sqlite3.IntegrityError:
        pass
    try:
        db.insert_card(card_id=task_id, card_type="task",
                       title="Add auth endpoint", body="",
                       parent_id=story_id, status="in_review",
                       branch="card/" + task_id)
    except sqlite3.IntegrityError:
        pass
    db.insert_ac(card_id=task_id, text="POST /login returns 200 with valid JWT")
    db.update_card_status(task_id, "in_review", owner="w-review")
    return db.get_card(task_id)


cfg = {
    "limits": {"max_turns": 5, "worker_timeout_sec": 60, "review_max": 3, "retry_max": 2},
    "coding_model": "claude-sonnet-4-6",
}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestM4ReviewAgent(unittest.TestCase):

    def setUp(self):
        _setup_db()

    # -----------------------------------------------------------------------
    # 1. Review PASS -> ready_to_merge
    # -----------------------------------------------------------------------

    def test_pass_advances_to_ready_to_merge(self):
        """Review agent returns pass -> card moves to ready_to_merge."""
        card = _seed_in_review()

        review_result = {"verdict": "pass", "reasons": []}

        with patch.object(agents, "call_reasoning_agent", return_value=review_result), \
             patch.object(truth, "worktree_path", return_value=_TMP), \
             patch.object(truth, "get_worktree_diff", return_value="diff content"):
            orchestrator._handle_in_review(card, cfg)

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "ready_to_merge")
        self.assertIsNone(updated["owner"])

        events = [e["event_type"] for e in db.get_events(card["id"])]
        self.assertIn("review_pass", events)

    # -----------------------------------------------------------------------
    # 2. Review FAIL -> back to coding
    # -----------------------------------------------------------------------

    def test_fail_resets_to_coding(self):
        """Review agent returns fail -> card goes back to coding."""
        card = _seed_in_review()

        review_result = {
            "verdict": "fail",
            "reasons": ["AC #1 has no test", "Missing 401 error case"],
        }

        with patch.object(agents, "call_reasoning_agent", return_value=review_result), \
             patch.object(truth, "worktree_path", return_value=_TMP), \
             patch.object(truth, "get_worktree_diff", return_value="diff content"):
            orchestrator._handle_in_review(card, cfg)

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "coding")
        self.assertIsNone(updated["owner"])
        # Rejection reasons injected into body
        self.assertIn("Review rejection", updated["body"] or "")

        events = [e["event_type"] for e in db.get_events(card["id"])]
        self.assertIn("review_fail", events)

    # -----------------------------------------------------------------------
    # 3. FAIL repeated to review_max -> paused
    # -----------------------------------------------------------------------

    def test_paused_after_max_review_failures(self):
        """After review_max failures, card is paused."""
        card = _seed_in_review()

        # Pre-exhaust loop_count (review_max = 3, already at 3)
        for _ in range(cfg["limits"]["review_max"]):
            db.increment_card_counter(card["id"], "loop_count")

        review_result = {"verdict": "fail", "reasons": ["Still broken"]}

        with patch.object(agents, "call_reasoning_agent", return_value=review_result), \
             patch.object(truth, "worktree_path", return_value=_TMP), \
             patch.object(truth, "get_worktree_diff", return_value="diff"):
            orchestrator._handle_in_review(card, cfg)

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "paused")

    # -----------------------------------------------------------------------
    # 4. Agent error -> retry
    # -----------------------------------------------------------------------

    def test_agent_error_triggers_retry(self):
        """If Review agent throws, card is retried."""
        card = _seed_in_review()

        with patch.object(agents, "call_reasoning_agent", side_effect=RuntimeError("Gemini timeout")), \
             patch.object(truth, "worktree_path", return_value=_TMP), \
             patch.object(truth, "get_worktree_diff", return_value="diff"):
            orchestrator._handle_in_review(card, cfg)

        updated = db.get_card(card["id"])
        self.assertIn(updated["status"], ("todo", "paused"))
        self.assertGreater(updated["retry_count"], 0)

    # -----------------------------------------------------------------------
    # 5. Invalid verdict -> retry
    # -----------------------------------------------------------------------

    def test_invalid_verdict_triggers_retry(self):
        """If Review agent returns an unrecognized verdict, card is retried."""
        card = _seed_in_review()

        review_result = {"verdict": "maybe", "reasons": []}

        with patch.object(agents, "call_reasoning_agent", return_value=review_result), \
             patch.object(truth, "worktree_path", return_value=_TMP), \
             patch.object(truth, "get_worktree_diff", return_value="diff"):
            orchestrator._handle_in_review(card, cfg)

        updated = db.get_card(card["id"])
        self.assertGreater(updated["retry_count"], 0)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestM4ReviewAgent)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    passed = result.testsRun - len(result.failures) - len(result.errors)
    print("\n=== M4 Acceptance: " + str(passed) + "/" + str(result.testsRun) + " passed ===")
    if result.failures or result.errors:
        print("FAIL")
        sys.exit(1)
    else:
        print("PASS")

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
