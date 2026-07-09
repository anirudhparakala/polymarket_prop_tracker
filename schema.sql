-- Frozen contract. db.py is the only module allowed to execute SQL against it.

CREATE TABLE IF NOT EXISTS settings (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    wallet_address    TEXT    NOT NULL,
    starting_bankroll REAL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- wallet_address is what makes cross-wallet comparison impossible by
-- construction. Without it, pasting a second wallet renders every old row
-- Closed and every new row New, with all numbers technically correct.
CREATE TABLE IF NOT EXISTS checkpoints (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT    NOT NULL,
    label          TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_wallet
    ON checkpoints (wallet_address);

CREATE TABLE IF NOT EXISTS checkpoint_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id INTEGER NOT NULL REFERENCES checkpoints (id) ON DELETE CASCADE,
    asset         TEXT    NOT NULL,
    condition_id  TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    event_slug    TEXT    NOT NULL,
    outcome       TEXT    NOT NULL,
    size          REAL    NOT NULL,
    avg_price     REAL    NOT NULL,
    stake         REAL    NOT NULL,
    current_value REAL    NOT NULL,
    cur_price     REAL    NOT NULL,
    cash_pnl      REAL    NOT NULL,
    percent_pnl   REAL    NOT NULL,
    realized_pnl  REAL    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cp_positions_checkpoint
    ON checkpoint_positions (checkpoint_id);

-- asset is the join key; a checkpoint holds exactly one row per prop.
CREATE UNIQUE INDEX IF NOT EXISTS idx_cp_positions_asset
    ON checkpoint_positions (checkpoint_id, asset);
