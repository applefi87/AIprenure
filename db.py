"""
db.py — 唯一 import sqlite3 的檔案。
所有 SQL 都在這裡，編排器透過這支檔案存取 DB，agent 不得直接呼叫。
"""

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "data" / "company.db"
SCHEMA_PATH = Path(__file__).parent / "db" / "schema.sql"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """首次啟動時建表（逐句 execute，避免 executescript 在 WAL 模式下的 disk I/O 問題）。"""
    schema_full = SCHEMA_PATH.read_text(encoding="utf-8")
    # 過濾 PRAGMA 與注解，拆成獨立 statement
    stmts = []
    current = []
    for line in schema_full.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("PRAGMA") or stripped.startswith("--") or not stripped:
            continue
        current.append(line)
        if stripped.endswith(";"):
            stmts.append("\n".join(current))
            current = []
    with get_conn() as conn:
        for stmt in stmts:
            conn.execute(stmt)
    print(f"[db] 初始化完成：{DB_PATH}")


# ─── 卡片操作 ─────────────────────────────────────────────────────────────

def get_card(card_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM cards WHERE id = ?", (card_id,)
        ).fetchone()
    return dict(row) if row else None


def list_cards(status: Optional[str] = None) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM cards WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cards ORDER BY created_at"
            ).fetchall()
    return [dict(r) for r in rows]


def insert_card(
    card_id: str,
    card_type: str,
    title: str,
    body: str = "",
    parent_id: Optional[str] = None,
    status: str = "todo",
    branch: Optional[str] = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO cards (id, type, parent_id, title, body, status, branch)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (card_id, card_type, parent_id, title, body, status, branch),
        )


def update_card_status(
    card_id: str,
    new_status: str,
    owner: Optional[str] = None,
    pr_number: Optional[int] = None,
    verified: Optional[int] = None,
) -> None:
    fields = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    values = [new_status]

    if owner is not None:
        fields.append("owner = ?")
        values.append(owner)
    if pr_number is not None:
        fields.append("pr_number = ?")
        values.append(pr_number)
    if verified is not None:
        fields.append("verified = ?")
        values.append(verified)

    values.append(card_id)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE cards SET {', '.join(fields)} WHERE id = ?", values
        )


def increment_card_counter(card_id: str, field: str) -> None:
    """field: 'loop_count' 或 'retry_count'"""
    assert field in ("loop_count", "retry_count")
    with get_conn() as conn:
        conn.execute(
            f"UPDATE cards SET {field} = {field} + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (card_id,),
        )


def clear_card_owner(card_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE cards SET owner = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (card_id,),
        )


# ─── 原子認領 ────────────────────────────────────────────────────────────

def atomic_claim(
    eligible_status: str,
    next_status: str,
    worker_id: str,
    card_type: Optional[str] = None,
) -> Optional[dict]:
    """
    Atomically claim a card: status == eligible_status, owner IS NULL.
    Optional card_type filter ('story' | 'task').
    Returns card dict on success, None if no card available.
    """
    type_filter = "AND type = ?" if card_type else ""
    select_params: list = [eligible_status]
    if card_type:
        select_params.append(card_type)

    with get_conn() as conn:
        cur = conn.execute(
            f"""
            UPDATE cards
            SET owner = ?, status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM cards
                WHERE owner IS NULL AND status = ? {type_filter}
                ORDER BY created_at LIMIT 1
            )
            """,
            [worker_id, next_status] + select_params,
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM cards WHERE owner = ? AND status = ?",
            (worker_id, next_status),
        ).fetchone()
    return dict(row) if row else None

def count_active_workers() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE owner IS NOT NULL"
        ).fetchone()
    return row["n"] if row else 0


# ─── 驗收條件 ────────────────────────────────────────────────────────────

def insert_ac(card_id: str, text: str, kind: str = "functional", source: str = "po") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO acceptance_criteria (card_id, text, kind, source) VALUES (?, ?, ?, ?)",
            (card_id, text, kind, source),
        )


def get_ac_for_card(card_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM acceptance_criteria WHERE card_id = ?", (card_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def mark_ac_satisfied(ac_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE acceptance_criteria SET satisfied = 1 WHERE id = ?", (ac_id,)
        )


# ─── 事件 ────────────────────────────────────────────────────────────────

def insert_event(
    event_type: str,
    actor: str,
    card_id: Optional[str] = None,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
    metadata: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
) -> None:
    key = idempotency_key or str(uuid.uuid4())
    meta_str = json.dumps(metadata, ensure_ascii=False) if metadata else None
    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO events
                  (event_type, actor, card_id, old_status, new_status, idempotency_key, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_type, actor, card_id, old_status, new_status, key, meta_str),
            )
    except sqlite3.IntegrityError:
        pass  # 重複 idempotency_key，忽略


def get_events(card_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        if card_id:
            rows = conn.execute(
                "SELECT * FROM events WHERE card_id = ? ORDER BY created_at DESC LIMIT ?",
                (card_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# ─── CLI 工具（手動 seed / 查詢）────────────────────────────────────────

def _seed_test_card() -> None:
    """開發用：手動塞一張 todo 子卡，供 M0 驗收。"""
    init_db()
    # 先塞一張母卡
    story_id = "S-001"
    try:
        insert_card(
            card_id=story_id,
            card_type="story",
            title="[SEED] 測試母卡",
            body="M0 驗收用",
            status="developing",
            branch="feature/S-001",
        )
    except sqlite3.IntegrityError:
        print(f"[db] {story_id} 已存在，跳過")

    # 塞一張 todo 子卡
    task_id = "T-001"
    try:
        insert_card(
            card_id=task_id,
            card_type="task",
            title="[SEED] 加 GET /health 端點",
            body="回傳 {\"status\": \"ok\"}",
            parent_id=story_id,
            status="todo",
            branch="card/T-001",
        )
        insert_event("seed", "human", card_id=task_id, new_status="todo")
        print(f"[db] 已塞入測試卡 {task_id}（status=todo）")
    except sqlite3.IntegrityError:
        print(f"[db] {task_id} 已存在，跳過")


if __name__ == "__main__":
    import sys
    if "--seed" in sys.argv:
        _seed_test_card()
    elif "--list" in sys.argv:
        init_db()
        for c in list_cards():
            print(c)
    elif "--events" in sys.argv:
        init_db()
        for e in get_events():
            print(e)
    else:
        print("Usage: python db.py [--seed | --list | --events]")
