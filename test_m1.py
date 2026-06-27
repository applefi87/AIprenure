"""
test_m1.py - M1 acceptance test
================================
Run this on your Windows machine (not in Docker):
    cd D:\GitHub\AIprenure
    python test_m1.py

What it does:
  1. Loads GEMINI_API_KEY from .env
  2. Inserts a story card with status=awaiting_approval
  3. Calls the Spec agent directly (same code orchestrator uses)
  4. Validates the JSON output
  5. Writes ACs + task cards to DB
  6. Prints a summary

Pass criteria:
  - Gemini returns valid JSON with contract, acceptance_criteria, cards
  - DB has >= 1 AC and >= 1 task card linked to the story
"""

import json
import sys
from pathlib import Path

# Load .env
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import db
import agents
import orchestrator as orch

# Use local data/ DB
db.init_db()

STORY_ID = "S-M1-TEST"
REQUIREMENT = "Add a GET /health endpoint that returns {\"status\": \"ok\", \"version\": \"1.0\"}"

def seed_story():
    """Insert test story if not already there."""
    existing = db.get_card(STORY_ID)
    if existing:
        if existing["status"] in ("awaiting_approval", "refining"):
            print(f"[seed] Story {STORY_ID} already exists with status={existing['status']}, using it")
            return
        else:
            print(f"[seed] Story {STORY_ID} exists with status={existing['status']}, skipping re-seed")
            return
    db.insert_card(
        card_id=STORY_ID,
        card_type="story",
        title="[M1 TEST] Health endpoint",
        body=REQUIREMENT,
        status="awaiting_approval",
        branch="feature/S-M1-TEST",
    )
    db.insert_event("seed", "human", card_id=STORY_ID, new_status="awaiting_approval")
    print(f"[seed] Inserted story {STORY_ID} (awaiting_approval)")


def run_spec_agent_direct():
    """Call Spec agent directly and validate output."""
    print(f"\n[spec] Calling Gemini Spec agent for: {REQUIREMENT[:60]}...")

    context = {"story_id": STORY_ID, "requirement": REQUIREMENT}
    result = agents.call_reasoning_agent("spec", context)

    print("[spec] Raw output:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def validate_and_write(result: dict):
    """Validate JSON schema and write to DB."""
    assert "contract" in result, "Missing 'contract'"
    assert "acceptance_criteria" in result and result["acceptance_criteria"], "Missing/empty acceptance_criteria"
    assert "cards" in result and result["cards"], "Missing/empty cards"
    print(f"\n[validate] Schema OK: contract present, {len(result['acceptance_criteria'])} ACs, {len(result['cards'])} tasks")

    # Write ACs
    for ac in result["acceptance_criteria"]:
        db.insert_ac(
            card_id=STORY_ID,
            text=ac["text"],
            kind=ac.get("kind", "functional"),
            source=ac.get("source", "po"),
        )

    # Write task cards
    task_ids = []
    for i, task in enumerate(result["cards"], start=1):
        task_id = f"T-M1-{i}"
        try:
            db.insert_card(
                card_id=task_id,
                card_type="task",
                title=task["title"],
                body=task.get("body", ""),
                parent_id=STORY_ID,
                status="todo",
                branch=f"card/{task_id}",
            )
            task_ids.append(task_id)
        except Exception as e:
            print(f"  [warn] {task_id} already exists, skipping: {e}")

    # Advance story to refined
    db.update_card_status(STORY_ID, "refined", owner=None)
    db.insert_event(
        event_type="spec_done",
        actor="test_m1",
        card_id=STORY_ID,
        old_status="awaiting_approval",
        new_status="refined",
        metadata={"contract": result["contract"], "task_ids": task_ids},
    )
    return task_ids


def print_summary(task_ids):
    """Print final DB state."""
    story = db.get_card(STORY_ID)
    acs = db.get_ac_for_card(STORY_ID)
    print(f"\n{'='*50}")
    print(f"M1 PASS - Story {STORY_ID}")
    print(f"  status  : {story['status']}")
    print(f"  ACs     : {len(acs)}")
    for ac in acs:
        print(f"    [{ac['kind']}] {ac['text']}")
    print(f"  Tasks   : {task_ids}")
    for tid in task_ids:
        t = db.get_card(tid)
        if t:
            print(f"    {t['id']}: {t['title']} ({t['status']})")
    print('='*50)


if __name__ == "__main__":
    try:
        seed_story()
        result = run_spec_agent_direct()
        task_ids = validate_and_write(result)
        print_summary(task_ids)
        print("\nM1 acceptance test PASSED")
    except AssertionError as e:
        print(f"\nFAIL: Schema validation error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nFAIL: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
