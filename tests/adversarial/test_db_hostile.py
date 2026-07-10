"""Adversarial tests for db.py and schema.sql.

This suite treats db.py's public API as hostile territory: user-controlled
wallet strings and checkpoint labels, concurrent callers sharing the single
`check_same_thread=False` connection, and Position data that violates the
models.py "contract" (NaN, None, wrong types) because nothing upstream is
guaranteed to enforce it. Every test is fully offline and uses pytest's
`tmp_path` fixture for its own throwaway SQLite file -- nothing under data/
is touched.

Findings are written up in .superpowers/adversarial/db-findings.md.
Real bugs are encoded here as `@pytest.mark.xfail(strict=True, ...)` tests:
the assertion inside states the CORRECT behavior, which currently fails
against the actual (buggy) behavior. If the bug is ever fixed, the test
starts passing and pytest reports XPASS (a hard failure under strict=True),
which is the point: the fix is caught automatically.

One real, reliably-reproducing concurrency bug (racing `cursor.lastrowid` on
the shared connection under plain concurrent `create_checkpoint` calls --
no error injection needed) is deliberately NOT encoded here because it
cannot be forced into a specific outcome deterministically (it depends on
CPython/SQLite C-level thread scheduling, not on anything a Python-level
Event/Barrier can pin down). Its repro and actual output are in the findings
report instead, per the assignment's rule for non-deterministic concurrency
bugs.

Run:
    .venv/Scripts/python.exe -m pytest tests/adversarial/test_db_hostile.py -v --basetemp=.pytest_tmp/db
"""

from __future__ import annotations

import math
import sqlite3
import stat
import threading

import pytest

import db
from models import Position

WALLET = "0x" + "a" * 40


def make_position(asset: str = "a1", **overrides) -> Position:
    fields = dict(
        asset=asset,
        condition_id="c1",
        market_title="Will X happen?",
        event_slug="will-x-happen",
        outcome="Yes",
        size=1.0,
        entry_price=0.5,
        current_price=0.6,
        stake=1.0,
        current_value=1.2,
        open_pnl=0.2,
        percent_pnl=20.0,
        realized_pnl=0.0,
        redeemable=False,
        end_date="2026-01-01",
    )
    fields.update(overrides)
    return Position(**fields)


# ---------------------------------------------------------------------------
# BUG: wallet identity is never normalized
# ---------------------------------------------------------------------------


# FIXED: db._normalize_wallet canonicalizes (strip + lowercase) on both write
# and read, so the same account in any textual form maps to one bucket.
@pytest.mark.parametrize(
    "canonical,variant",
    [
        # EIP-55 checksummed vs. the all-lowercase form MetaMask's
        # eth_accounts / most wallet-connect flows actually return.
        (
            "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9",
            "0xab5801a7d398351b8be11c439e05c5b3259aec9",
        ),
        # all-uppercase hex digits after the 0x prefix (also valid hex,
        # seen from users who copy addresses out of some block explorers).
        (
            "0xab5801a7d398351b8be11c439e05c5b3259aec9",
            "0xAB5801A7D398351B8BE11C439E05C5B3259AEC9",
        ),
        # trailing newline: the single most common copy-paste artifact
        # (copying an address out of a terminal or a text file).
        (
            "0x" + "b" * 40,
            "0x" + "b" * 40 + "\n",
        ),
        # leading space, e.g. pasted after a stray space in a chat message.
        (
            "0x" + "c" * 40,
            " 0x" + "c" * 40,
        ),
    ],
)
def test_wallet_identity_not_normalized_hides_checkpoints(tmp_path, canonical, variant):
    conn = db.init_db(tmp_path / "wallet.db")
    db.create_checkpoint(conn, canonical, "my checkpoint")

    # Same underlying wallet, different textual form -> should still find it.
    found = db.list_checkpoints(conn, variant)
    assert len(found) == 1, (
        f"checkpoint created under {canonical!r} is invisible when looked up "
        f"as {variant!r} -- same wallet, different text, different bucket"
    )


# ---------------------------------------------------------------------------
# BUG: NaN silently becomes NULL / destroys unrelated rows in the same batch
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="BUG: save_settings silently stores NaN bankroll as NULL with no "
    "exception and no warning -- the user's number is gone without a trace",
)
def test_nan_bankroll_silently_becomes_null(tmp_path):
    conn = db.init_db(tmp_path / "settings.db")
    db.save_settings(conn, WALLET, bankroll=float("nan"))

    loaded = db.load_settings(conn)
    # A NaN bankroll is nonsensical, but silently rewriting it to None with
    # zero signal is not "handling" it -- at minimum the round trip should
    # preserve *some* trace that a value was supplied, not silently erase it.
    assert loaded["starting_bankroll"] is not None or math.isnan(
        loaded["starting_bankroll"]
    ), "NaN bankroll vanished into NULL with no error and no way to detect it"


@pytest.mark.xfail(
    strict=True,
    reason="BUG: one NaN field in one position (e.g. a 0/0-derived percent_pnl "
    "from upstream API data) raises NOT NULL and wipes the ENTIRE checkpoint "
    "batch, including otherwise-valid sibling positions, with a misleading "
    "error message that says NOT NULL when the value was never null",
)
def test_nan_in_one_position_does_not_destroy_the_whole_batch(tmp_path):
    conn = db.init_db(tmp_path / "nan_batch.db")
    cp_id = db.create_checkpoint(conn, WALLET, "cp1")

    positions = [make_position(asset=f"good{i}") for i in range(5)] + [
        make_position(asset="bad", percent_pnl=float("nan"))
    ]

    try:
        db.save_checkpoint_positions(conn, cp_id, positions)
    except sqlite3.Error:
        pass

    # Correct behavior: the five well-formed positions should have survived
    # even if the NaN one could not be stored.
    rows = db.load_checkpoint_positions(conn, cp_id)
    good_assets = {r.asset for r in rows if r.asset.startswith("good")}
    assert len(good_assets) == 5, (
        f"expected 5 valid sibling positions to survive, found {len(good_assets)} "
        "-- one bad float destroyed the entire checkpoint snapshot"
    )


# ---------------------------------------------------------------------------
# BUG: the shared, unsynchronized connection lets one thread's rollback wipe
# another thread's already-"committed" write
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="BUG: check_same_thread=False hands out one connection with no "
    "locking; an unrelated failed write on another thread can roll back a "
    "write whose own commit() already returned successfully with no error",
)
def test_concurrent_rollback_does_not_wipe_unrelated_committed_write(tmp_path):
    conn = db.init_db(tmp_path / "race.db")

    # step1_done / step2_done give a fully deterministic interleaving (no
    # sleeps): thread A's INSERT is guaranteed to still be uncommitted when
    # thread B's failing batch (and its internal rollback()) runs, because
    # thread A blocks on step2_done until thread B signals it is finished.
    step1_done = threading.Event()
    step2_done = threading.Event()
    result: dict = {}

    def thread_a():
        # This is exactly what create_checkpoint (db.py:47-53) does, with a
        # deliberate pause inserted between the execute() and the commit()
        # to make the otherwise-timing-dependent window deterministic. Two
        # real callers of create_checkpoint on two real threads can land in
        # this exact window purely from OS scheduling -- check_same_thread
        # =False is what makes that legal in the first place.
        cur = conn.execute(
            "INSERT INTO checkpoints (wallet_address, label) VALUES (?, ?)",
            (WALLET, "A-checkpoint"),
        )
        result["checkpoint_id"] = cur.lastrowid
        step1_done.set()
        step2_done.wait()
        conn.commit()
        result["a_commit_raised"] = False

    def thread_b():
        step1_done.wait()
        cp_id_b = conn.execute(
            "INSERT INTO checkpoints (wallet_address, label) VALUES (?, ?)",
            (WALLET, "B-checkpoint"),
        ).lastrowid
        try:
            # Guaranteed UNIQUE(checkpoint_id, asset) violation -> db.py's
            # own except/rollback path (db.py:88-95) fires for real.
            db.save_checkpoint_positions(
                conn, cp_id_b, [make_position(asset="dup"), make_position(asset="dup")]
            )
        except sqlite3.Error:
            pass
        step2_done.set()

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    assert result["a_commit_raised"] is False  # thread A's commit() never raised

    rows = conn.execute(
        "SELECT * FROM checkpoints WHERE id = ?", (result["checkpoint_id"],)
    ).fetchall()
    assert len(rows) == 1, (
        "thread A's checkpoint disappeared even though its own commit() call "
        "returned without raising -- an unrelated thread's rollback() ate it"
    )


# ---------------------------------------------------------------------------
# Attacks that correctly failed: injection & hostile strings
# ---------------------------------------------------------------------------


def test_sql_injection_string_stored_literally_not_executed(tmp_path):
    conn = db.init_db(tmp_path / "inj.db")
    evil_label = "x'); DROP TABLE checkpoints; --"
    db.create_checkpoint(conn, WALLET, evil_label)

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "checkpoints" in tables
    loaded = db.list_checkpoints(conn, WALLET)
    assert loaded[0]["label"] == evil_label


def test_nul_byte_in_label_round_trips(tmp_path):
    conn = db.init_db(tmp_path / "nul.db")
    label = "before\x00after"
    cp_id = db.create_checkpoint(conn, WALLET, label)
    loaded = [c for c in db.list_checkpoints(conn, WALLET) if c["id"] == cp_id][0]
    assert loaded["label"] == label


def test_unicode_hostile_label_round_trips(tmp_path):
    conn = db.init_db(tmp_path / "unicode.db")
    # emoji, RTL override (U+202E/U+202C), combining acute accent
    label = "checkpoint \U0001F600 \u202Eevil\u202C café"
    cp_id = db.create_checkpoint(conn, WALLET, label)
    loaded = [c for c in db.list_checkpoints(conn, WALLET) if c["id"] == cp_id][0]
    assert loaded["label"] == label


def test_very_long_label_not_truncated(tmp_path):
    conn = db.init_db(tmp_path / "long.db")
    label = "A" * (1024 * 1024)
    cp_id = db.create_checkpoint(conn, WALLET, label)
    loaded = [c for c in db.list_checkpoints(conn, WALLET) if c["id"] == cp_id][0]
    assert len(loaded["label"]) == len(label)


def test_percent_and_underscore_wallet_have_no_wildcard_effect(tmp_path):
    conn = db.init_db(tmp_path / "wildcard.db")
    # No LIKE clause exists anywhere in db.py, so SQL wildcard metacharacters
    # in a wallet string must behave as plain literal text.
    db.create_checkpoint(conn, "0x%", "percent-wallet")
    db.create_checkpoint(conn, "0x_AAAA", "underscore-wallet")
    db.create_checkpoint(conn, "0xAAAA", "should-not-match-wildcards")

    assert len(db.list_checkpoints(conn, "0x%")) == 1
    assert len(db.list_checkpoints(conn, "0x_AAAA")) == 1


def test_whitespace_only_wallet_canonicalizes_to_empty(tmp_path):
    # Neither "" nor "   " is a valid wallet (both are rejected upstream by
    # validate_wallet). If they do reach db, normalization maps them to the
    # same empty bucket rather than fragmenting into distinct phantom
    # identities -- the safe direction (never a cross-identity collision of two
    # *valid* wallets, which test_different_wallets_never_collide guards).
    conn = db.init_db(tmp_path / "empty.db")
    db.create_checkpoint(conn, "", "empty-wallet-cp")
    db.create_checkpoint(conn, "   ", "whitespace-wallet-cp")
    assert db.list_checkpoints(conn, "") == db.list_checkpoints(conn, "   ")
    assert len(db.list_checkpoints(conn, "")) == 2


def test_different_wallets_never_collide(tmp_path):
    """No case-folding/hex-parsing happens anywhere, so two genuinely
    different addresses can never be merged into one identity bucket --
    only under-matching (same account, different bucket) is possible."""
    conn = db.init_db(tmp_path / "collide.db")
    wallet_1 = "0x" + "1" * 40
    wallet_2 = "0x" + "2" * 40
    db.create_checkpoint(conn, wallet_1, "wallet-1-cp")
    db.create_checkpoint(conn, wallet_2, "wallet-2-cp")

    assert len(db.list_checkpoints(conn, wallet_1)) == 1
    assert len(db.list_checkpoints(conn, wallet_2)) == 1
    assert db.list_checkpoints(conn, wallet_1)[0]["label"] == "wallet-1-cp"
    assert db.list_checkpoints(conn, wallet_2)[0]["label"] == "wallet-2-cp"


# ---------------------------------------------------------------------------
# Attacks that correctly failed: constraint enforcement
# ---------------------------------------------------------------------------


def test_foreign_key_violation_rejected_for_orphan_positions(tmp_path):
    conn = db.init_db(tmp_path / "fk.db")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.save_checkpoint_positions(conn, 9999, [make_position(asset="orphan")])


def test_unique_violation_rolls_back_entire_batch_not_partial(tmp_path):
    """Verifies the claim in db.py's comment (lines 88-95): a UNIQUE
    violation partway through executemany must not leave any partial rows
    from that batch sitting uncommitted for a later, unrelated commit() to
    silently persist."""
    conn = db.init_db(tmp_path / "unique.db")
    cp_id = db.create_checkpoint(conn, WALLET, "cp1")

    positions = [make_position(asset="dup"), make_position(asset="other"), make_position(asset="dup")]
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.save_checkpoint_positions(conn, cp_id, positions)

    rows = conn.execute(
        "SELECT asset FROM checkpoint_positions WHERE checkpoint_id = ?", (cp_id,)
    ).fetchall()
    assert rows == []  # nothing from the failed batch survived, not even "other"

    # And the checkpoint itself (a prior, already-committed write) is untouched.
    assert conn.execute(
        "SELECT 1 FROM checkpoints WHERE id = ?", (cp_id,)
    ).fetchone() is not None


def test_cascade_delete_removes_child_positions(tmp_path):
    conn = db.init_db(tmp_path / "cascade.db")
    cp_id = db.create_checkpoint(conn, WALLET, "will-be-deleted")
    db.save_checkpoint_positions(conn, cp_id, [make_position(asset="p1"), make_position(asset="p2")])
    assert len(db.load_checkpoint_positions(conn, cp_id)) == 2

    conn.execute("DELETE FROM checkpoints WHERE id = ?", (cp_id,))
    conn.commit()

    assert db.load_checkpoint_positions(conn, cp_id) == []


def test_none_in_notnull_column_rejected_cleanly(tmp_path):
    conn = db.init_db(tmp_path / "none.db")
    cp_id = db.create_checkpoint(conn, WALLET, "cp1")
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        db.save_checkpoint_positions(conn, cp_id, [make_position(asset="x", size=None)])


# ---------------------------------------------------------------------------
# Attacks that correctly failed: init_db idempotency / hostile paths
# ---------------------------------------------------------------------------


def test_init_db_idempotent_reopen_preserves_data(tmp_path):
    path = tmp_path / "idempotent.db"
    conn1 = db.init_db(path)
    db.create_checkpoint(conn1, WALLET, "before-reinit")

    conn2 = db.init_db(path)  # re-running the schema script must be a no-op
    rows = conn2.execute("SELECT label FROM checkpoints").fetchall()
    assert [r[0] for r in rows] == ["before-reinit"]


def test_init_db_on_path_blocked_by_existing_file_raises_cleanly(tmp_path):
    blocker = tmp_path / "iamafile"
    blocker.write_text("i am a file, not a directory")
    target = blocker / "sub" / "db.sqlite"

    with pytest.raises(OSError):
        db.init_db(target)


def test_init_db_on_non_sqlite_file_raises_cleanly_without_corrupting_it(tmp_path):
    path = tmp_path / "not_a_db.db"
    original_content = b"this is just a text file, not a sqlite database"
    path.write_bytes(original_content)

    with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
        db.init_db(path)

    # The failed attempt must not have clobbered the file's contents.
    assert path.read_bytes() == original_content


def test_init_db_on_readonly_file_raises_cleanly(tmp_path):
    path = tmp_path / "readonly.db"
    conn = db.init_db(path)
    conn.close()

    path.chmod(stat.S_IREAD)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn2 = db.init_db(path)
            db.create_checkpoint(conn2, WALLET, "should fail")
    finally:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_write_lock_contention_raises_operational_error_not_corruption(tmp_path, monkeypatch):
    """db.py never sets an explicit busy_timeout, so init_db's connection
    uses the sqlite3 stdlib default (5s). That's slow to test directly, so
    a second, independent connection to the same file is given a short
    timeout instead -- the failure mode under contention (a clean, catchable
    OperationalError, not silent corruption) is identical either way."""
    path = tmp_path / "locked.db"
    conn1 = db.init_db(path)

    orig_connect = sqlite3.connect

    def fast_timeout_connect(database, *a, **kw):
        kw["timeout"] = 0.1
        return orig_connect(database, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", fast_timeout_connect)
    conn2 = sqlite3.connect(path, check_same_thread=False)

    conn1.execute("BEGIN IMMEDIATE")
    conn1.execute(
        "INSERT INTO checkpoints (wallet_address, label) VALUES (?, ?)", (WALLET, "holder")
    )

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.create_checkpoint(conn2, WALLET, "blocked-writer")

    conn1.commit()
    # No corruption: the holder's own write is intact and it's the only row.
    rows = conn1.execute("SELECT label FROM checkpoints").fetchall()
    assert [r[0] for r in rows] == ["holder"]


# ---------------------------------------------------------------------------
# Attacks that correctly failed / documented behavior: type coercion
# ---------------------------------------------------------------------------


def test_float_precision_round_trips_exactly(tmp_path):
    conn = db.init_db(tmp_path / "precision.db")
    cp_id = db.create_checkpoint(conn, WALLET, "cp1")
    tricky = 0.1 + 0.2  # 0.30000000000000004
    db.save_checkpoint_positions(
        conn,
        cp_id,
        [make_position(asset="p1", size=tricky, entry_price=1e308, current_price=5e-324)],
    )
    loaded = db.load_checkpoint_positions(conn, cp_id)[0]
    assert loaded.size == tricky
    assert loaded.entry_price == 1e308
    assert loaded.current_price == 5e-324


def test_inf_round_trips_through_real_column(tmp_path):
    conn = db.init_db(tmp_path / "inf.db")
    cp_id = db.create_checkpoint(conn, WALLET, "cp1")
    db.save_checkpoint_positions(
        conn,
        cp_id,
        [make_position(asset="p1", entry_price=float("inf"), current_price=float("-inf"))],
    )
    loaded = db.load_checkpoint_positions(conn, cp_id)[0]
    assert loaded.entry_price == float("inf")
    assert loaded.current_price == float("-inf")


def test_non_numeric_string_bypassing_the_position_contract_is_stored_literally(tmp_path):
    """models.Position.from_api always runs values through _f()/_s(), so this
    can only happen if some future caller builds a Position by hand with the
    wrong type. Documented here because db.py performs no validation of its
    own -- SQLite's REAL "type affinity" is a hint, not an enforced type, so
    whatever the caller hands over is what gets stored."""
    conn = db.init_db(tmp_path / "strtype.db")
    cp_id = db.create_checkpoint(conn, WALLET, "cp1")
    db.save_checkpoint_positions(conn, cp_id, [make_position(asset="p1", size="not-a-number")])
    loaded = db.load_checkpoint_positions(conn, cp_id)[0]
    assert loaded.size == "not-a-number"
    assert isinstance(loaded.size, str)
