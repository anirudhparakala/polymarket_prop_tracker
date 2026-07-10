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


def test_nan_bankroll_is_rejected_loudly_not_silently_nulled(tmp_path):
    """FIXED: SQLite binds NaN as NULL (https://sqlite.org/quirks.html), so the
    user's number used to vanish into a nullable column with no exception and no
    way to detect it. save_settings now refuses the value and names the field."""
    conn = db.init_db(tmp_path / "settings.db")
    with pytest.raises(ValueError, match="starting_bankroll"):
        db.save_settings(conn, WALLET, bankroll=float("nan"))

    # And nothing was written: the settings row was never created.
    assert db.load_settings(conn) is None


def test_nan_in_one_position_rejects_the_batch_with_a_clear_error(tmp_path):
    """FIXED (deliberately atomic, not partial).

    The original finding wanted the five valid siblings to survive. They must
    not. A checkpoint is the baseline every later comparison is measured
    against, so a snapshot silently missing one position -- or storing a zeroed
    field -- would quietly corrupt every future refresh. The right behavior is to
    reject the whole snapshot and say exactly which field of which asset is bad,
    instead of SQLite's misleading "NOT NULL constraint failed" for a field that
    plainly had a value.
    """
    conn = db.init_db(tmp_path / "nan_batch.db")
    cp_id = db.create_checkpoint(conn, WALLET, "cp1")

    positions = [make_position(asset=f"good{i}") for i in range(5)] + [
        make_position(asset="bad", percent_pnl=float("nan"))
    ]

    with pytest.raises(ValueError, match="percent_pnl.*'bad'"):
        db.save_checkpoint_positions(conn, cp_id, positions)

    # Atomic: nothing at all persisted, no half-written snapshot.
    assert db.load_checkpoint_positions(conn, cp_id) == []


# ---------------------------------------------------------------------------
# BUG: the shared, unsynchronized connection lets one thread's rollback wipe
# another thread's already-"committed" write
# ---------------------------------------------------------------------------


def test_concurrent_public_api_writes_do_not_wipe_each_other(tmp_path):
    """FIXED: db._WRITE_LOCK serializes every write, so create_checkpoint's
    INSERT + lastrowid + commit is atomic with respect to another thread's
    failing batch and its rollback().

    The original repro paused *between* execute() and commit() using raw SQL on
    the shared connection. That window cannot exist once create_checkpoint holds
    the lock across both -- but it also means a caller that bypasses db.py and
    issues raw SQL on the shared connection is still unprotected. That is the
    reason every write must go through this module.
    """
    conn = db.init_db(tmp_path / "race.db")
    start = threading.Barrier(2)
    created: list[int] = []
    errors: list[Exception] = []

    def writer():
        start.wait()
        for i in range(50):
            try:
                created.append(db.create_checkpoint(conn, WALLET, f"cp-{i}"))
            except Exception as exc:  # noqa: BLE001 - test records anything
                errors.append(exc)

    def failing_saver():
        start.wait()
        for _ in range(50):
            victim = db.create_checkpoint(conn, WALLET, "victim")
            try:
                # Guaranteed UNIQUE(checkpoint_id, asset) violation -> db.py's
                # own except/rollback path fires for real, on the shared conn.
                db.save_checkpoint_positions(
                    conn, victim, [make_position(asset="dup"), make_position(asset="dup")]
                )
            except sqlite3.Error:
                pass  # expected

    threads = [threading.Thread(target=writer), threading.Thread(target=failing_saver)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(created) == 50
    persisted = {r[0] for r in conn.execute("SELECT id FROM checkpoints")}
    missing = set(created) - persisted
    assert not missing, (
        f"{len(missing)} checkpoint(s) whose create_checkpoint() returned an id "
        "were eaten by an unrelated thread's rollback()"
    )


def test_concurrent_create_checkpoint_hands_out_unique_ids(tmp_path):
    """FIXED: lastrowid is per-connection state. Without serialization a
    concurrent INSERT overwrote it between execute() and the read, so callers
    got duplicate ids, None (crashing on int(None)), or a row that existed in
    the table but whose id was never returned -- an orphaned, unreachable
    checkpoint that positions could never be attached to.
    """
    conn = db.init_db(tmp_path / "lastrowid.db")
    n_threads, per_thread = 8, 25
    start = threading.Barrier(n_threads)
    guard = threading.Lock()
    ids: list[int] = []
    errors: list[Exception] = []

    def worker():
        start.wait()
        for _ in range(per_thread):
            try:
                new_id = db.create_checkpoint(conn, WALLET, "cp")
                with guard:
                    ids.append(new_id)
            except Exception as exc:  # noqa: BLE001 - test records anything
                with guard:
                    errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = n_threads * per_thread
    assert errors == [], f"concurrent create_checkpoint raised: {errors[:3]}"
    assert len(ids) == expected
    assert len(set(ids)) == expected, "two callers were handed the same row id"
    rows = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    assert rows == expected, "orphaned rows: inserted but id never returned"


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
    # FIXED: db validates the batch up front, so a None numeric fails with a
    # TypeError naming the field instead of a raw sqlite NOT NULL message.
    conn = db.init_db(tmp_path / "none.db")
    cp_id = db.create_checkpoint(conn, WALLET, "cp1")
    with pytest.raises(TypeError, match="size.*not a number"):
        db.save_checkpoint_positions(conn, cp_id, [make_position(asset="x", size=None)])
    assert db.load_checkpoint_positions(conn, cp_id) == []


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


def test_inf_is_rejected_before_it_can_poison_a_checkpoint(tmp_path):
    # SQLite happily round-trips +/-inf through a REAL column, so nothing below
    # this layer would have caught it. An infinite price or value is nonsense,
    # and a checkpoint is the baseline every later comparison is measured
    # against -- so it is rejected at the write, not stored.
    conn = db.init_db(tmp_path / "inf.db")
    cp_id = db.create_checkpoint(conn, WALLET, "cp1")
    with pytest.raises(ValueError, match="entry_price"):
        db.save_checkpoint_positions(
            conn,
            cp_id,
            [
                make_position(
                    asset="p1",
                    entry_price=float("inf"),
                    current_price=float("-inf"),
                )
            ],
        )
    assert db.load_checkpoint_positions(conn, cp_id) == []


def test_non_numeric_string_bypassing_the_position_contract_is_rejected(tmp_path):
    """FIXED. models.Position.from_api runs values through _f()/_s(), so this
    can only happen if a future caller builds a Position by hand with the wrong
    type. SQLite's REAL "type affinity" is a hint, not an enforced type, so
    without validation the string would be stored verbatim and silently corrupt
    the checkpoint baseline. db now type-checks the batch before writing."""
    conn = db.init_db(tmp_path / "strtype.db")
    cp_id = db.create_checkpoint(conn, WALLET, "cp1")
    with pytest.raises(TypeError, match="size.*not a number"):
        db.save_checkpoint_positions(
            conn, cp_id, [make_position(asset="p1", size="not-a-number")]
        )
    assert db.load_checkpoint_positions(conn, cp_id) == []
