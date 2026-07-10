"""The scenarios from initial_plan.md Step 14. These decide correctness.

Part A: logic-level acceptance, transcribed from the Task 9 brief
(.superpowers/sdd/task-9-brief.md) and reconciled against the live,
post-hardening code in calculations.py / models.py / db.py. No reconciliation
turned out to be necessary -- every symbol, signature, and behavior the brief
assumes (compare, summarize, Status, CheckpointRow.from_position, the
db.init_db -> create_checkpoint -> save_checkpoint_positions ->
load_checkpoint_positions round trip) matches the current modules exactly, so
Part A is the brief's test code unchanged.

Part B: UI-level end-to-end acceptance (new, beyond the brief). Drives the
real Streamlit app via streamlit.testing.v1.AppTest to prove the same
Step-14 promise through the actual widget wiring -- toggle, selectboxes,
buttons, and the rendered st.dataframe -- rather than only the pure
functions underneath it. Every AppTest here repoints app.DB_PATH at a
pytest-managed tmp_path and clears the cached @st.cache_resource connection,
so the user's real data/*.db is never touched.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

import app as app_module
import db
from calculations import compare, summarize
from fixtures import FixtureSource
from models import CheckpointRow, Status
from streamlit.testing.v1 import AppTest

WALLET = "0x" + "0" * 40


# ===========================================================================
# Part A -- logic-level acceptance (FixtureSource + compare/summarize + db)
# ===========================================================================


def _rows(current_scenario: str, checkpoint_scenario: str):
    current = FixtureSource(current_scenario).fetch(WALLET)
    checkpoint = [
        CheckpointRow.from_position(p)
        for p in FixtureSource(checkpoint_scenario).fetch(WALLET)
    ]
    return {r.market_title: r for r in compare(current, checkpoint)}


def test_after_a_goal_each_prop_shows_the_expected_change():
    rows = _rows("after_goal", "before_match")
    assert math.isclose(rows["Morocco wins"].change_since_checkpoint, 5.0)
    assert math.isclose(rows["0-0 first half"].change_since_checkpoint, -2.0)
    assert math.isclose(rows["France 2-1"].change_since_checkpoint, -2.0)


def test_after_a_goal_the_biggest_mover_is_first():
    current = FixtureSource("after_goal").fetch(WALLET)
    checkpoint = [
        CheckpointRow.from_position(p)
        for p in FixtureSource("before_match").fetch(WALLET)
    ]
    assert compare(current, checkpoint)[0].market_title == "Morocco wins"


def test_a_cashout_is_closed_and_never_a_market_loss():
    rows = _rows("after_cashout", "after_goal")
    morocco = rows["Morocco wins"]
    assert morocco.status is Status.CLOSED
    # It was worth $10 at the checkpoint. A -$10 "loss" would be a lie.
    assert morocco.change_since_checkpoint is None
    assert morocco.current_value is None
    assert morocco.checkpoint_value == 10.0


def test_a_cashout_is_excluded_from_the_totals():
    rows = list(_rows("after_cashout", "after_goal").values())
    summary = summarize(rows)
    assert summary.open_positions == 2  # Morocco is gone, not counted


def test_surviving_props_are_unaffected_by_the_cashout():
    rows = _rows("after_cashout", "after_goal")
    assert rows["0-0 first half"].status is Status.OPEN
    assert rows["France 2-1"].status is Status.OPEN
    assert math.isclose(rows["France 2-1"].change_since_checkpoint, 0.0, abs_tol=1e-9)


def test_a_partial_cashout_reads_reduced_not_closed():
    """The sizeThreshold=0 failure mode: a position shrunk below 1 share must
    still be Reduced, never Closed. With the API's default sizeThreshold of 1,
    this row would not come back at all and would be reported Closed.

    dataclasses.replace keeps every other field (percent_pnl, realized_pnl,
    redeemable, end_date) at its original, already-finite fixture value, so
    the shrunk Position stays finite in every field -- required because
    db.save_checkpoint_positions rejects non-finite numbers at the write
    boundary (db._require_finite). This test never touches the db, but the
    shrunk Position is built the same finite-safe way regardless.
    """
    before = FixtureSource("before_match").fetch(WALLET)
    checkpoint = [CheckpointRow.from_position(p) for p in before]

    morocco = next(p for p in before if p.market_title == "Morocco wins")
    shrunk = dataclasses.replace(
        morocco, size=0.4, current_value=0.4, stake=0.2, open_pnl=0.2
    )
    rows = {r.market_title: r for r in compare([shrunk], checkpoint)}
    assert rows["Morocco wins"].status is Status.REDUCED


@pytest.mark.parametrize("scenario", ["before_match", "after_goal", "after_cashout"])
def test_the_full_round_trip_through_sqlite_preserves_the_join_key(tmp_path, scenario):
    conn = db.init_db(tmp_path / "acc.db")
    positions = FixtureSource(scenario).fetch(WALLET)
    checkpoint_id = db.create_checkpoint(conn, WALLET, scenario)
    db.save_checkpoint_positions(conn, checkpoint_id, positions)

    reloaded = db.load_checkpoint_positions(conn, checkpoint_id)
    assert {r.asset for r in reloaded} == {p.asset for p in positions}

    # Comparing a scenario against itself must yield all-Open, zero change.
    rows = compare(positions, reloaded)
    assert all(r.status is Status.OPEN for r in rows)
    assert all(math.isclose(r.change_since_checkpoint, 0.0) for r in rows)
    conn.close()


# ===========================================================================
# Part B -- UI-level end-to-end acceptance (AppTest drives the real app.py)
# ===========================================================================


def _fresh_app(tmp_path) -> AppTest:
    """Point the app at a throwaway DB per test so the user's real
    data/*.db is never touched, then run it once to get past the initial
    script execution."""
    app_module.DB_PATH = tmp_path / "acc.db"
    app_module._connection.clear()  # drop any cached @st.cache_resource connection
    at = AppTest.from_file(app_module.__file__, default_timeout=30)
    at.run()
    return at


def _box(at: AppTest, label: str):
    return next(s for s in at.selectbox if s.label == label)


def _btn(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def _txt(at: AppTest, label: str):
    return next(t for t in at.text_input if t.label == label)


def _enable_fake_data_and_refresh(at: AppTest) -> None:
    """Flip the "Use fake data" toggle, set the wallet, and press Refresh.
    Leaves the scenario at its default (SCENARIOS[0] == "before_match")."""
    at.toggle[0].set_value(True)
    at.run()
    _txt(at, "Wallet address").set_value(WALLET)
    _btn(at, "Refresh").click()
    at.run()
    assert not at.exception


def _save_checkpoint(at: AppTest, label: str) -> None:
    _txt(at, "Checkpoint label").set_value(label)
    _btn(at, "Save checkpoint").click()
    at.run()
    assert not at.exception


def _switch_scenario_and_refresh(at: AppTest, scenario: str) -> None:
    _box(at, "Scenario").set_value(scenario)
    _btn(at, "Refresh").click()
    at.run()
    assert not at.exception


def _select_checkpoint(at: AppTest, label_prefix: str) -> None:
    compare_box = _box(at, "Compare against")
    option = next(o for o in compare_box.options if o.startswith(label_prefix))
    compare_box.set_value(option)
    at.run()
    assert not at.exception


def test_fake_before_match_refresh_renders_the_three_props(tmp_path):
    at = _fresh_app(tmp_path)
    _enable_fake_data_and_refresh(at)

    assert len(at.dataframe) == 1
    raw = at.dataframe[0].value
    assert len(raw) == 3
    assert set(raw["Market"]) == {"Morocco wins", "0-0 first half", "France 2-1"}


def test_after_goal_vs_saved_checkpoint_morocco_is_plus_five_and_first(tmp_path):
    at = _fresh_app(tmp_path)
    _enable_fake_data_and_refresh(at)  # loads before_match
    _save_checkpoint(at, "Before match")
    _switch_scenario_and_refresh(at, "after_goal")
    _select_checkpoint(at, "Before match")

    raw = at.dataframe[0].value
    # Biggest absolute mover sorts first (Step 14 / calculations.sort_rows).
    assert raw.iloc[0]["Market"] == "Morocco wins"
    change = raw.set_index("Market").loc["Morocco wins", "Change Since Checkpoint"]
    assert math.isclose(change, 5.00)


def test_after_cashout_morocco_is_closed_with_no_fabricated_loss(tmp_path):
    at = _fresh_app(tmp_path)
    _enable_fake_data_and_refresh(at)  # loads before_match
    _save_checkpoint(at, "Before match")
    _switch_scenario_and_refresh(at, "after_cashout")
    _select_checkpoint(at, "Before match")

    raw = at.dataframe[0].value.set_index("Market")
    assert raw.loc["Morocco wins", "Size Status"] == Status.CLOSED.value
    # The app never saw the cashout proceeds: "Now" must read as missing
    # (rendered "--" by ui.py), never as a fabricated -$10 loss.
    assert math.isnan(raw.loc["Morocco wins", "Now"])
    assert math.isnan(raw.loc["Morocco wins", "Change Since Checkpoint"])
