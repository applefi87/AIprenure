"""
test_m2.py -- M2 acceptance test
=================================
Verifies the TDD red-green state machine WITHOUT actually running Docker.

Strategy: mock truth.run_in_container to return controlled exit codes,
then call orchestrator._run_test_agent / _run_code_agent directly and
assert DB state transitions are correct.

Run:
    python test_m2.py
"""

import os
import sys
import sqlite3
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Bootstrap: use a throw-away DB so tests do not touch production data
# ---------------------------------------------------------------------------

_TMP = Path(tempfile.mkdtemp(prefix="m2_test_"))
_DB = _TMP / "test.db"

# Patch DB_PATH before importing project modules
import db as _db_mod
_db_mod.DB_PATH = _DB

sys.path.insert(0, str(Path(__file__).parent))

import db
import orchestrator
import truth

db.DB_PATH = _DB  # ensure both references point to temp DB


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

import uuid

def _setup_db():
    """Create a fresh test DB."""
    new_db = _TMP / f"test_{uuid.uuid4().hex}.db"
    db.DB_PATH = new_db
    db.init_db()


def _seed_task(task_id: str = "T-M2-1", story_id: str = "S-M2") -> dict:
    """Seed a story + task card ready for M2 TDD loop."""
    # story
    try:
        db.insert_card(
            card_id=story_id, card_type="story",
            title="M2 test story", body="Build a health endpoint",
            status="developing", branch=f"feature/{story_id}",
        )
    except sqlite3.IntegrityError:
        pass

    # AC
    db.insert_ac(card_id=story_id, text="GET /health returns 200")
    ac_id = db.get_ac_for_card(story_id)[0]["id"]

    # task
    try:
        db.insert_card(
            card_id=task_id, card_type="task",
            title="Add GET /health endpoint",
            body="Must return {status: ok}",
            parent_id=story_id, status="todo",
            branch=f"card/{task_id}",
        )
    except sqlite3.IntegrityError:
        pass

    # propagate AC to task
    db.insert_ac(card_id=task_id, text="GET /health returns 200")

    card = db.get_card(task_id)
    assert card is not None
    return card


cfg = {
    "limits": {"max_turns": 5, "worker_timeout_sec": 60, "review_max": 3, "retry_max": 2},
    "docker": {"worker_image": "ai-company-worker"},
    "coding_model": "claude-sonnet-4-6",
}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestTDDStateMachine(unittest.TestCase):

    def setUp(self):
        _setup_db()

    # -----------------------------------------------------------------------
    # Test agent: happy path
    # -----------------------------------------------------------------------

    def test_test_agent_advances_to_coding_when_tests_fail(self):
        """
        Test agent runs OK (exit 0) and tests actually fail (exit != 0)
        -> status must become coding.
        """
        card = _seed_task()
        # Claim it as test_writing
        card["status"] = "test_writing"
        db.update_card_status(card["id"], "test_writing", owner="w-test")

        # agent exits 0 (ran OK); tests exit 1 (they fail = good)
        agent_result = {"exit_code": 0, "stdout": "DONE", "stderr": ""}
        container_result_red = MagicMock(exit_code=1, stdout="FAILED 1", stderr="")

        with patch.object(orchestrator.agents, "run_coding_agent", return_value=agent_result), \
             patch.object(orchestrator.truth, "run_in_container", return_value=container_result_red), \
             patch.object(orchestrator.truth, "worktree_path", return_value=_TMP), \
             patch.object(orchestrator.truth, "setup_worktree", return_value=_TMP):
            orchestrator._run_test_agent(card, cfg)

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "coding",
                         "Expected coding, got " + str(updated.get("status")))
        self.assertIsNone(updated["owner"])
        events = db.get_events(card["id"])
        types = [e["event_type"] for e in events]
        self.assertIn("tests_red", types)

    # -----------------------------------------------------------------------
    # Test agent: bad tests (green before impl)
    # -----------------------------------------------------------------------

    def test_test_agent_resets_to_todo_when_tests_pass_prematurely(self):
        """
        Test agent writes tests that pass even without implementation
        -> status must go back to todo.
        """
        card = _seed_task()
        card["status"] = "test_writing"
        db.update_card_status(card["id"], "test_writing", owner="w-test")

        agent_result = {"exit_code": 0, "stdout": "DONE", "stderr": ""}
        container_result_green = MagicMock(exit_code=0, stdout="passed", stderr="")

        with patch.object(orchestrator.agents, "run_coding_agent", return_value=agent_result), \
             patch.object(orchestrator.truth, "run_in_container", return_value=container_result_green), \
             patch.object(orchestrator.truth, "worktree_path", return_value=_TMP), \
             patch.object(orchestrator.truth, "setup_worktree", return_value=_TMP):
            orchestrator._run_test_agent(card, cfg)

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "todo")
        events = db.get_events(card["id"])
        types = [e["event_type"] for e in events]
        self.assertIn("tests_not_red", types)

    # -----------------------------------------------------------------------
    # Code agent: happy path
    # -----------------------------------------------------------------------

    def test_code_agent_advances_to_in_review_when_tests_pass(self):
        """
        Code agent runs OK and tests pass -> status must become in_review.
        """
        card = _seed_task()
        card["status"] = "coding"
        db.update_card_status(card["id"], "coding", owner="w-code")

        agent_result = {"exit_code": 0, "stdout": "DONE", "stderr": ""}
        container_result_green = MagicMock(exit_code=0, stdout="passed", stderr="")

        with patch.object(orchestrator.agents, "run_coding_agent", return_value=agent_result), \
             patch.object(orchestrator.truth, "run_in_container", return_value=container_result_green), \
             patch.object(orchestrator.truth, "worktree_path", return_value=_TMP), \
             patch.object(orchestrator.truth, "setup_worktree", return_value=_TMP):
            orchestrator._run_code_agent(card, cfg)

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "in_review")
        self.assertIsNone(updated["owner"])
        events = db.get_events(card["id"])
        types = [e["event_type"] for e in events]
        self.assertIn("tests_green", types)

    # -----------------------------------------------------------------------
    # Code agent: tests still failing
    # -----------------------------------------------------------------------

    def test_code_agent_stays_coding_when_tests_still_fail(self):
        """
        Code agent runs but tests still fail -> status stays coding (for retry).
        """
        card = _seed_task()
        card["status"] = "coding"
        db.update_card_status(card["id"], "coding", owner="w-code")

        agent_result = {"exit_code": 0, "stdout": "DONE", "stderr": ""}
        container_result_red = MagicMock(exit_code=1, stdout="FAILED", stderr="")

        with patch.object(orchestrator.agents, "run_coding_agent", return_value=agent_result), \
             patch.object(orchestrator.truth, "run_in_container", return_value=container_result_red), \
             patch.object(orchestrator.truth, "worktree_path", return_value=_TMP), \
             patch.object(orchestrator.truth, "setup_worktree", return_value=_TMP):
            orchestrator._run_code_agent(card, cfg)

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "coding")
        events = db.get_events(card["id"])
        types = [e["event_type"] for e in events]
        self.assertIn("tests_not_green", types)

    # -----------------------------------------------------------------------
    # Retry / pause logic
    # -----------------------------------------------------------------------

    def test_task_paused_after_max_retries(self):
        """
        If agent keeps failing, card should be paused after retry_max attempts.
        """
        card = _seed_task()
        card["status"] = "test_writing"
        db.update_card_status(card["id"], "test_writing", owner="w-test")

        # Exhaust retries manually
        for _ in range(cfg["limits"]["retry_max"]):
            db.increment_card_counter(card["id"], "retry_count")

        boom = Exception("container exploded")
        with patch.object(orchestrator.agents, "run_coding_agent", side_effect=boom):
            orchestrator._run_test_agent(card, cfg)

        updated = db.get_card(card["id"])
        self.assertEqual(updated["status"], "paused")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestTDDStateMachine)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    passed = result.testsRun - len(result.failures) - len(result.errors)
    total = result.testsRun
    print(f"\n=== M2 Acceptance: {passed}/{total} passed ===")
    if result.failures or result.errors:
        print("FAIL")
        sys.exit(1)
    else:
        print("PASS")

    # Cleanup
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
