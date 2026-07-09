import math

from calculations import compare, summarize
from models import CheckpointRow, Position, Status


def _position(asset, size=10.0, value=5.0, price=0.5, stake=5.0, realized=0.0):
    return Position(
        asset=asset,
        condition_id="0xc",
        market_title=f"Market {asset}",
        event_slug="evt",
        outcome="Yes",
        size=size,
        entry_price=0.5,
        current_price=price,
        stake=stake,
        current_value=value,
        open_pnl=value - stake,
        percent_pnl=0.0,
        realized_pnl=realized,
        redeemable=False,
        end_date="2026-07-09",
    )


def _checkpoint(asset, size=10.0, value=5.0, price=0.5, stake=5.0):
    return CheckpointRow.from_position(
        _position(asset, size=size, value=value, price=price, stake=stake)
    )


def _by_asset(rows):
    return {r.asset: r for r in rows}


# --- status derives from size, never from value ---------------------------


def test_same_size_is_open_even_when_value_collapses_to_zero():
    rows = _by_asset(
        compare([_position("A", size=10.0, value=0.0)], [_checkpoint("A", size=10.0)])
    )
    assert rows["A"].status is Status.OPEN


def test_smaller_size_is_reduced():
    rows = _by_asset(
        compare([_position("A", size=4.0)], [_checkpoint("A", size=10.0)])
    )
    assert rows["A"].status is Status.REDUCED


def test_larger_size_is_increased():
    rows = _by_asset(
        compare([_position("A", size=12.0)], [_checkpoint("A", size=10.0)])
    )
    assert rows["A"].status is Status.INCREASED


def test_absent_now_is_closed():
    rows = _by_asset(compare([], [_checkpoint("A")]))
    assert rows["A"].status is Status.CLOSED


def test_absent_at_checkpoint_is_new():
    rows = _by_asset(compare([_position("A")], []))
    assert rows["A"].status is Status.NEW


def test_a_resolved_market_is_open_not_closed():
    # A resolved-but-unredeemed market still appears in /positions with price
    # 1.0 or 0.0 and redeemable=True. That is a real market outcome, not a
    # cashout, and it must not be labelled Closed.
    resolved = _position("A", size=10.0, value=10.0, price=1.0)
    rows = _by_asset(compare([resolved], [_checkpoint("A", size=10.0, value=5.0)]))
    assert rows["A"].status is Status.OPEN
    assert math.isclose(rows["A"].change_since_checkpoint, 5.0)


def test_float_noise_does_not_flip_open_to_reduced():
    # Exact == would report Reduced here.
    rows = _by_asset(
        compare(
            [_position("A", size=90548.087076)],
            [_checkpoint("A", size=90548.08707600001)],
        )
    )
    assert rows["A"].status is Status.OPEN


# --- the union, not just current rows -------------------------------------


def test_compare_iterates_the_union_of_both_sides():
    rows = _by_asset(compare([_position("B")], [_checkpoint("A")]))
    assert set(rows) == {"A", "B"}
    assert rows["A"].status is Status.CLOSED
    assert rows["B"].status is Status.NEW


# --- closed rows never assert numbers the app did not measure -------------


def test_closed_row_reports_no_current_value_and_no_change():
    rows = _by_asset(compare([], [_checkpoint("A", value=10.0)]))
    row = rows["A"]
    assert row.current_value is None
    assert row.change_since_checkpoint is None
    assert row.current_price is None
    assert row.price_change is None
    assert row.checkpoint_value == 10.0  # what we did measure, we keep
    assert row.current_size == 0.0


def test_new_row_has_no_checkpoint_side():
    rows = _by_asset(compare([_position("A", value=7.0)], []))
    row = rows["A"]
    assert row.checkpoint_value is None
    assert row.change_since_checkpoint is None
    assert row.checkpoint_price is None
    assert row.current_value == 7.0


# --- arithmetic -----------------------------------------------------------


def test_change_is_current_value_minus_checkpoint_value():
    rows = _by_asset(
        compare([_position("A", value=10.0)], [_checkpoint("A", value=5.0)])
    )
    assert math.isclose(rows["A"].change_since_checkpoint, 5.0)


def test_price_change_and_size_change_are_computed():
    rows = _by_asset(
        compare(
            [_position("A", size=4.0, price=0.9)],
            [_checkpoint("A", size=10.0, price=0.5)],
        )
    )
    row = rows["A"]
    assert math.isclose(row.price_change, 0.4)
    assert math.isclose(row.size_change, -6.0)
    assert math.isclose(row.size_change_percent, -0.6)


def test_size_change_percent_is_none_when_checkpoint_size_is_zero():
    rows = _by_asset(
        compare([_position("A", size=5.0)], [_checkpoint("A", size=0.0)])
    )
    assert rows["A"].size_change_percent is None


def test_since_entry_is_the_positions_open_pnl():
    rows = _by_asset(
        compare([_position("A", value=12.0, stake=5.0)], [_checkpoint("A")])
    )
    assert math.isclose(rows["A"].since_entry, 7.0)


# --- sorting --------------------------------------------------------------


def test_biggest_absolute_mover_sorts_first():
    current = [
        _position("small", value=6.0),
        _position("big", value=0.0),
        _position("mid", value=8.0),
    ]
    checkpoint = [_checkpoint("small"), _checkpoint("big"), _checkpoint("mid")]
    assert [r.asset for r in compare(current, checkpoint)] == ["big", "mid", "small"]


def test_closed_and_new_rows_sort_last():
    current = [_position("moved", value=9.0), _position("fresh")]
    checkpoint = [_checkpoint("moved"), _checkpoint("gone")]
    ordered = [r.asset for r in compare(current, checkpoint)]
    assert ordered[0] == "moved"
    assert set(ordered[1:]) == {"fresh", "gone"}


# --- summary --------------------------------------------------------------


def test_summary_excludes_closed_rows():
    rows = compare([_position("A", value=10.0, stake=5.0)], [_checkpoint("A"), _checkpoint("gone")])
    summary = summarize(rows)
    assert summary.open_positions == 1
    assert math.isclose(summary.total_stake, 5.0)
    assert math.isclose(summary.current_value, 10.0)
    assert math.isclose(summary.open_pnl, 5.0)


def test_summary_of_no_rows_is_all_zero():
    summary = summarize([])
    assert summary.open_positions == 0
    assert summary.total_stake == 0.0
