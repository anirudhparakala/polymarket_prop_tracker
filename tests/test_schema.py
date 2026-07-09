import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).parent.parent / "schema.sql"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_three_tables_exist():
    conn = _conn()
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"settings", "checkpoints", "checkpoint_positions"} <= names


def test_checkpoints_are_scoped_to_a_wallet():
    conn = _conn()
    assert "wallet_address" in _columns(conn, "checkpoints")


def test_checkpoint_positions_has_every_documented_column():
    conn = _conn()
    assert _columns(conn, "checkpoint_positions") >= {
        "id", "checkpoint_id", "asset", "condition_id", "title", "event_slug",
        "outcome", "size", "avg_price", "stake", "current_value", "cur_price",
        "cash_pnl", "percent_pnl", "realized_pnl", "created_at",
    }


def test_one_row_per_asset_per_checkpoint_is_enforced():
    conn = _conn()
    conn.execute("INSERT INTO checkpoints (wallet_address, label) VALUES ('0x0', 'a')")
    args = (1, "AST", "0xc", "t", "e", "Yes", 1.0, 0.5, 0.5, 0.5, 0.5, 0.0, 0.0, 0.0)
    sql = (
        "INSERT INTO checkpoint_positions (checkpoint_id, asset, condition_id, title,"
        " event_slug, outcome, size, avg_price, stake, current_value, cur_price,"
        " cash_pnl, percent_pnl, realized_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    conn.execute(sql, args)
    try:
        conn.execute(sql, args)
    except sqlite3.IntegrityError:
        return
    raise AssertionError("duplicate (checkpoint_id, asset) must be rejected")


def test_deleting_a_checkpoint_cascades_to_its_positions():
    conn = _conn()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO checkpoints (wallet_address, label) VALUES ('0x0', 'a')")
    conn.execute(
        "INSERT INTO checkpoint_positions (checkpoint_id, asset, condition_id, title,"
        " event_slug, outcome, size, avg_price, stake, current_value, cur_price,"
        " cash_pnl, percent_pnl, realized_pnl)"
        " VALUES (1,'AST','0xc','t','e','Yes',1.0,0.5,0.5,0.5,0.5,0.0,0.0,0.0)"
    )
    conn.execute("DELETE FROM checkpoints WHERE id = 1")
    remaining = conn.execute("SELECT COUNT(*) FROM checkpoint_positions").fetchone()[0]
    assert remaining == 0
