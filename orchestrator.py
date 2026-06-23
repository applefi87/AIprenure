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
    # Task: TDD path (M2+)
    ("todo",   "test_writing", "task"),
    ("coding", "coding",       "task"),
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
    db.update_card_status(card["id"], "refined", owner=None)
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
    db.update_card_status(card_id, "paused", owner=None)
    db.insert_event(
        event_type="paused",
        actor="orchestrator",
        card_id=card_id,
        new_status="paused",
        metadata={"reason": reason},
    )
    log.warning("Card %s paused: %s", card_id, reason)


# ------------------------------------------------------------------ #
#  Task handler (M0 stub, M2+ real)                                    #
# ------------------------------------------------------------------ #

def handle_task(card: dict, cfg: dict) -> None:
    status = card["status"]

    if status == "test_writing":
        log.info("[M0-STUB] Test agent not yet implemented (M2), releasing %s", card["id"])
        db.clear_card_owner(card["id"])
        db.update_card_status(card["id"], "todo")

    elif status == "coding":
        log.info("[M0-STUB] Code agent not yet implemented (M2), releasing %s", card["id"])
        db.clear_card_owner(card["id"])

    else:
        log.warning("[STUB] No task handler for status=%s (%s)", status, card["id"])
        db.clear_card_owner(card["id"])


def collect_finished_workers() -> None:
    # Sync in M0/M1; M2+ polls subprocess exit codes
    pass


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    cfg = load_config()
    main_loop(cfg)
