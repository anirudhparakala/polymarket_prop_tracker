"""SQLite persistence. Every SQL statement in this project lives in this file."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from models import CheckpointRow, Position

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _normalize_wallet(wallet: str) -> str:
    """Canonical form used as the checkpoint scoping key.

    Ethereum addresses are case-insensitive and copy-paste adds whitespace, so
    the same account pasted in a different case (or with a trailing newline)
    must map to one identity — otherwise a user's own checkpoints become
    invisible. Kept here (not only in the client) because this is the module
    that does the identity comparison in SQL.
    """
    return wallet.strip().lower()


def init_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # SQLite defaults this OFF per connection; ON DELETE CASCADE is inert without it.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def save_settings(
    conn: sqlite3.Connection, wallet: str, bankroll: float | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO settings (id, wallet_address, starting_bankroll)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            wallet_address    = excluded.wallet_address,
            starting_bankroll = excluded.starting_bankroll,
            updated_at        = datetime('now')
        """,
        (_normalize_wallet(wallet), bankroll),
    )
    conn.commit()


def load_settings(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return dict(row) if row else None


def create_checkpoint(conn: sqlite3.Connection, wallet: str, label: str) -> int:
    cursor = conn.execute(
        "INSERT INTO checkpoints (wallet_address, label) VALUES (?, ?)",
        (_normalize_wallet(wallet), label),
    )
    conn.commit()
    return int(cursor.lastrowid)


def save_checkpoint_positions(
    conn: sqlite3.Connection, checkpoint_id: int, positions: list[Position]
) -> None:
    try:
        conn.executemany(
            """
            INSERT INTO checkpoint_positions (
                checkpoint_id, asset, condition_id, title, event_slug, outcome,
                size, avg_price, stake, current_value, cur_price,
                cash_pnl, percent_pnl, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    checkpoint_id,
                    p.asset,
                    p.condition_id,
                    p.market_title,
                    p.event_slug,
                    p.outcome,
                    p.size,
                    p.entry_price,
                    p.stake,
                    p.current_value,
                    p.current_price,
                    p.open_pnl,
                    p.percent_pnl,
                    p.realized_pnl,
                )
                for p in positions
            ],
        )
    except sqlite3.Error:
        # executemany can partially apply inserts before raising (e.g. a
        # duplicate (checkpoint_id, asset) pair fails the UNIQUE constraint
        # only on the second row). Roll back so no partial batch is left
        # sitting in the open transaction for a later, unrelated commit()
        # elsewhere on this connection to silently persist.
        conn.rollback()
        raise
    conn.commit()


def list_checkpoints(conn: sqlite3.Connection, wallet: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, label, created_at
        FROM checkpoints
        WHERE wallet_address = ?
        ORDER BY id DESC
        """,
        (_normalize_wallet(wallet),),
    ).fetchall()
    return [dict(r) for r in rows]


def load_checkpoint_positions(
    conn: sqlite3.Connection, checkpoint_id: int
) -> list[CheckpointRow]:
    rows = conn.execute(
        "SELECT * FROM checkpoint_positions WHERE checkpoint_id = ? ORDER BY asset",
        (checkpoint_id,),
    ).fetchall()
    return [
        CheckpointRow(
            asset=r["asset"],
            condition_id=r["condition_id"],
            market_title=r["title"],
            event_slug=r["event_slug"],
            outcome=r["outcome"],
            size=r["size"],
            entry_price=r["avg_price"],
            current_price=r["cur_price"],
            stake=r["stake"],
            current_value=r["current_value"],
            open_pnl=r["cash_pnl"],
            percent_pnl=r["percent_pnl"],
            realized_pnl=r["realized_pnl"],
        )
        for r in rows
    ]
