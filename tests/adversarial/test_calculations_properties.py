"""Adversarial property tests for calculations.compare / sort_rows / summarize.

Written by an INDEPENDENT tester. Correctness is derived from first principles
(the product's stated promises and basic financial/ordering invariants), NOT
from the repo's own plan/docs/tests.

Layout:
  * "PROVEN INVARIANTS" -- properties I tried hard to break and could not.
    These are plain passing tests / hypothesis properties.
  * "BUGS" -- each real defect is pinned with @pytest.mark.xfail(strict=True).
    The suite stays green today; the moment someone fixes the bug the test
    XPASSes loudly and forces the marker to be removed.

Run:
  .venv/Scripts/python.exe -m pytest tests/adversarial/test_calculations_properties.py -v --basetemp=.pytest_tmp/calc
"""

from __future__ import annotations

import itertools
import math
import os
import pathlib
import subprocess
import sys

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

# Defensive: guarantee the repo root is importable even if the runner's import
# mode does not surface it (the root conftest.py normally handles this).
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from calculations import compare, sort_rows, summarize  # noqa: E402
from models import CheckpointRow, Position, Row, Status  # noqa: E402

SIZE_REL_TOL = 1e-9  # must mirror calculations.SIZE_REL_TOL


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def make_position(
    asset: str = "x",
    *,
    size: float = 100.0,
    current_value: float = 50.0,
    current_price: float = 0.5,
    stake: float = 40.0,
    open_pnl: float = 10.0,
    realized_pnl: float = 0.0,
) -> Position:
    return Position(
        asset=asset,
        condition_id="c",
        market_title="t",
        event_slug="e",
        outcome="o",
        size=size,
        entry_price=0.4,
        current_price=current_price,
        stake=stake,
        current_value=current_value,
        open_pnl=open_pnl,
        percent_pnl=0.0,
        realized_pnl=realized_pnl,
        redeemable=False,
        end_date="",
    )


def make_checkpoint(
    asset: str = "x",
    *,
    size: float = 100.0,
    current_value: float = 50.0,
    current_price: float = 0.5,
    stake: float = 40.0,
    open_pnl: float = 10.0,
    realized_pnl: float = 0.0,
) -> CheckpointRow:
    return CheckpointRow(
        asset=asset,
        condition_id="c",
        market_title="t",
        event_slug="e",
        outcome="o",
        size=size,
        entry_price=0.4,
        current_price=current_price,
        stake=stake,
        current_value=current_value,
        open_pnl=open_pnl,
        percent_pnl=0.0,
        realized_pnl=realized_pnl,
    )


def make_row(asset: str, change: float | None, status: Status = Status.OPEN) -> Row:
    """Directly-constructed Row to feed sort_rows in isolation."""
    return Row(
        asset=asset,
        market_title="t",
        outcome="o",
        status=status,
        stake=1.0,
        checkpoint_value=1.0,
        current_value=1.0,
        change_since_checkpoint=change,
        since_entry=0.0,
        realized_pnl=0.0,
        checkpoint_price=0.5,
        current_price=0.5,
        price_change=0.0,
        checkpoint_size=1.0,
        current_size=1.0,
        size_change=0.0,
        size_change_percent=None,
    )


# --------------------------------------------------------------------------- #
# Hypothesis strategies
# --------------------------------------------------------------------------- #
ASSETS = st.sampled_from(["a", "b", "c", "d", "e", ""])  # "" is a degenerate but legal key

# Bounded finite floats: no NaN/inf, magnitudes small enough that sums stay exact
# enough for the equalities we assert on the "proven invariant" side.
FINITE = st.floats(
    allow_nan=False, allow_infinity=False, min_value=-1e9, max_value=1e9
)


@st.composite
def positions(draw) -> Position:
    return make_position(
        asset=draw(ASSETS),
        size=draw(FINITE),
        current_value=draw(FINITE),
        current_price=draw(FINITE),
        stake=draw(FINITE),
        open_pnl=draw(FINITE),
        realized_pnl=draw(FINITE),
    )


@st.composite
def checkpoints(draw) -> CheckpointRow:
    return make_checkpoint(
        asset=draw(ASSETS),
        size=draw(FINITE),
        current_value=draw(FINITE),
        current_price=draw(FINITE),
        stake=draw(FINITE),
        open_pnl=draw(FINITE),
        realized_pnl=draw(FINITE),
    )


CURRENT_LIST = st.lists(positions(), max_size=6)
CHECKPOINT_LIST = st.lists(checkpoints(), max_size=6)

HYP = settings(deadline=None, derandomize=True, max_examples=250)


# =========================================================================== #
# PROVEN INVARIANTS (these must PASS)
# =========================================================================== #
@HYP
@given(CURRENT_LIST, CHECKPOINT_LIST)
def test_output_assets_are_exactly_the_union(current, checkpoint):
    """No asset is dropped, none is invented, none is duplicated."""
    rows = compare(current, checkpoint)
    expected = {p.asset for p in current} | {c.asset for c in checkpoint}
    got = [r.asset for r in rows]
    assert set(got) == expected
    assert len(got) == len(set(got))  # one row per asset, no duplicates


@HYP
@given(CURRENT_LIST, CHECKPOINT_LIST)
def test_compare_is_deterministic_within_a_process(current, checkpoint):
    a = [r.asset for r in compare(current, checkpoint)]
    b = [r.asset for r in compare(current, checkpoint)]
    assert a == b


@HYP
@given(size_cur=FINITE, size_cp=FINITE)
def test_status_matches_size_direction(size_cur, size_cp):
    """Classification is consistent with the size relationship (no inversion)."""
    row = compare(
        [make_position("x", size=size_cur)],
        [make_checkpoint("x", size=size_cp)],
    )[0]
    if math.isclose(size_cur, size_cp, rel_tol=SIZE_REL_TOL):
        assert row.status is Status.OPEN
    elif size_cur < size_cp:
        assert row.status is Status.REDUCED  # shares went down
    else:
        assert row.status is Status.INCREASED  # shares went up


@HYP
@given(st.lists(positions(), max_size=6))
def test_identity_roundtrip_finite_is_open_and_zero(current):
    """Comparing positions against a checkpoint made from those same positions
    yields OPEN and zero change for finite inputs (the round-trip property)."""
    checkpoint = [CheckpointRow.from_position(p) for p in current]
    rows = compare(current, checkpoint)
    for r in rows:
        assert r.status is Status.OPEN
        assert r.change_since_checkpoint == 0.0
        assert r.size_change == 0.0
        assert r.price_change == 0.0


@HYP
@given(CURRENT_LIST, CHECKPOINT_LIST)
def test_core_promise_full_cashout_is_closed_and_excluded(current, checkpoint):
    """The product's core promise: a position that vanished from `current`
    (a manual cashout) is CLOSED, carries NO measured current value / change,
    and is EXCLUDED from the portfolio totals -- never reported as a loss."""
    cur_assets = {p.asset for p in current}
    rows = compare(current, checkpoint)
    closed = [r for r in rows if r.status is Status.CLOSED]
    for r in closed:
        assert r.asset not in cur_assets
        assert r.current_value is None
        assert r.change_since_checkpoint is None
        assert r.current_price is None
        assert r.since_entry is None
        assert r.realized_pnl is None
    # totals ignore closed rows entirely
    s = summarize(rows)
    assert s.open_positions == len([r for r in rows if r.status is not Status.CLOSED])


@HYP
@given(CURRENT_LIST, CHECKPOINT_LIST)
def test_summarize_equals_sum_of_visible_live_rows(current, checkpoint):
    """summarize does not double-count, drop, or corrupt via the `or 0.0`
    coercion: its totals equal the exact sum over the non-closed rows the
    user sees, computed in the same order."""
    rows = compare(current, checkpoint)
    live = [r for r in rows if r.status is not Status.CLOSED]
    s = summarize(rows)
    assert s.open_positions == len(live)
    assert s.total_stake == sum(r.stake or 0.0 for r in live)
    assert s.current_value == sum(r.current_value or 0.0 for r in live)
    assert s.open_pnl == sum(r.since_entry or 0.0 for r in live)


@HYP
@given(CURRENT_LIST, CHECKPOINT_LIST)
def test_sort_puts_no_change_rows_after_movers(current, checkpoint):
    """Closed/New rows (change_since_checkpoint is None) always sort after
    every live mover."""
    rows = compare(current, checkpoint)
    seen_none = False
    for r in rows:
        if r.change_since_checkpoint is None:
            seen_none = True
        else:
            assert not seen_none, "a mover appeared after a no-change row"


def test_empty_inputs_do_not_crash():
    assert compare([], []) == []
    assert summarize([]) .open_positions == 0
    # one side empty
    assert [r.status for r in compare([make_position("x")], [])] == [Status.NEW]
    assert [r.status for r in compare([], [make_checkpoint("x")])] == [Status.CLOSED]


# =========================================================================== #
# BUGS (each is a strict xfail: green today, XPASS the moment it is fixed)
# =========================================================================== #

# --- FIXED: sort_rows key is now total (asset breaks every tie) ------------- #
def test_sort_rows_ties_are_a_deterministic_function_of_data():
    a = [make_row("alpha", None, Status.CLOSED),
         make_row("bravo", None, Status.CLOSED),
         make_row("charlie", None, Status.CLOSED)]
    b = list(reversed(a))
    assert [r.asset for r in sort_rows(a)] == [r.asset for r in sort_rows(b)]


# --- BUG 1 (user-visible): compare() order depends on PYTHONHASHSEED --------- #
_CHILD = r"""
import sys
sys.path.insert(0, r"{root}")
from models import CheckpointRow
from calculations import compare
def cp(a):
    return CheckpointRow(asset=a, condition_id="c", market_title="t",
        event_slug="e", outcome="o", size=100.0, entry_price=0.4,
        current_price=0.5, stake=40.0, current_value=50.0, open_pnl=10.0,
        percent_pnl=0.0, realized_pnl=0.0)
rows = compare([], [cp(a) for a in ["alpha","bravo","charlie","delta","echo","foxtrot"]])
print(",".join(r.asset for r in rows))
"""


def _order_for_seed(seed: int) -> str:
    env = dict(os.environ, PYTHONHASHSEED=str(seed))
    out = subprocess.run(
        [sys.executable, "-c", _CHILD.format(root=str(REPO_ROOT))],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), check=True,
    )
    return out.stdout.strip()


# FIXED: compare() iterates the union sorted, and sort_rows' key is total.
def test_compare_row_order_is_independent_of_hash_seed():
    orders = {_order_for_seed(s) for s in range(8)}
    assert len(orders) == 1, f"row order varies across processes: {orders}"


# --- BUG 2: a NaN size is mis-classified as INCREASED ----------------------- #
@pytest.mark.xfail(
    strict=True,
    reason="BUG: math.isclose(NaN,x) is False and NaN<x is False, so a size "
    "that is NaN falls through to INCREASED -- the app claims the user bought "
    "more shares",
)
def test_nan_size_is_not_reported_as_increased():
    row = compare(
        [make_position("x", size=float("nan"))],
        [make_checkpoint("x", size=100.0)],
    )[0]
    assert row.status is not Status.INCREASED


@pytest.mark.xfail(
    strict=True,
    reason="BUG: identity round-trip fails for a NaN size -> classified "
    "INCREASED instead of OPEN",
)
@settings(deadline=None, derandomize=True, max_examples=50)
@given(size=st.floats(allow_nan=True, allow_infinity=True))
@example(size=float("nan"))
def test_identity_roundtrip_open_for_any_size(size):
    p = make_position("x", size=size)
    row = compare([p], [CheckpointRow.from_position(p)])[0]
    assert row.status is Status.OPEN


# --- FIXED: a non-finite change is sorted into the "no change" tier, never
# fed to abs(), so one bad-data row can no longer scramble the whole table.
# (models._f also now stops NaN at the API boundary; this is defence in depth.)
def test_sort_is_permutation_invariant_with_a_nan_change():
    nan = float("nan")
    base = [make_row("a", nan), make_row("b", 10.0),
            make_row("c", 30.0), make_row("d", 20.0)]
    outs = {tuple(r.asset for r in sort_rows(list(p)))
            for p in itertools.permutations(base)}
    assert len(outs) == 1, f"{len(outs)} distinct orderings from one row set: {outs}"


# --- BUG 4 (Minor): one NaN value poisons the entire portfolio total -------- #
@pytest.mark.xfail(
    strict=True,
    reason="BUG: summarize sums straight through NaN, so a single unmeasurable "
    "position makes the whole portfolio total read NaN on the dashboard",
)
def test_one_nan_value_does_not_poison_portfolio_total():
    rows = compare(
        [make_position("a", current_value=float("nan")),
         make_position("b", current_value=100.0)],
        [make_checkpoint("a"), make_checkpoint("b")],
    )
    assert math.isfinite(summarize(rows).current_value)


# =========================================================================== #
# FIXED: a PARTIAL cashout is no longer conflated with a market loss.
#
# This test previously pinned the bug as "documented semantics": a partial
# cashout at a flat market reported a big negative `change_since_checkpoint`
# and sorted to the TOP as the biggest mover, even though price_change was
# exactly 0 -- the same conflation the product forbids for FULL cashouts,
# committed on partial ones. change_since_checkpoint is now
# `checkpoint_size * (current_price - checkpoint_price)`: the market move on the
# position as it stood at the checkpoint, which the user's own trading cannot
# manufacture. The assertions are inverted and kept as a permanent guard.
# =========================================================================== #
def test_partial_cashout_at_a_flat_market_is_not_reported_as_a_loss():
    """User sells half of a 100-share position at a dead-flat $1.00: they banked
    $50 and the market never moved. The headline number must be 0, not -$50."""
    row = compare(
        [make_position("x", size=50.0, current_value=50.0, current_price=1.0)],
        [make_checkpoint("x", size=100.0, current_value=100.0, current_price=1.0)],
    )[0]
    assert row.status is Status.REDUCED
    assert row.price_change == 0.0            # market did not move...
    assert row.change_since_checkpoint == 0.0  # ...so the headline says nothing moved
    assert row.size_change == -50.0           # the trade is reported as a SIZE change


def test_a_flat_partial_cashout_no_longer_hijacks_the_top_of_the_table():
    """The fabricated -$50 used to be the largest abs(change) in the table, so it
    sorted above every genuine mover -- taking over the one slot the product
    exists to fill. A real $10 move must now outrank it."""
    rows = compare(
        [
            make_position("cashed", size=50.0, current_value=50.0, current_price=1.0),
            make_position("mover", size=100.0, current_value=60.0, current_price=0.6),
        ],
        [
            make_checkpoint("cashed", size=100.0, current_value=100.0, current_price=1.0),
            make_checkpoint("mover", size=100.0, current_value=50.0, current_price=0.5),
        ],
    )
    assert [r.asset for r in rows] == ["mover", "cashed"]
    assert math.isclose(rows[0].change_since_checkpoint, 10.0)


def test_a_flat_top_up_does_not_fabricate_a_gain():
    """The mirror image: buying 50 more shares at a flat $1.00 is spending money,
    not winning it. The value delta would show a green +$50."""
    row = compare(
        [make_position("x", size=150.0, current_value=150.0, current_price=1.0)],
        [make_checkpoint("x", size=100.0, current_value=100.0, current_price=1.0)],
    )[0]
    assert row.status is Status.INCREASED
    assert row.change_since_checkpoint == 0.0
    assert row.size_change == 50.0


@HYP
@given(sold=FINITE, price_now=FINITE)
def test_the_users_own_trading_cannot_move_the_headline_number(sold, price_now):
    """The defining property: for a fixed checkpoint and a fixed current price,
    change_since_checkpoint is the SAME no matter how many shares the user
    bought or sold in between. It measures the market, and only the market."""
    checkpoint = make_checkpoint("x", size=100.0, current_price=0.5)
    traded = compare(
        [make_position("x", size=sold, current_price=price_now)], [checkpoint]
    )[0]
    untouched = compare(
        [make_position("x", size=100.0, current_price=price_now)], [checkpoint]
    )[0]
    assert traded.change_since_checkpoint == untouched.change_since_checkpoint
