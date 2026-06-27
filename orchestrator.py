"""
orchestrator.py - Main loop
============================
Iron rules:
  1. Only this file holds the DB connection and changes card status.
  2. Agents output JSON or modify files in containers - never touch DB.
  3. Every state transition requires proof from truth.py (exit code / git query).
  4. Tests must go red before green - enforced by truth.py, not agent self-report.

M0: skeleton with atomic claim and stub workers
M1: Spec agent wired in (awaiting_approval -> refined)
M2+: coding agents, containers, git
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

import db
import agents
import truth

# Load .env from project root
load_dotenv(Path(__file__).parent / ".env")

# ------------------------------------------------------------------ #
#  Config                                                              #
# ------------------------------------------------------------------ #

CONFIG_PATH = Path(__file__).parent / "company.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------ #
#  Logging                                                             #
# ------------------------------------------------------------------ #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("orchestrator")


# ------------------------------------------------------------------ #
#  Worker tracking                                                     #
# ------------------------------------------------------------------ #

@dataclass
class Worker:
    worker_id: str
    card_id: str
    card_status: str
    started_at: float = field(default_factory=time.time)
    process: Optional[object] = None  # M2+: real subprocess


_active_workers: dict[str, Worker] = {}


def count_active_workers() -> int:
    return len(_active_workers)


def _remove_worker(worker_id: str) -> None:
    _active_workers.pop(worker_id, None)


# ------------------------------------------------------------------ #
#  Eligible transitions                                                #
# ------------------------------------------------------------------ #

# (eligible_status, next_status, card_type)
ELIGIBLE_TRANSITIONS = [
    # Story: Spec agent path
    ("awaiting_approval", "refining", "story"),
    # Task: TDD path (M2)
    ("todo",        "test_writing", "task"),
    ("coding",      "coding",       "task"),
    # Task: GitHub PR + CI polling (M3)
    ("in_review",   "in_review",    "task"),
]


# ------------------------------------------------------------------ #
#  Main loop                                                           #
# ------------------------------------------------------------------ #

def main_loop(cfg: dict) -> None:
    poll = cfg.get("poll_interval_sec", 3)
    pool_size = cfg.get("pool_size", 2)

    log.info("Orchestrator started  poll=%ds  pool=%d", poll, pool_size)
    db.init_db()

    while True:
        try:
            time.sleep(poll)
            collect_finished_workers()
            free_slots = pool_size - count_active_workers()
            for _ in range(free_slots):
                if not try_claim_next(cfg):
                    break
        except KeyboardInterrupt:
            log.info("Interrupted, shutting down.")
            break
        except Exception as exc:
            log.exception("Main loop error: %s", exc)
            time.sleep(poll)


def try_claim_next(cfg: dict) -> bool:
    for eligible_status, next_status, card_type in ELIGIBLE_TRANSITIONS:
        worker_id = f"w-{uuid.uuid4().hex[:8]}"
        card = db.atomic_claim(
            eligible_status=eligible_status,
            next_status=next_status,
            worker_id=worker_id,
            card_type=card_type,
        )
        if card:
            log.info(
                "Claimed %s %s (%s -> %s) worker=%s",
                card["type"], card["id"], eligible_status, next_status, worker_id,
            )
            db.insert_event(
                event_type="claim",
                actor="orchestrator",
                card_id=card["id"],
                old_status=eligible_status,
                new_status=next_status,
                metadata={"worker_id": worker_id},
            )
            spawn_worker(card, worker_id, cfg)
            return True
    return False


def spawn_worker(card: dict, worker_id: str, cfg: dict) -> None:
    worker = Worker(
        worker_id=worker_id,
        card_id=card["id"],
        card_status=card["status"],
    )
    _active_workers[worker_id] = worker
    handle_card(card, worker, cfg)


def handle_card(card: dict, worker: Worker, cfg: dict) -> None:
    """Route card to the right handler based on type and status."""
    try:
        if card["type"] == "story":
            handle_story(card, cfg)
        elif card["type"] == "task":
            handle_task(card, cfg)
        else:
            log.warning("Unknown card type %s for %s", card["type"], card["id"])
            db.clear_card_owner(card["id"])
    finally:
        _remove_worker(worker.worker_id)


# ------------------------------------------------------------------ #
#  Story handler                                                       #
# ------------------------------------------------------------------ #

def handle_story(card: dict, cfg: dict) -> None:
    status = card["status"]

    if status == "refining":
        _run_spec_agent(card, cfg)
    else:
        log.warning("[STUB] No handler for story status=%s (%s)", status, card["id"])
        db.clear_card_owner(card["id"])


def _run_spec_agent(card: dict, cfg: dict) -> None:
    """
    M1: Call Spec agent (Gemini) to decompose a requirement.
    On success: insert ACs + task cards, advance story to 'refined'.
    On failure: pause card and notify.
    """
    log.info("Running Spec agent for story %s", card["id"])

    context = {
        "story_id": card["id"],
        "requirement": card["body"] or card["title"],
    }

    try:
        result = agents.call_reasoning_agent("spec", context)
    except Exception as e:
        log.error("Spec agent failed for %s: %s", card["id"], e)
        _pause_card(card["id"], f"Spec agent error: {e}")
        return

    # Validate schema
    if not _validate_spec_output(result, card["id"]):
        _pause_card(card["id"], "Spec agent returned invalid JSON schema")
        return

    # Write ACs to DB
    for ac in result["acceptance_criteria"]:
        db.insert_ac(
            card_id=card["id"],
            text=ac["text"],
            kind=ac.get("kind", "functional"),
            source=ac.get("source", "po"),
        )

    # Write task cards to DB
    task_ids = []
    for i, task in enumerate(result["cards"], start=1):
        task_id = _next_task_id(card["id"], i)
        branch = f"card/{task_id}"
        try:
            db.insert_card(
                card_id=task_id,
                card_type="task",
                title=task["title"],
                body=task.get("body", ""),
                parent_id=card["id"],
                status="todo",
                branch=branch,
            )
            task_ids.append(task_id)
            log.info("  Created task %s: %s", task_id, task["title"])
        except Exception as e:
            log.warning("  Could not insert task %s: %s", task_id, e)

    # Update story body with contract summary, advance to refined
    db.update_card_status(card["id"], "refined")
    db.clear_card_owner(card["id"])
    db.insert_event(
        event_type="spec_done",
        actor="orchestrator",
        card_id=card["id"],
        old_status="refining",
        new_status="refined",
        metadata={
            "contract": result.get("contract", ""),
            "ac_count": len(result["acceptance_criteria"]),
            "task_count": len(task_ids),
            "task_ids": task_ids,
        },
    )
    log.info(
        "Story %s refined: %d ACs, %d tasks created %s",
        card["id"],
        len(result["acceptance_criteria"]),
        len(task_ids),
        task_ids,
    )


def _validate_spec_output(result: dict, card_id: str) -> bool:
    """Check Spec agent output has required fields."""
    required = ["contract", "acceptance_criteria", "cards"]
    for key in required:
        if key not in result:
            log.error("Spec output missing key '%s' for %s", key, card_id)
            return False
    if not result["acceptance_criteria"]:
        log.error("Spec output has empty acceptance_criteria for %s", card_id)
        return False
    if not result["cards"]:
        log.error("Spec output has empty cards for %s", card_id)
        return False
    return True


def _next_task_id(story_id: str, index: int) -> str:
    """Generate task ID from story ID. S-001 -> T-001-1, T-001-2, ..."""
    num = story_id.split("-")[-1] if "-" in story_id else "000"
    return f"T-{num}-{index}"


def _pause_card(card_id: str, reason: str) -> None:
    db.update_card_status(card_id, "paused")
    db.clear_card_owner(card_id)
    db.insert_event(
        event_type="paused",
        actor="orchestrator",
        card_id=card_id,
        new_status="paused",
        metadata={"reason": reason},
    )
    log.warning("Card %s paused: %s", card_id, reason)


# ------------------------------------------------------------------ #
#  Task handler — M2 TDD red-green loop                               #
# ------------------------------------------------------------------ #

def handle_task(card: dict, cfg: dict) -> None:
    status = card["status"]

    if status == "test_writing":
        _run_test_agent(card, cfg)
    elif status == "coding":
        _run_code_agent(card, cfg)
    elif status == "in_review":
        _handle_in_review(card, cfg)
    else:
        log.warning("No task handler for status=%s (%s)", status, card["id"])
        db.clear_card_owner(card["id"])


def _inject_acs(card: dict) -> dict:
    """Return a copy of card with _acceptance_criteria fetched from DB."""
    card = dict(card)
    card["_acceptance_criteria"] = db.get_ac_for_card(card["id"])
    return card


def _run_test_agent(card: dict, cfg: dict) -> None:
    """
    1. Run Test agent in container (writes failing tests, commits).
    2. Verify tests actually FAIL (red) -- orchestrator enforces TDD.
    3. On confirmed red  -> advance to coding.
    4. On not-red        -> back to todo (bad tests, retry).
    5. On agent error    -> retry or pause.
    """
    card = _inject_acs(card)
    log.info("Running Test agent for task %s", card["id"])

    try:
        result = agents.run_coding_agent(card, "test")
    except Exception as e:
        log.error("Test agent raised exception for %s: %s", card["id"], e)
        _handle_task_retry(card, cfg, f"Test agent exception: {e}")
        return

    if result["exit_code"] != 0:
        log.error(
            "Test agent process failed  card=%s  exit_code=%d",
            card["id"], result["exit_code"],
        )
        _handle_task_retry(card, cfg, f"Test agent exited {result[chr(39) + chr(39) + 'exit_code' + chr(39) + chr(39)]}")
        return

    # Verify tests actually fail before implementation (TDD enforcement)
    wt = truth.worktree_path(card)
    if not truth.tests_fail(card, wt):
        log.warning(
            "Tests did NOT fail for %s -- agent wrote bad tests; resetting to todo",
            card["id"],
        )
        db.increment_card_counter(card["id"], "loop_count")
        db.update_card_status(card["id"], "todo")
        db.clear_card_owner(card["id"])
        db.insert_event(
            event_type="tests_not_red",
            actor="orchestrator",
            card_id=card["id"],
            old_status="test_writing",
            new_status="todo",
            metadata={"reason": "tests passed before implementation"},
        )
        return

    # Tests confirmed red -> advance to coding
    db.update_card_status(card["id"], "coding")
    db.clear_card_owner(card["id"])
    db.insert_event(
        event_type="tests_red",
        actor="orchestrator",
        card_id=card["id"],
        old_status="test_writing",
        new_status="coding",
    )
    log.info("Task %s tests confirmed RED -> coding", card["id"])


def _run_code_agent(card: dict, cfg: dict) -> None:
    """
    1. Run Code agent in container (implements until tests pass).
    2. Verify tests PASS (green) -- orchestrator enforces green before review.
    3. On confirmed green -> advance to in_review.
    4. On still-failing  -> retry coding or pause.
    5. On agent error    -> retry or pause.
    """
    card = _inject_acs(card)
    log.info("Running Code agent for task %s", card["id"])

    try:
        result = agents.run_coding_agent(card, "code")
    except Exception as e:
        log.error("Code agent raised exception for %s: %s", card["id"], e)
        _handle_task_retry(card, cfg, f"Code agent exception: {e}")
        return

    # Verify tests pass
    wt = truth.worktree_path(card)
    if not truth.tests_pass(card, wt):
        db.increment_card_counter(card["id"], "loop_count")
        current = db.get_card(card["id"])
        loop_count = current["loop_count"] if current else 0
        review_max = cfg.get("limits", {}).get("review_max", 3)
        log.warning(
            "Tests still failing after code agent  card=%s  loop_count=%d",
            card["id"], loop_count,
        )
        if loop_count >= review_max:
            _pause_card(
                card["id"],
                f"Tests still failing after {loop_count} code attempts",
            )
        else:
            db.update_card_status(card["id"], "coding")
            db.clear_card_owner(card["id"])
            db.insert_event(
                event_type="tests_not_green",
                actor="orchestrator",
                card_id=card["id"],
                metadata={"loop_count": loop_count},
            )
        return

    # Tests green -> advance to in_review
    db.update_card_status(card["id"], "in_review")
    db.clear_card_owner(card["id"])
    db.insert_event(
        event_type="tests_green",
        actor="orchestrator",
        card_id=card["id"],
        old_status="coding",
        new_status="in_review",
    )
    log.info("Task %s tests GREEN -> in_review", card["id"])


def _handle_task_retry(card: dict, cfg: dict, reason: str) -> None:
    """Increment retry_count; pause if exhausted, else reset to todo."""
    db.increment_card_counter(card["id"], "retry_count")
    current = db.get_card(card["id"])
    retry_count = current["retry_count"] if current else 0
    retry_max = cfg.get("limits", {}).get("retry_max", 2)

    if retry_count >= retry_max:
        _pause_card(card["id"], f"Max retries ({retry_max}) reached: {reason}")
    else:
        db.update_card_status(card["id"], "todo")
        db.clear_card_owner(card["id"])
        db.insert_event(
            event_type="retry",
            actor="orchestrator",
            card_id=card["id"],
            new_status="todo",
            metadata={"reason": reason, "retry_count": retry_count},
        )
        log.info("Task %s retry %d/%d: %s", card["id"], retry_count, retry_max, reason)


def _handle_in_review(card: dict, cfg: dict) -> None:
    """
    M3: GitHub PR + CI polling loop.

    State machine (card stays in_review between polls):
      1. No PR yet       -> push branch + create PR -> release
      2. CI pending      -> release (poll again next cycle)
      3. CI success      -> trigger merge -> done
      4. CI failure      -> reset to coding
    """
    import github_api

    card_id = card["id"]
    wt = truth.worktree_path(card)

    # Step 1: create PR if not yet done
    pr_number = card.get("pr_number")
    if not pr_number:
        existing = github_api.get_pr_for_branch(card)
        if existing:
            pr_number = existing["number"]
            db.update_card_status(card_id, "in_review", pr_number=pr_number)
            log.info("Found existing PR #%d for %s", pr_number, card_id)
        else:
            try:
                github_api.push_branch(card, wt)
                pr_number = github_api.create_pr(card)
                db.update_card_status(card_id, "in_review", pr_number=pr_number)
                db.insert_event(
                    event_type="pr_created",
                    actor="orchestrator",
                    card_id=card_id,
                    metadata={"pr_number": pr_number},
                )
                log.info("PR #%d created for %s -- CI starting", pr_number, card_id)
            except Exception as e:
                log.error("Failed to push/create PR for %s: %s", card_id, e)
                _handle_task_retry(card, cfg, "PR creation failed: " + str(e))
                return
        db.clear_card_owner(card_id)
        return  # release; next poll checks CI

    # Step 2-4: PR exists, check CI
    try:
        ci_status = github_api.get_ci_status(pr_number)
    except Exception as e:
        log.error("CI status check failed for %s PR#%d: %s", card_id, pr_number, e)
        db.clear_card_owner(card_id)
        return

    if ci_status == "pending":
        log.info("CI pending for %s PR#%d -- releasing for next poll", card_id, pr_number)
        db.clear_card_owner(card_id)
        return

    if ci_status == "success":
        try:
            github_api.trigger_merge(pr_number, card_id)
        except Exception as e:
            log.error("Merge trigger failed for %s PR#%d: %s", card_id, pr_number, e)
            db.clear_card_owner(card_id)
            return

        merged = github_api.wait_for_merge(pr_number, max_polls=20, interval_sec=15)
        if merged:
            db.update_card_status(card_id, "done")
            db.clear_card_owner(card_id)
            db.insert_event(
                event_type="merged",
                actor="orchestrator",
                card_id=card_id,
                old_status="in_review",
                new_status="done",
                metadata={"pr_number": pr_number},
            )
            log.info("Task %s merged and done (PR #%d)", card_id, pr_number)
            truth.teardown_worktree(card)
        else:
            log.warning("Merge wait timed out for %s PR#%d", card_id, pr_number)
            db.clear_card_owner(card_id)
        return

    # ci_status == "failure"
    log.warning("CI failed for %s PR#%d -- resetting to coding", card_id, pr_number)
    db.update_card_status(card_id, "coding", pr_number=None)
    db.clear_card_owner(card_id)
    db.insert_event(
        event_type="ci_failed",
        actor="orchestrator",
        card_id=card_id,
        old_status="in_review",
        new_status="coding",
        metadata={"pr_number": pr_number},
    )


def collect_finished_workers() -> None:
    # Synchronous in M0-M3; future async poll goes here
    pass


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    cfg = load_config()
    main_loop(cfg)
