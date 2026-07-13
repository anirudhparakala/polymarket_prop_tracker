"""SQLite persistence. Every SQL statement in this project lives in this file."""

from __future__ import annotations

import math
import sqlite3
import threading
from pathlib import Path

from models import CheckpointRow, Position

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# init_db hands one connection to every caller and sets check_same_thread=False,
# so Streamlit's worker threads can share it. SQLite tracks exactly one open
# transaction per connection handle: without serialization, an INSERT from one
# logical operation and a rollback() from an unrelated one land in the *same*
# implicit transaction, and the rollback silently discards a write whose own
# commit() already returned successfully. Reads are safe; every write goes
# through this lock.
_WRITE_LOCK = threading.RLock()


def _require_finite(value: float, field: str, context: str = "") -> float:
    """Reject NaN/Inf before it reaches SQLite.

    SQLite has no NaN: it binds one as NULL (https://sqlite.org/quirks.html).
    Against a `REAL NOT NULL` column that surfaces as a baffling "NOT NULL
    constraint failed" for a field that plainly had a value. Worse, on a
    nullable column (starting_bankroll) it succeeds and the number is simply
    gone. Fail loudly and name the field instead.

    Checkpoints are the baseline every later comparison is measured against, so
    a silently-wrong stored value would poison every future refresh. That is why
    writes reject bad data rather than defaulting it the way the display path
    does.
    """
    where = f" for {context}" if context else ""
    # SQLite's REAL "type affinity" is a hint, not an enforced type: a str or
    # None handed to a REAL NOT NULL column is stored verbatim or rejected with
    # a confusing message. Check the type before the value.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{field}{where} is {value!r} ({type(value).__name__}), not a number. "
            "SQLite would store it verbatim and corrupt the checkpoint."
        )
    if not math.isfinite(value):
        raise ValueError(
            f"{field}{where} is {value!r}, which SQLite cannot store meaningfully "
            "(NaN binds as NULL; an infinite price or value is nonsense). "
            "Refusing to persist it."
        )
    return float(value)


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
    if bankroll is not None:
        _require_finite(bankroll, "starting_bankroll")
    with _WRITE_LOCK:
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
    # INSERT, lastrowid read, and commit must be one atomic unit: lastrowid is
    # per-connection state, so a concurrent insert can overwrite it between the
    # execute() and the read, handing this caller someone else's row id (or None).
    with _WRITE_LOCK:
        cursor = conn.execute(
            "INSERT INTO checkpoints (wallet_address, label) VALUES (?, ?)",
            (_normalize_wallet(wallet), label),
        )
        checkpoint_id = cursor.lastrowid
        conn.commit()
    if checkpoint_id is None:  # pragma: no cover - defensive
        raise RuntimeError("SQLite did not report a row id for the new checkpoint")
    return int(checkpoint_id)


_NUMERIC_FIELDS = (
    "size",
    "entry_price",
    "stake",
    "current_value",
    "current_price",
    "open_pnl",
    "percent_pnl",
    "realized_pnl",
)


def _validate_positions(positions: list[Position]) -> None:
    """Reject a bad batch BEFORE any write. A NaN would bind as NULL and fail the
    REAL NOT NULL column with a misleading "NOT NULL constraint failed" naming a
    field that plainly had a value."""
    for position in positions:
        for field in _NUMERIC_FIELDS:
            _require_finite(
                getattr(position, field), field, context=f"asset {position.asset!r}"
            )


def save_checkpoint(
    conn: sqlite3.Connection, wallet: str, label: str, positions: list[Position]
) -> int:
    """Create a checkpoint AND store its positions atomically. Returns its id.

    Calling create_checkpoint() then save_checkpoint_positions() is NOT atomic:
    create_checkpoint commits the checkpoint row immediately, so if storing the
    positions then fails, an empty phantom checkpoint is left behind. It appears
    in the compare dropdown looking legitimate, and comparing against it reports
    every live position as `New` -- garbage the user was told had failed to save.
    Here either the whole snapshot lands or none of it does.
    """
    _validate_positions(positions)  # raises before touching the database

    with _WRITE_LOCK:
        try:
            cursor = conn.execute(
                "INSERT INTO checkpoints (wallet_address, label) VALUES (?, ?)",
                (_normalize_wallet(wallet), label),
            )
            checkpoint_id = cursor.lastrowid
            if checkpoint_id is None:  # pragma: no cover - defensive
                raise RuntimeError("SQLite did not report a row id for the checkpoint")
            _insert_checkpoint_positions(conn, int(checkpoint_id), positions)
            conn.commit()
        except sqlite3.Error:
            # Undo the checkpoint row too, not just the positions.
            conn.rollback()
            raise

    return int(checkpoint_id)


def save_checkpoint_positions(
    conn: sqlite3.Connection, checkpoint_id: int, positions: list[Position]
) -> None:
    _validate_positions(positions)
    with _WRITE_LOCK:
        try:
            _insert_checkpoint_positions(conn, checkpoint_id, positions)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise


def _insert_checkpoint_positions(
    conn: sqlite3.Connection, checkpoint_id: int, positions: list[Position]
) -> None:
    """Raw insert only. The CALLER owns the transaction (commit/rollback):
    executemany can partially apply rows before raising (a duplicate
    (checkpoint_id, asset) fails the UNIQUE index only on the second row), so a
    failure must roll back whatever the caller had open -- which, for
    save_checkpoint(), includes the checkpoint row itself."""
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
