-- AI 開發公司 MVP — SQLite Schema
-- 啟動時必須執行 PRAGMA journal_mode=WAL
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS cards (
  id            TEXT PRIMARY KEY,         -- 'S-101'（母卡）/ 'T-201'（子卡）
  type          TEXT NOT NULL,            -- 'story' | 'task'
  parent_id     TEXT,                     -- 子卡指向母卡
  title         TEXT NOT NULL,
  body          TEXT,                     -- 需求內容 / 卡片描述
  status        TEXT NOT NULL DEFAULT 'todo',  -- 見狀態機
  owner         TEXT,                     -- 認領中的 worker id；NULL=無人
  branch        TEXT,                     -- 'card/T-201' / 'feature/S-101'
  pr_number     INTEGER,                  -- 開了 PR 後填
  loop_count    INTEGER DEFAULT 0,        -- 被審查退回次數
  retry_count   INTEGER DEFAULT 0,        -- 因崩潰/逾時重試次數
  verified      INTEGER DEFAULT 0,        -- 只由母卡 E2E 寫
  created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS acceptance_criteria (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id     TEXT NOT NULL REFERENCES cards(id),
  text        TEXT NOT NULL,             -- 一條驗收條件（人話）
  kind        TEXT DEFAULT 'functional', -- 'functional' | 'security'
  source      TEXT DEFAULT 'po',         -- 'po' | 'standing_security' | 'discovered'
  satisfied   INTEGER DEFAULT 0          -- E2E 驗過才置 1
);

CREATE TABLE IF NOT EXISTS events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  event_type      TEXT NOT NULL,         -- claim/test_red/test_green/review_pass/...
  actor           TEXT,                  -- agent id 或 'human' 或 'orchestrator'
  card_id         TEXT,
  old_status      TEXT,
  new_status      TEXT,
  idempotency_key TEXT UNIQUE,           -- 去重：同一動作只記一次
  metadata        TEXT                   -- JSON 字串（tokens、cost、exit_code…）
);

-- ─── 原子認領 SQL（供 db.py 使用）────────────────────────────────────────
-- 在一個交易裡執行，回傳影響列數；== 1 才算認領成功
--
--   UPDATE cards
--   SET owner = :worker_id, status = :next_status, updated_at = CURRENT_TIMESTAMP
--   WHERE id = (
--     SELECT id FROM cards
--     WHERE owner IS NULL AND status = :eligible_status
--     ORDER BY created_at LIMIT 1
--   );
