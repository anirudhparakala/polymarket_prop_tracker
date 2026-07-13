"""Adversarial end-to-end suite that drives the REAL app.py through hostile
interaction sequences with streamlit.testing.v1.AppTest.

First-principles only: correctness here is derived from the domain contract
(a wallet's data must never mix with another wallet's; fake fixture data must
never be presented as real; manual-refresh only; read-only) and from the
public signatures of the modules app.py wires -- never from any prose in the
repo.

Isolation (mandatory):
  * Every AppTest points app.py's DB at a per-test tmp_path via the
    POLYMARKET_TRACKER_DB env var, and clears Streamlit's process-wide
    @st.cache_resource so a connection opened under another test's tmp_path is
    never reused. See ``_boot``.
  * ``_no_network`` (autouse) replaces polymarket_client.PolymarketSource with
    an offline stub for EVERY test, so a stray real-mode refresh can never hit
    the network. Tests that need specific "real" positions re-patch it.
  * ``_guard_real_db`` (autouse) snapshots the user's real
    data/polymarket_tracker.db before each test and asserts it is byte-for-byte
    untouched afterward.

Convention (per the deliverable brief):
  * behaviour the code gets RIGHT  -> a passing test
  * each REAL BUG                  -> @pytest.mark.xfail(strict=True, ...)
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
import streamlit as st

import app as app_module
import db as db_module
import polymarket_client
from models import Position
from streamlit.testing.v1 import AppTest

# Distinct, clearly-fake wallets. Never a real address (pre-commit hook + rule).
WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40
ZERO = "0x" + "0" * 40

REAL_DB = Path(app_module.__file__).parent / "data" / "polymarket_tracker.db"


# ---------------------------------------------------------------------------
# Isolation fixtures
# ---------------------------------------------------------------------------
def _real_db_snapshot():
    if not REAL_DB.exists():
        return None
    s = REAL_DB.stat()
    return (s.st_size, s.st_mtime_ns)


@pytest.fixture(autouse=True)
def _guard_real_db():
    """The user's real DB must be byte-for-byte untouched by any test."""
    before = _real_db_snapshot()
    yield
    after = _real_db_snapshot()
    assert after == before, (
        f"REAL DB was modified during the test: {before!r} -> {after!r}. "
        f"Isolation failed at {REAL_DB}."
    )


class _NoNetworkSource:
    """Offline default for PolymarketSource: never touches the network."""

    def __init__(self, *args, **kwargs):
        pass

    def fetch(self, wallet):  # noqa: D401
        return []


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    # app.py does `from polymarket_client import PolymarketSource`; because
    # AppTest re-execs app.py's source on every run, that import re-resolves
    # against this patched attribute, so the stub is what real mode uses.
    monkeypatch.setattr(polymarket_client, "PolymarketSource", _NoNetworkSource)
    yield
    # Drop the cached sqlite connection so tmp_path teardown isn't file-locked
    # on Windows and no connection leaks into the next test.
    st.cache_resource.clear()


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------
def _boot(tmp_path, monkeypatch, name="adv.db") -> AppTest:
    monkeypatch.setenv("POLYMARKET_TRACKER_DB", str(tmp_path / name))
    st.cache_resource.clear()
    at = AppTest.from_file(app_module.__file__, default_timeout=60)
    at.run()
    return at


def _seed_db(tmp_path, name="adv.db"):
    """Return a fresh sqlite path with the real schema applied, connection closed."""
    path = tmp_path / name
    conn = db_module.init_db(path)
    conn.close()
    return path


def tog(at, label="Use fake data"):
    return next(t for t in at.toggle if t.label == label)


def txt(at, label):
    return next(t for t in at.text_input if t.label == label)


def btn(at, label):
    return next(b for b in at.button if b.label == label)


def box(at, label):
    return next(s for s in at.selectbox if s.label == label)


def compare_options(at):
    return list(box(at, "Compare against").options)


def markets(at):
    return list(at.dataframe[0].value["Market"]) if at.dataframe else []


def _fake_refresh(at, wallet=ZERO, scenario="before_match"):
    """Turn on fake data, set wallet, pick scenario, click Refresh."""
    tog(at).set_value(True)
    at.run()
    txt(at, "Wallet address").set_value(wallet)
    if scenario != "before_match":
        box(at, "Scenario").set_value(scenario)
    btn(at, "Refresh").click()
    at.run()
    assert not at.exception, at.exception


def _save_checkpoint(at, label):
    txt(at, "Checkpoint label").set_value(label)
    btn(at, "Save checkpoint").click()
    at.run()


def _count_checkpoints(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
    finally:
        conn.close()


class _StubRealSource:
    """A stand-in for the live source that returns a fixed, offline position
    list whose asset ids never collide with the JSON fixtures."""

    calls: list[str] = []
    positions: list[Position] = []

    def __init__(self, *args, **kwargs):
        pass

    def fetch(self, wallet):
        type(self).calls.append(wallet)
        return list(type(self).positions)


def _real_position(asset="real_asset_1", title="REAL Lakers win", value=2.0):
    return Position.from_api(
        {
            "asset": asset,
            "conditionId": "0xreal",
            "title": title,
            "outcome": "Yes",
            "size": 3.0,
            "avgPrice": 0.5,
            "initialValue": 1.5,
            "currentValue": value,
            "curPrice": 0.66,
            "realizedPnl": 0.0,
        }
    )


# ===========================================================================
# CORRECT BEHAVIOUR  (attacks that failed -> passing tests)
# ===========================================================================
def test_stale_guard_blocks_saving_A_positions_under_wallet_B_fake(tmp_path, monkeypatch):
    """Fetch for A, retype the wallet to B WITHOUT refreshing, click Save.
    The staleness guard must refuse and NOT persist A's positions under B."""
    at = _boot(tmp_path, monkeypatch)
    _fake_refresh(at, wallet=WALLET_A)
    assert len(markets(at)) == 3

    txt(at, "Wallet address").set_value(WALLET_B)  # change wallet, no Refresh
    _save_checkpoint(at, "sneaky")

    assert any("stale" in w.value.lower() for w in at.warning)
    # Nothing may be written for EITHER wallet.
    assert _count_checkpoints(tmp_path / "adv.db") == 0


def test_stale_guard_blocks_cross_wallet_save_in_real_mode(tmp_path, monkeypatch):
    """Same attack through the real (stubbed) source path, which is the actual
    product flow: positions fetched for A must never be saved under B."""
    _StubRealSource.calls = []
    _StubRealSource.positions = [_real_position()]
    monkeypatch.setattr(polymarket_client, "PolymarketSource", _StubRealSource)

    at = _boot(tmp_path, monkeypatch)
    txt(at, "Wallet address").set_value(WALLET_A)
    btn(at, "Refresh").click()
    at.run()
    assert markets(at) == ["REAL Lakers win"]

    txt(at, "Wallet address").set_value(WALLET_B)  # switch wallet, no refresh
    _save_checkpoint(at, "sneaky")
    assert any("stale" in w.value.lower() for w in at.warning)
    assert _count_checkpoints(tmp_path / "adv.db") == 0


def test_stale_guard_blocks_comparison_after_scenario_change(tmp_path, monkeypatch):
    """Switch the fixture scenario WITHOUT refreshing, then select a saved
    checkpoint. The app must flag staleness rather than silently comparing the
    new scenario's checkpoint against the old scenario's positions."""
    at = _boot(tmp_path, monkeypatch)
    _fake_refresh(at, wallet=WALLET_A, scenario="before_match")
    _save_checkpoint(at, "Before match")

    box(at, "Scenario").set_value("after_goal")  # change scenario, NO refresh
    at.run()
    assert any("changed since the last fetch" in i.value for i in at.info)
    # With positions treated as stale, the table must not present after_goal
    # numbers as if they were live for this (unrefreshed) selection.
    assert markets(at) == []


def test_per_wallet_checkpoint_scoping_is_honoured(tmp_path, monkeypatch):
    """A checkpoint saved for A is invisible under B and reappears under A."""
    at = _boot(tmp_path, monkeypatch)
    _fake_refresh(at, wallet=WALLET_A)
    _save_checkpoint(at, "A-only")
    assert any(o.startswith("A-only") for o in compare_options(at))

    txt(at, "Wallet address").set_value(WALLET_B)
    at.run()
    assert not any(o.startswith("A-only") for o in compare_options(at))  # scoped out

    txt(at, "Wallet address").set_value(WALLET_A)
    at.run()
    assert any(o.startswith("A-only") for o in compare_options(at))  # back in scope


class _ValidatingSource:
    """Runs the real wallet validation, then stops. Never touches the network.

    The autouse _no_network fixture has already replaced
    polymarket_client.PolymarketSource with a stub whose fetch() returns [] and
    never validates -- so re-assigning that attribute to itself (as this test
    originally did) leaves the stub in place and the invalid wallet is never
    caught. Use a source that genuinely validates instead.
    """

    def __init__(self, *args, **kwargs):
        pass

    def fetch(self, wallet):
        polymarket_client.validate_wallet(wallet)  # raises InvalidWalletError
        return []


def test_invalid_wallet_in_real_mode_is_a_clean_banner(tmp_path, monkeypatch):
    """Validation happens before any HTTP, so this stays offline. A garbage
    wallet must produce a readable error, never an uncaught traceback."""
    monkeypatch.setattr(polymarket_client, "PolymarketSource", _ValidatingSource)
    at = _boot(tmp_path, monkeypatch)
    txt(at, "Wallet address").set_value("not-a-wallet")
    btn(at, "Refresh").click()
    at.run()

    assert not at.exception
    assert any("not a valid wallet" in e.value.lower() for e in at.error)


def test_db_write_error_surfaces_as_banner_not_traceback(tmp_path, monkeypatch):
    """A non-finite numeric field (rejected by db._require_finite) must surface
    as a 'Could not save checkpoint' banner, never an uncaught exception, and
    must NOT report success."""
    bad = Position(
        asset="a1", condition_id="c", market_title="M", event_slug="e", outcome="Yes",
        size=float("inf"), entry_price=0.5, current_price=0.5, stake=1.0,
        current_value=1.0, open_pnl=0.0, percent_pnl=0.0, realized_pnl=0.0,
        redeemable=False, end_date="",
    )
    _StubRealSource.calls = []
    _StubRealSource.positions = [bad]
    monkeypatch.setattr(polymarket_client, "PolymarketSource", _StubRealSource)

    at = _boot(tmp_path, monkeypatch)
    txt(at, "Wallet address").set_value(WALLET_A)
    btn(at, "Refresh").click()
    at.run()
    _save_checkpoint(at, "infcp")

    assert not at.exception
    assert any("could not save checkpoint" in e.value.lower() for e in at.error)
    assert not at.success
    assert _count_checkpoints(tmp_path / "adv.db") == 0  # rolled back / never inserted


def test_sqlish_and_emoji_labels_are_stored_literally(tmp_path, monkeypatch):
    """Labels are user free-text bound through parametrised SQL; an injection
    string or emoji must be stored verbatim and must not damage the schema."""
    at = _boot(tmp_path, monkeypatch)
    _fake_refresh(at, wallet=WALLET_A)

    injection = "'; DROP TABLE checkpoints;--"
    _save_checkpoint(at, injection)
    _save_checkpoint(at, "goal 🎉 Coupe du Monde")

    conn = sqlite3.connect(tmp_path / "adv.db")
    try:
        labels = {r[0] for r in conn.execute("SELECT label FROM checkpoints")}
    finally:
        conn.close()
    assert injection in labels          # table survived, stored verbatim
    assert "goal 🎉 Coupe du Monde" in labels


def test_all_new_default_state_renders_without_crashing(tmp_path, monkeypatch):
    """The app's default state right after the very first refresh is 'no
    checkpoint selected', so every row is NEW and every checkpoint-derived
    column is missing. Rendering that must not raise."""
    at = _boot(tmp_path, monkeypatch)
    _fake_refresh(at, wallet=WALLET_A)  # no checkpoint selected -> all NEW

    assert not at.exception
    df = at.dataframe[0].value
    assert len(df) == 3
    # checkpoint-derived columns are missing for NEW rows and render as blanks
    assert df["Checkpoint Value"].isna().all()
    assert df["Change Since Checkpoint"].isna().all()


def test_app_has_no_autorefresh_threads_or_advice_text(tmp_path, monkeypatch):
    """Scope/rules: manual-refresh only (no timers/threads/websockets) and no
    advisory language in user-facing strings."""
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("time.sleep", "threading", "Thread(", "websocket",
                      "st_autorefresh", "add_script_run_ctx"):
        assert forbidden not in source, f"scope violation: {forbidden}"
    # advice phrasing (guard against false positives like 'placeholder')
    assert re.search(
        r"\b(cash\s*out\s*now|good bet|you should (?:sell|buy|hold)|we recommend)\b",
        source, re.I,
    ) is None


def test_empty_checkpoint_save_is_currently_accepted(tmp_path, monkeypatch):
    """DOCUMENTS current behaviour (minor design gap): saving with zero
    positions loaded creates an empty checkpoint and reports success with no
    warning. Not silently-wrong data, so recorded as a passing test."""
    _StubRealSource.calls = []
    _StubRealSource.positions = []  # wallet with no positions
    monkeypatch.setattr(polymarket_client, "PolymarketSource", _StubRealSource)

    at = _boot(tmp_path, monkeypatch)
    txt(at, "Wallet address").set_value(WALLET_A)
    btn(at, "Refresh").click()
    at.run()
    _save_checkpoint(at, "emptycp")

    assert any("saved checkpoint" in s.value.lower() for s in at.success)
    conn = sqlite3.connect(tmp_path / "adv.db")
    try:
        ncp = conn.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
        npos = conn.execute("SELECT count(*) FROM checkpoint_positions").fetchone()[0]
    finally:
        conn.close()
    assert (ncp, npos) == (1, 0)  # empty checkpoint persisted


def test_normalised_equal_wallet_goes_stale_but_data_stays_safe(tmp_path, monkeypatch):
    """DOCUMENTS current (safe but over-strict) behaviour: a trailing space on
    the wallet -- the SAME account after normalisation -- makes the loaded
    positions read as stale. This is conservative, not contamination: the table
    empties and a Refresh banner appears, and the checkpoint is still correctly
    scoped in the DB (which DOES normalise). The over-strictness is a MINOR
    usability issue captured separately in the findings report."""
    at = _boot(tmp_path, monkeypatch)
    _fake_refresh(at, wallet=ZERO)
    _save_checkpoint(at, "CP")

    txt(at, "Wallet address").set_value(ZERO + " ")  # same account, trailing space
    at.run()

    assert any("changed since the last fetch" in i.value for i in at.info)
    assert markets(at) == []                                   # over-strict: emptied
    assert any(o.startswith("CP") for o in compare_options(at))  # DB still finds it


# ===========================================================================
# REAL BUGS  (reproduced -> strict xfail; each flips green when fixed)
# ===========================================================================
@pytest.mark.xfail(
    strict=True,
    reason="BUG: a fake-mode checkpoint is indistinguishable from a real one "
    "and is silently compared against real positions -- fixture props "
    "(e.g. 'Morocco wins') are injected into a real-wallet view with no warning.",
)
def test_fake_checkpoint_must_not_mix_into_a_real_comparison(tmp_path, monkeypatch):
    at = _boot(tmp_path, monkeypatch)
    # Save a checkpoint while in FAKE mode, under a wallet (the wallet field is
    # pre-fillable from settings, so this is a realistic accidental mix).
    _fake_refresh(at, wallet=WALLET_A, scenario="before_match")
    _save_checkpoint(at, "FakeBaseline")

    # Switch to REAL data (stubbed, offline) for the SAME wallet and refresh.
    _StubRealSource.calls = []
    _StubRealSource.positions = [_real_position()]
    monkeypatch.setattr(polymarket_client, "PolymarketSource", _StubRealSource)
    tog(at).set_value(False)
    at.run()
    btn(at, "Refresh").click()
    at.run()
    assert markets(at) == ["REAL Lakers win"]

    # Compare against the fake checkpoint.
    opt = next(o for o in compare_options(at) if o.startswith("FakeBaseline"))
    box(at, "Compare against").set_value(opt)
    at.run()

    # DESIRED: fixture-only markets must never appear in a real comparison.
    assert "Morocco wins" not in markets(at)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: the compare dropdown is keyed on label+created_at (second "
    "precision). Two checkpoints saved with the same label in the same second "
    "collide, so one becomes permanently unreachable in the UI.",
)
def test_same_label_same_second_checkpoints_are_both_selectable(tmp_path, monkeypatch):
    # Deterministically reproduce the collision by seeding two checkpoints that
    # share label AND created_at (SQLite datetime('now') has second precision,
    # so two rapid saves realistically land here).
    path = _seed_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO checkpoints (wallet_address, label, created_at) VALUES (?,?,?)",
        (WALLET_A, "before match", "2026-07-10 12:00:00"),
    )
    conn.execute(
        "INSERT INTO checkpoints (wallet_address, label, created_at) VALUES (?,?,?)",
        (WALLET_A, "before match", "2026-07-10 12:00:00"),
    )
    conn.commit()
    conn.close()

    at = _boot(tmp_path, monkeypatch)  # same db path; init_db is IF NOT EXISTS
    tog(at).set_value(True)            # fake mode: avoid any real fetch
    txt(at, "Wallet address").set_value(WALLET_A)
    at.run()

    # Two distinct saved checkpoints exist; both must be selectable.
    selectable = [o for o in compare_options(at) if o != "(none)"]
    assert len(selectable) == 2


# FIXED: was a strict xfail; the bug is fixed, so this now guards it.
def test_opening_the_app_does_not_fetch_until_refresh(tmp_path, monkeypatch):
    # A saved wallet already in settings is the trigger.
    path = _seed_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO settings (id, wallet_address) VALUES (1, ?)", (WALLET_A,)
    )
    conn.commit()
    conn.close()

    _StubRealSource.calls = []
    _StubRealSource.positions = [_real_position(title="SHOULD NOT AUTOLOAD")]
    monkeypatch.setattr(polymarket_client, "PolymarketSource", _StubRealSource)

    at = _boot(tmp_path, monkeypatch)  # FIRST load only; no Refresh clicked

    # DESIRED: nothing is fetched until the user asks for it.
    assert _StubRealSource.calls == []
    assert markets(at) == []


# FIXED: was a strict xfail; the bug is fixed, so this now guards it.
def test_whitespace_only_label_is_rejected(tmp_path, monkeypatch):
    at = _boot(tmp_path, monkeypatch)
    _fake_refresh(at, wallet=WALLET_A)
    _save_checkpoint(at, "   ")  # whitespace only

    # DESIRED: rejected with a warning, nothing persisted.
    assert any("label" in w.value.lower() for w in at.warning)
    assert _count_checkpoints(tmp_path / "adv.db") == 0
