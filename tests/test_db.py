import pytest

import db
from models import CheckpointRow, Position

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


def _position(asset: str, size: float = 10.0, value: float = 5.0) -> Position:
    return Position.from_api(
        {
            "asset": asset,
            "conditionId": "0xc",
            "title": "Morocco wins",
            "eventSlug": "morocco-france",
            "outcome": "Yes",
            "size": size,
            "avgPrice": 0.5,
            "initialValue": 5.0,
            "currentValue": value,
            "percentPnl": 0.0,
            "curPrice": 0.5,
            "realizedPnl": 0.0,
        }
    )


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def test_init_db_creates_the_file_and_tables(tmp_path):
    path = tmp_path / "nested" / "test.db"
    connection = db.init_db(path)
    assert path.exists()
    names = {
        r[0]
        for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"settings", "checkpoints", "checkpoint_positions"} <= names


def test_init_db_enables_foreign_keys(conn):
    # SQLite defaults foreign_keys OFF per connection; ON CASCADE is inert without it.
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_init_db_is_idempotent(tmp_path):
    db.init_db(tmp_path / "t.db").close()
    db.init_db(tmp_path / "t.db").close()


def test_load_settings_returns_none_when_unset(conn):
    assert db.load_settings(conn) is None


def test_save_then_load_settings_round_trips(conn):
    db.save_settings(conn, WALLET_A, 100.0)
    loaded = db.load_settings(conn)
    assert loaded["wallet_address"] == WALLET_A
    assert loaded["starting_bankroll"] == 100.0


def test_save_settings_overwrites_rather_than_appending(conn):
    db.save_settings(conn, WALLET_A)
    db.save_settings(conn, WALLET_B)
    assert db.load_settings(conn)["wallet_address"] == WALLET_B
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1


def test_create_checkpoint_returns_its_id(conn):
    first = db.create_checkpoint(conn, WALLET_A, "Before match")
    second = db.create_checkpoint(conn, WALLET_A, "Halftime")
    assert second > first


def test_list_checkpoints_only_returns_the_given_wallets(conn):
    db.create_checkpoint(conn, WALLET_A, "A one")
    db.create_checkpoint(conn, WALLET_B, "B one")
    labels = [c["label"] for c in db.list_checkpoints(conn, WALLET_A)]
    assert labels == ["A one"]


def test_list_checkpoints_is_newest_first(conn):
    db.create_checkpoint(conn, WALLET_A, "first")
    db.create_checkpoint(conn, WALLET_A, "second")
    labels = [c["label"] for c in db.list_checkpoints(conn, WALLET_A)]
    assert labels == ["second", "first"]


def test_save_and_load_checkpoint_positions_round_trip(conn):
    checkpoint_id = db.create_checkpoint(conn, WALLET_A, "Before match")
    db.save_checkpoint_positions(conn, checkpoint_id, [_position("AST1"), _position("AST2")])
    rows = db.load_checkpoint_positions(conn, checkpoint_id)
    assert len(rows) == 2
    assert all(isinstance(r, CheckpointRow) for r in rows)
    assert {r.asset for r in rows} == {"AST1", "AST2"}
    assert rows[0].market_title == "Morocco wins"


def test_saving_the_same_asset_twice_in_one_checkpoint_is_rejected(conn):
    checkpoint_id = db.create_checkpoint(conn, WALLET_A, "Before match")
    with pytest.raises(Exception):
        db.save_checkpoint_positions(
            conn, checkpoint_id, [_position("AST1"), _position("AST1")]
        )


def test_load_checkpoint_positions_of_unknown_checkpoint_is_empty(conn):
    assert db.load_checkpoint_positions(conn, 999) == []


def test_saving_an_empty_position_list_is_allowed(conn):
    checkpoint_id = db.create_checkpoint(conn, WALLET_A, "Empty")
    db.save_checkpoint_positions(conn, checkpoint_id, [])
    assert db.load_checkpoint_positions(conn, checkpoint_id) == []


def test_deleting_a_checkpoint_cascades_to_its_positions(conn):
    checkpoint_id = db.create_checkpoint(conn, WALLET_A, "Before match")
    db.save_checkpoint_positions(conn, checkpoint_id, [_position("AST1"), _position("AST2")])
    assert len(db.load_checkpoint_positions(conn, checkpoint_id)) == 2

    conn.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
    conn.commit()

    assert db.load_checkpoint_positions(conn, checkpoint_id) == []
    remaining = conn.execute(
        "SELECT COUNT(*) FROM checkpoint_positions WHERE checkpoint_id = ?",
        (checkpoint_id,),
    ).fetchone()[0]
    assert remaining == 0


def test_duplicate_asset_batch_leaves_no_partial_row_after_rollback(conn):
    """Reproduces the Finding 1 bug: executemany fails partway through a
    batch with a duplicate (checkpoint_id, asset) pair. Without a rollback,
    the first row of the batch would remain in the open transaction and get
    silently committed by any later, unrelated commit() on this connection.
    """
    checkpoint_id = db.create_checkpoint(conn, WALLET_A, "Before match")

    with pytest.raises(Exception):
        db.save_checkpoint_positions(
            conn, checkpoint_id, [_position("DUPE"), _position("DUPE")]
        )

    # The phantom first row must be gone even before anything else commits.
    assert db.load_checkpoint_positions(conn, checkpoint_id) == []

    # Prove it stays gone even after an unrelated commit on this same
    # connection -- this is exactly the scenario the reviewer reproduced.
    db.create_checkpoint(conn, WALLET_A, "Halftime")
    assert db.load_checkpoint_positions(conn, checkpoint_id) == []
    count = conn.execute(
        "SELECT COUNT(*) FROM checkpoint_positions WHERE checkpoint_id = ?",
        (checkpoint_id,),
    ).fetchone()[0]
    assert count == 0
