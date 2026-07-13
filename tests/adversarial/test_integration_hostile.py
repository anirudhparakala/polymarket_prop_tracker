"""End-to-end adversarial tests: does the ASSEMBLED system keep its promises?

The lower layers (models normalization, db write guards, pure compare) were each
hardened in isolation. This file attacks the *composition* -- source -> compare
-> rows_to_frame -> style_frame (and the real app.py wiring via AppTest) -- to
confirm the newest layers (UI render + app wiring) did not re-introduce, at the
top of the stack, a correctness problem the lower layers were built to prevent.

Every property is asserted at the OUTPUT (rendered frame / styled HTML / summary
card), not just inside a unit.

Correctness is derived from first principles, not from repo prose:
  1. A vanished position (cashout) is Closed, never a red market loss.
  2. Money/share fields must never carry NaN/Inf into what the user sees.
  3. One wallet's data must never mix with another's.
  4. Row order must be STABLE across refreshes / processes.
  5. Numbers the app never measured render as an em-dash, not a fabricated value.

Isolation: no network. Every db/app test writes to a tmp path; app tests set
POLYMARKET_TRACKER_DB and clear @st.cache_resource so the user's real
data/polymarket_tracker.db is never opened, read, or written. A final test
asserts that file was never created.

Bugs found in the assembled system are marked xfail(strict=True): the test
asserts the CORRECT behavior, so it fails today and will flip to an unexpected
pass (failing the strict xfail) the moment someone closes the gap -- forcing the
marker to be removed rather than silently rotting.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import app as app_module
import db
from calculations import compare, summarize
from fixtures import FixtureSource
from models import CheckpointRow, Position, Status
from ui import rows_to_frame, style_frame

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_DB = REPO_ROOT / "data" / "polymarket_tracker.db"
WALLET = "0x" + "0" * 40


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def pos(asset: str, **kw) -> Position:
    base = dict(
        asset=asset,
        condition_id="0xc",
        market_title=f"M{asset}",
        event_slug="e",
        outcome="Yes",
        size=10.0,
        entry_price=0.5,
        current_price=0.5,
        stake=5.0,
        current_value=5.0,
        open_pnl=0.0,
        percent_pnl=0.0,
        realized_pnl=0.0,
        redeemable=False,
        end_date="2026-07-09",
    )
    base.update(kw)
    return Position(**base)


def ckpt(p: Position) -> CheckpointRow:
    return CheckpointRow.from_position(p)


def _render(rows) -> tuple[pd.DataFrame, str]:
    """Run the exact render pipeline ui.render_table uses."""
    frame = rows_to_frame(rows)
    return frame, style_frame(frame).to_html()


def _cell_text(html: str, frame: pd.DataFrame, column: str) -> str:
    tds = re.findall(r"<td[^>]*>.*?</td>", html)
    return tds[list(frame.columns).index(column)]


def _cell_color(html: str, frame: pd.DataFrame, column: str) -> str:
    """pandas keys colors in a <style> block by cell id, not inline on the td."""
    col_idx = list(frame.columns).index(column)
    m = re.search(rf'<td id="([^"]*row0_col{col_idx})"[^>]*>', html)
    if not m:
        return ""
    rule = re.search(rf"#{re.escape(m.group(1))}\s*\{{([^}}]*)\}}", html)
    return rule.group(1).strip() if rule else ""


def _row_has_no_red(html: str, frame: pd.DataFrame, row_idx: int) -> bool:
    """True if no cell in the given styled row carries a red color rule."""
    for col_idx in range(len(frame.columns)):
        m = re.search(rf'<td id="([^"]*row{row_idx}_col{col_idx})"[^>]*>', html)
        if not m:
            continue
        rule = re.search(rf"#{re.escape(m.group(1))}\s*\{{([^}}]*)\}}", html)
        if rule and "red" in rule.group(1):
            return False
    return True


# ===========================================================================
# Property 5 + 1: a cashout renders as Closed with "missing" cells, never red
# ===========================================================================
def test_cashout_renders_closed_missing_never_red_end_to_end():
    """Position at checkpoint, absent now -> Closed. Now / Change / Since Entry /
    Realized are never-measured, so they render em-dash, never colored, never a
    fabricated -$10 loss. Verified on the styled HTML the user actually sees."""
    rows = compare([], [ckpt(pos("A", current_value=10.0, stake=6.0, open_pnl=4.0))])
    assert rows[0].status is Status.CLOSED
    frame, html = _render(rows)

    for col in ("Now", "Change Since Checkpoint", "Since Entry", "Realized"):
        assert "—" in _cell_text(html, frame, col), f"{col} should be em-dash"
        assert "$0.00" not in _cell_text(html, frame, col)
        assert _cell_color(html, frame, col) == "", f"{col} must not be colored"
    # Checkpoint value is a real measurement and must survive.
    assert "$10.00" in _cell_text(html, frame, "Checkpoint Value")
    # The whole Closed row must carry no red anywhere.
    assert "color: red" not in html


def test_cashout_excluded_from_summary_totals():
    rows = compare(
        [pos("A", current_value=8.0, stake=5.0, open_pnl=3.0)],
        [ckpt(pos("A")), ckpt(pos("B", current_value=10.0))],  # B vanished -> Closed
    )
    statuses = {r.asset: r.status for r in rows}
    assert statuses["B"] is Status.CLOSED
    summary = summarize(rows)
    assert summary.open_positions == 1  # B not counted
    # Totals equal exactly the one surviving live row, not the phantom cashout.
    assert math.isclose(summary.total_stake, 5.0)
    assert math.isclose(summary.current_value, 8.0)


# ===========================================================================
# Property 4: determinism through the whole stack across processes
# ===========================================================================
_DET_PROBE = textwrap.dedent(
    """
    from models import Position, CheckpointRow
    from calculations import compare
    from ui import rows_to_frame
    def pos(a, cv):
        return Position(asset=a, condition_id='0xc', market_title='M'+a,
            event_slug='e', outcome='Yes', size=10.0, entry_price=0.5,
            current_price=0.5, stake=5.0, current_value=cv, open_pnl=cv-5.0,
            percent_pnl=0.0, realized_pnl=0.0, redeemable=False, end_date='d')
    assets = [f'{i:064d}' for i in range(1, 13)]
    cur, ck = [], []
    for i, a in enumerate(assets):
        ck.append(CheckpointRow.from_position(pos(a, 5.0)))
        cur.append(pos(a, 6.0 if i % 2 == 0 else 4.0))  # abs(change)=1.0 for ALL -> ties
    cur.append(pos('f'*64, 7.0))   # New (no checkpoint) -> tier-1, change None
    cur.append(pos('e'*64, 7.0))   # New
    print('|'.join(rows_to_frame(compare(cur, ck))['Market']))
    """
)


def _row_order_with_seed(seed: str) -> str:
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(REPO_ROOT))
    out = subprocess.run(
        [sys.executable, "-c", _DET_PROBE],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_row_order_is_identical_across_hash_seeds():
    """Equal-magnitude movers and the entire New/Closed tier all key on the same
    change value; only the `asset` tiebreaker keeps them ordered. If that were
    dropped, set-union iteration order (PYTHONHASHSEED-dependent) would reshuffle
    the table on every refresh in a different process."""
    orders = [_row_order_with_seed(s) for s in ("0", "1", "12345", "999999")]
    assert len(set(orders)) == 1, f"row order drifted across seeds:\n{orders}"


# ===========================================================================
# Round-trip fidelity through SQLite (tmp DB only)
# ===========================================================================
def test_round_trip_through_sqlite_is_exact_and_all_open(tmp_path):
    conn = db.init_db(tmp_path / "rt.db")
    positions = [
        pos(
            "90548087076000000000000000000000000000000000000000000000000000001",
            size=90548.087076,
            current_value=0.1 + 0.2,  # a classic non-exact binary float
            stake=1.0 / 3.0,
            entry_price=0.123456789012345,
        ),
        pos(
            "90548087076000000000000000000000000000000000000000000000000000002",
            size=1e-9,
            current_value=1e12,
        ),
    ]
    cid = db.create_checkpoint(conn, WALLET, "rt")
    db.save_checkpoint_positions(conn, cid, positions)
    reloaded = db.load_checkpoint_positions(conn, cid)

    # asset (a 65-char numeric string) must survive TEXT affinity, not be
    # coerced to a number.
    assert {r.asset for r in reloaded} == {p.asset for p in positions}
    for p in positions:
        r = next(x for x in reloaded if x.asset == p.asset)
        assert r.size == p.size
        assert r.current_value == p.current_value
        assert r.stake == p.stake
        assert r.entry_price == p.entry_price

    # Compare a scenario against itself: every row Open, zero change, exact.
    rows = compare(positions, reloaded)
    assert all(r.status is Status.OPEN for r in rows)
    assert all(r.change_since_checkpoint == 0.0 for r in rows)
    conn.close()


@pytest.mark.parametrize("scenario", ["before_match", "after_goal", "after_cashout"])
def test_fixture_round_trip_renders_all_open_zero_change(tmp_path, scenario):
    conn = db.init_db(tmp_path / "f.db")
    positions = FixtureSource(scenario).fetch(WALLET)
    cid = db.create_checkpoint(conn, WALLET, scenario)
    db.save_checkpoint_positions(conn, cid, positions)
    reloaded = db.load_checkpoint_positions(conn, cid)

    frame = rows_to_frame(compare(positions, reloaded))
    # Self-comparison: every row Open, and the checkpoint-RELATIVE columns
    # (change and price change) are exactly zero. (Since Entry / Realized reflect
    # absolute position state, not the checkpoint delta, so they may still show a
    # real loss -- that is correct, not a round-trip failure.)
    assert set(frame["Size Status"]) == {Status.OPEN.value}
    assert (frame["Change Since Checkpoint"].abs() < 1e-9).all()
    assert (frame["Price Change"].abs() < 1e-9).all()
    conn.close()


# ===========================================================================
# Property 3: wallet data never mixes
# ===========================================================================
def test_checkpoints_are_wallet_scoped(tmp_path):
    conn = db.init_db(tmp_path / "w.db")
    wa, wb = "0x" + "a" * 40, "0x" + "b" * 40
    db.save_checkpoint_positions(conn, db.create_checkpoint(conn, wa, "A-cp"), [pos("a1")])
    db.save_checkpoint_positions(conn, db.create_checkpoint(conn, wb, "B-cp"), [pos("b1")])

    assert [c["label"] for c in db.list_checkpoints(conn, wa)] == ["A-cp"]
    assert [c["label"] for c in db.list_checkpoints(conn, wb)] == ["B-cp"]
    # Same account pasted mixed-case with whitespace is one identity.
    messy = "  0X" + "A" * 40 + "\n"
    assert [c["label"] for c in db.list_checkpoints(conn, messy)] == ["A-cp"]
    conn.close()


# ===========================================================================
# Duplicate / degenerate inputs hold end-to-end
# ===========================================================================
def test_duplicate_assets_collapse_and_do_not_double_count():
    dup = [pos("X", current_value=5.0, stake=5.0), pos("X", current_value=8.0, stake=5.0)]
    rows = compare(dup, [])
    assert len(rows) == 1
    assert rows[0].current_value == 8.0  # last occurrence wins
    assert summarize(rows).current_value == 8.0  # not 13.0


def test_all_checkpoint_assets_vanished_renders_all_closed_never_red():
    rows = compare([], [ckpt(pos("C1", current_value=10.0)), ckpt(pos("C2", current_value=4.0))])
    frame, html = _render(rows)
    assert set(frame["Size Status"]) == {Status.CLOSED.value}
    assert "color: red" not in html
    assert summarize(rows).open_positions == 0


def test_empty_both_sides_is_empty_frame():
    frame = rows_to_frame(compare([], []))
    assert frame.empty


def test_brand_new_assets_are_new_and_counted():
    rows = compare([pos("N", current_value=9.0, stake=6.0, open_pnl=3.0)], [])
    assert rows[0].status is Status.NEW
    frame, html = _render(rows)
    # A New row has no checkpoint, so Checkpoint Value / Change are unmeasured.
    assert "—" in _cell_text(html, frame, "Change Since Checkpoint")
    assert summarize(rows).open_positions == 1  # New IS a live position


# ===========================================================================
# Property: summary card totals equal the sum of the non-closed table rows
# ===========================================================================
def test_summary_equals_sum_of_non_closed_table_rows():
    current = [
        pos("A", size=10.0, current_value=8.0, stake=5.0, open_pnl=3.0),  # Open
        pos("B", size=4.0, current_value=2.0, stake=5.0, open_pnl=-3.0),  # Reduced
        pos("D", size=7.0, current_value=9.0, stake=6.0, open_pnl=3.0),   # New
    ]
    checkpoint = [
        ckpt(pos("A", size=10.0, current_value=6.0)),
        ckpt(pos("B", size=10.0, current_value=6.0)),
        ckpt(pos("C", size=3.0, current_value=10.0)),  # vanished -> Closed
    ]
    rows = compare(current, checkpoint)
    frame = rows_to_frame(rows)
    summary = summarize(rows)

    live = frame[frame["Size Status"] != Status.CLOSED.value]
    assert math.isclose(live["Stake"].sum(), summary.total_stake)
    assert math.isclose(live["Now"].sum(), summary.current_value)
    assert math.isclose(live["Since Entry"].sum(), summary.open_pnl)
    assert summary.open_positions == len(live)
    # The Closed row's Now must be missing so it can never leak into a sum.
    assert bool(frame[frame["Size Status"] == Status.CLOSED.value]["Now"].isna().all())


# ===========================================================================
# Realized / Since-Entry semantics for Reduced vs Closed
# ===========================================================================
def test_reduced_partial_cashout_realized_and_since_entry_render_colored():
    rows = compare(
        [pos("B", size=4.0, current_value=2.0, stake=5.0, open_pnl=-3.0, realized_pnl=7.5)],
        [ckpt(pos("B", size=10.0, current_value=6.0))],
    )
    assert rows[0].status is Status.REDUCED
    frame, html = _render(rows)
    # Realized gain (money banked on the sold portion) reads green;
    # the unrealized Since-Entry loss on the remainder reads red.
    assert "green" in _cell_color(html, frame, "Realized")
    assert "red" in _cell_color(html, frame, "Since Entry")


def test_closed_realized_and_since_entry_are_missing():
    rows = compare([], [ckpt(pos("A", realized_pnl=3.0, open_pnl=4.0))])
    frame, html = _render(rows)
    assert "—" in _cell_text(html, frame, "Realized")
    assert "—" in _cell_text(html, frame, "Since Entry")


# ===========================================================================
# Property 2: NaN never reaches a rendered cell as a number  (HOLDS)
# ===========================================================================
def test_nan_current_value_renders_as_missing_not_a_number():
    """A NaN in a money field (here forced onto a hand-built Position that skips
    the from_api boundary) must render em-dash, not '$nan', and never colored."""
    rows = compare([pos("A", current_value=float("nan"))], [ckpt(pos("A"))])
    frame, html = _render(rows)
    now = _cell_text(html, frame, "Now")
    assert "nan" not in now.lower()
    assert "—" in now


# ===========================================================================
# App-wiring end-to-end via the real app.py (DB-isolated AppTest)
# ===========================================================================
def _fresh_app(tmp_path, monkeypatch) -> AppTest:
    monkeypatch.setenv("POLYMARKET_TRACKER_DB", str(tmp_path / "app.db"))
    st.cache_resource.clear()
    at = AppTest.from_file(app_module.__file__, default_timeout=60)
    at.run()
    return at


def _txt(at, label):
    return next(t for t in at.text_input if t.label == label)


def _btn(at, label):
    return next(b for b in at.button if b.label == label)


def _load_fake(at) -> None:
    at.toggle[0].set_value(True)
    at.run()
    _txt(at, "Wallet address").set_value(WALLET)
    _btn(at, "Refresh").click()
    at.run()
    assert not at.exception


def test_app_fake_flow_renders_three_props_and_leaves_real_db_untouched(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch)
    _load_fake(at)
    frame = at.dataframe[0].value
    assert set(frame["Market"]) == {"Morocco wins", "0-0 first half", "France 2-1"}
    assert not REAL_DB.exists()


def test_app_cashout_flow_renders_closed_with_no_fabricated_loss(tmp_path, monkeypatch):
    at = _fresh_app(tmp_path, monkeypatch)
    _load_fake(at)  # loads before_match
    _txt(at, "Checkpoint label").set_value("Before match")
    _btn(at, "Save checkpoint").click()
    at.run()
    # switch to the cashout scenario and refresh
    next(s for s in at.selectbox if s.label == "Scenario").set_value("after_cashout")
    _btn(at, "Refresh").click()
    at.run()
    compare_box = next(s for s in at.selectbox if s.label == "Compare against")
    compare_box.set_value(next(o for o in compare_box.options if o.startswith("Before match")))
    at.run()
    assert not at.exception

    frame = at.dataframe[0].value
    raw = frame.set_index("Market")
    assert raw.loc["Morocco wins", "Size Status"] == Status.CLOSED.value
    # The app never saw the cashout proceeds: the Closed row's measured cells are
    # missing (NaN -> em-dash), never a fabricated -$10 market loss. Because every
    # money cell on this row is NaN, it is impossible for it to be painted red.
    assert math.isnan(raw.loc["Morocco wins", "Now"])
    assert math.isnan(raw.loc["Morocco wins", "Change Since Checkpoint"])
    assert math.isnan(raw.loc["Morocco wins", "Since Entry"])
    # Confirm at the styled-HTML level that Morocco's row carries no red anywhere.
    morocco_idx = list(frame["Market"]).index("Morocco wins")
    assert _row_has_no_red(style_frame(frame).to_html(), frame, morocco_idx)
    assert not REAL_DB.exists()


def test_app_wallet_change_marks_positions_stale_no_cross_wallet_mix(tmp_path, monkeypatch):
    """Positions cached for one wallet must not be compared against / rendered
    for a different wallet without an explicit refresh."""
    at = _fresh_app(tmp_path, monkeypatch)
    _load_fake(at)
    assert len(at.dataframe) == 1  # three props visible

    _txt(at, "Wallet address").set_value("0x" + "1" * 40)  # different wallet, no refresh
    at.run()
    assert not at.exception
    # Stale guard empties positions and shows the info banner instead of the
    # previous wallet's table.
    assert len(at.dataframe) == 0
    assert any("changed since the last fetch" in str(i.value) for i in at.info)
    assert not REAL_DB.exists()


# ===========================================================================
# BUGS (assert the CORRECT behavior -> xfail strict until closed)
# ===========================================================================
# FIXED, at both ends of the composition:
#  - models.Position.from_api screens the DERIVED open_pnl for finiteness (_f
#    only screened the raw fields, not values computed from them);
#  - ui treats any non-finite value as unmeasured, rendering "—" and never
#    colouring it, so an overflow can no longer fabricate a dollar figure.
def test_inf_change_from_finite_inputs_should_render_missing_not_inf():
    # Two finite, from_api- and db-acceptable values whose difference overflows.
    # change_since_checkpoint is now checkpoint_size * (price_now - price_then),
    # so the overflow is composed through the PRICES rather than the values --
    # the same finite-in / non-finite-out gap, on the field that now feeds the
    # column. (A real curPrice is a probability in [0,1]; these are hostile
    # inputs that models._f nonetheless accepts, being finite.)
    rows = compare(
        [pos("A", current_price=1.5e308)],
        [ckpt(pos("A", current_price=-1e308))],
    )
    assert not math.isfinite(rows[0].change_since_checkpoint)  # compose produced inf
    frame, html = _render(rows)
    cell = _cell_text(html, frame, "Change Since Checkpoint")
    # DESIRED: a non-finite value the user should never see as a number.
    assert "inf" not in cell.lower()
    assert _cell_color(html, frame, "Change Since Checkpoint") == ""


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: same Inf-composition gap reaches the summary card. summarize() sums "
        "open_pnl over live rows; a single position whose open_pnl overflowed to "
        "Inf makes render_summary print 'Open PnL: $inf'. Nothing between the finite "
        "boundary and the card re-checks finiteness."
    ),
)
def test_summary_open_pnl_should_stay_finite():
    rows = compare([pos("A", current_value=1e308, stake=-1e308, open_pnl=1e308 - (-1e308))], [])
    assert math.isfinite(summarize(rows).open_pnl)


# ===========================================================================
# Guard: the user's real database was never created by this suite
# ===========================================================================
def test_zzz_real_database_was_never_created():
    assert not REAL_DB.exists(), (
        f"A test wrote to the real DB at {REAL_DB}; every test must use a tmp path "
        "and set POLYMARKET_TRACKER_DB."
    )
