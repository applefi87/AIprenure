"""
test_m3.py -- M3 acceptance test
=================================
Verifies GitHub PR + CI polling state machine WITHOUT hitting the real GitHub API.

Strategy: mock github_api functions, call orchestrator._handle_in_review directly,
assert DB state transitions are correct.

Run:
    python test_m3.py
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

_TMP = Path(tempfile.mkdtemp(prefix="m3_test_"))

import db as _db_mod
_db_mod.DB_PATH = _TMP / "test.db"

sys.path.insert(0, str(Path(__file__).parent))

import db
import orchestrator
import truth

db.DB_PATH = _TMP / "test.db"


def _setup_db():
    if db.DB_PATH.exists():
        db.DB_PATH.unlink()
    db.init_db()


def _seed_in_review(task_id="T-M3-1", story_id="S-M3", pr_number=None):
    """Seed a task already in in_review state."""
    try:
        db.insert_card(card_id=story_id, card_type="story",
                       title="M3 story", body="", status="developing",
                       branch="feature/" + story_id)
    except sqlite3.IntegrityError:
        pass

    try:
        db.insert_card(card_id=task_id, card_type="task",
                       title="Add health endpoint", body="",
                       parent_id=story_id, status="in_review",
                       branch="card/" + task_id)
    except sqlite3.IntegrityError:
        pass

    if pr_number:
        db.update_card_status(task_id, "in_review", pr_number=pr_number)

    card = db.get_card(task_id)
    assert card is not None
    return card


cfg = {
    "limits": {"max_turns": 5, "worker_timeout_sec": 60, "review_max": 3, "retry_max": 2},
    "docker": {"worker_image": "ai-company-worker"},
}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestM3GitHubPRFlow(unittest.TestCase):

    def setUp(self):
        _setup_db()

    # -----------------------------------------------------------------------
    # 1. No PR -> push branch + create PR -> release
    # -----------------------------------------------------------------------

    def test_creates_pr_when_none_exists(self):
        """When no PR exists, orchestrator pushes branch and creates PR."""
        card = _seed_in_review()
        card["status"] = "in_review"
        db.update_card_status(card["id"], "in_review", owner="w-test")
        card = db.get_card(card["id"])

        with patch("github_api.get_pr_for_branch", return_value=None), \
             patch("github_api.push_branch") as mock_push, \
             patch("github_api.create_pr", return_value=42) as mock_create, \
             patch.object(truth, "worktree_path", return_value=_TMP):
            orchestrator._handle_in_review(card, cfg)

        mock_push.assert_called_once()
        mock_create.assert_called_once()

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "in_review")
        self.assertEqual(updated["pr_number"], 42)
        self.assertIsNone(updated["owner"])

        events = [e["event_type"] for e in db.get_events(card["id"])]
        self.assertIn("pr_created", events)

    # -----------------------------------------------------------------------
    # 2. PR exists, CI pending -> release (poll later)
    # -----------------------------------------------------------------------

    def test_releases_when_ci_pending(self):
        """When CI is still pending, card is released for next poll."""
        card = _seed_in_review(pr_number=99)
        card = db.get_card(card["id"])
        db.update_card_status(card["id"], "in_review", owner="w-test")

        with patch("github_api.get_ci_status", return_value="pending"), \
             patch.object(truth, "worktree_path", return_value=_TMP):
            orchestrator._handle_in_review(card, cfg)

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "in_review")
        self.assertIsNone(updated["owner"])

    # -----------------------------------------------------------------------
    # 3. PR exists, CI success -> trigger merge -> done
    # -----------------------------------------------------------------------

    def test_merges_and_marks_done_when_ci_passes(self):
        """When CI is green, orchestrator triggers merge and marks card done."""
        card = _seed_in_review(pr_number=77)
        card = db.get_card(card["id"])
        db.update_card_status(card["id"], "in_review", owner="w-test")

        with patch("github_api.get_ci_status", return_value="success"), \
             patch("github_api.trigger_merge") as mock_merge, \
             patch("github_api.wait_for_merge", return_value=True), \
             patch.object(truth, "worktree_path", return_value=_TMP), \
             patch.object(truth, "teardown_worktree"):
            orchestrator._handle_in_review(card, cfg)

        mock_merge.assert_called_once_with(77, card["id"])

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "done")
        self.assertIsNone(updated["owner"])

        events = [e["event_type"] for e in db.get_events(card["id"])]
        self.assertIn("merged", events)

    # -----------------------------------------------------------------------
    # 4. CI failure -> reset to coding
    # -----------------------------------------------------------------------

    def test_resets_to_coding_when_ci_fails(self):
        """When CI fails, card goes back to coding for re-implementation."""
        card = _seed_in_review(pr_number=55)
        card = db.get_card(card["id"])
        db.update_card_status(card["id"], "in_review", owner="w-test")

        with patch("github_api.get_ci_status", return_value="failure"), \
             patch.object(truth, "worktree_path", return_value=_TMP):
            orchestrator._handle_in_review(card, cfg)

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "coding")
        self.assertIsNone(updated["owner"])

        events = [e["event_type"] for e in db.get_events(card["id"])]
        self.assertIn("ci_failed", events)

    # -----------------------------------------------------------------------
    # 5. PR creation error -> retry or pause
    # -----------------------------------------------------------------------

    def test_retry_on_push_failure(self):
        """If git push fails, card is retried (retry_count incremented)."""
        card = _seed_in_review()
        card = db.get_card(card["id"])
        db.update_card_status(card["id"], "in_review", owner="w-test")

        with patch("github_api.get_pr_for_branch", return_value=None), \
             patch("github_api.push_branch", side_effect=RuntimeError("push failed")), \
             patch.object(truth, "worktree_path", return_value=_TMP):
            orchestrator._handle_in_review(card, cfg)

        updated = db.get_card(card["id"])
        self.assertIn(updated["status"], ("todo", "paused"))
        self.assertGreater(updated["retry_count"], 0)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestM3GitHubPRFlow)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    passed = result.testsRun - len(result.failures) - len(result.errors)
    print("\n=== M3 Acceptance: " + str(passed) + "/" + str(result.testsRun) + " passed ===")
    if result.failures or result.errors:
        print("FAIL")
        sys.exit(1)
    else:
        print("PASS")

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
