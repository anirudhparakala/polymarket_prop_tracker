import math

from calculations import compare, summarize
from models import CheckpointRow, Position, Status


def _position(asset, size=10.0, price=0.5, stake=5.0, realized=0.0):
    """A *coherent* position: current_value is always size * price.

    The real API cannot report otherwise, and a fixture that violates it (value
    moving while price sits still) describes a market that cannot exist. That
    matters now that change_since_checkpoint is derived from price rather than
    value: an incoherent fixture would test arithmetic on impossible data.
    Express "the position is worth more" by moving the *price*.
    """
    value = size * price
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


def _checkpoint(asset, size=10.0, price=0.5, stake=5.0):
    return CheckpointRow.from_position(
        _position(asset, size=size, price=price, stake=stake)
    )


def _by_asset(rows):
    return {r.asset: r for r in rows}


# --- status derives from size, never from value ---------------------------


def test_same_size_is_open_even_when_value_collapses_to_zero():
    rows = _by_asset(
        compare([_position("A", size=10.0, price=0.0)], [_checkpoint("A", size=10.0)])
    )
    assert rows["A"].status is Status.OPEN
    assert rows["A"].current_value == 0.0


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
    resolved = _position("A", size=10.0, price=1.0)
    rows = _by_asset(compare([resolved], [_checkpoint("A", size=10.0, price=0.5)]))
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
    rows = _by_asset(compare([], [_checkpoint("A", price=1.0)]))
    row = rows["A"]
    assert row.current_value is None
    assert row.change_since_checkpoint is None
    assert row.current_price is None
    assert row.price_change is None
    assert row.checkpoint_value == 10.0  # what we did measure, we keep
    assert row.current_size == 0.0


def test_new_row_has_no_checkpoint_side():
    rows = _by_asset(compare([_position("A", price=0.7)], []))
    row = rows["A"]
    assert row.checkpoint_value is None
    assert row.change_since_checkpoint is None
    assert row.checkpoint_price is None
    assert row.current_value == 7.0


# --- arithmetic -----------------------------------------------------------
#
# change_since_checkpoint = checkpoint_size * (current_price - checkpoint_price)
#
# "How the market moved the position I marked at the checkpoint." Holding the
# size fixed at its checkpoint value is what makes the number immune to the
# user's own buying and selling.


def test_change_equals_the_value_delta_when_size_is_unchanged():
    # The ordinary case -- the user did not trade, only the market moved. Here
    # the price-based formula is algebraically identical to the old value delta,
    # so this reads exactly as it always did.
    rows = _by_asset(
        compare([_position("A", size=10.0, price=1.0)], [_checkpoint("A", size=10.0)])
    )
    row = rows["A"]
    assert math.isclose(row.change_since_checkpoint, 5.0)
    assert math.isclose(
        row.change_since_checkpoint, row.current_value - row.checkpoint_value
    )


def test_partial_cashout_at_a_flat_price_is_not_a_loss():
    # The bug this formula exists to kill: 10 shares @ $1.00 at the checkpoint,
    # the user sells 6 at the SAME $1.00. They banked $6 and the market never
    # moved. A value delta would report -$6.00, colored red, sorted first --
    # the user's own profit-taking rendered as their biggest loss.
    rows = _by_asset(
        compare(
            [_position("A", size=4.0, price=1.0)],
            [_checkpoint("A", size=10.0, price=1.0)],
        )
    )
    row = rows["A"]
    assert row.status is Status.REDUCED
    assert row.price_change == 0.0  # the market did not move...
    assert row.change_since_checkpoint == 0.0  # ...so nothing changed
    assert math.isclose(row.current_value - row.checkpoint_value, -6.0)  # the old lie


def test_top_up_at_a_flat_price_is_not_a_gain():
    # The mirror image: buying 20 more shares at a flat $1.00 is spending money,
    # not winning it. A value delta would fabricate a green +$20.00.
    rows = _by_asset(
        compare(
            [_position("A", size=30.0, price=1.0)],
            [_checkpoint("A", size=10.0, price=1.0)],
        )
    )
    row = rows["A"]
    assert row.status is Status.INCREASED
    assert row.change_since_checkpoint == 0.0
    assert math.isclose(row.current_value - row.checkpoint_value, 20.0)  # the old lie


def test_partial_cashout_after_a_real_price_move_reports_only_the_market_move():
    # 10 shares @ $0.50 at the checkpoint; the price doubled to $1.00 and the
    # user sold 6 into the rise. The market moved the 10 shares they marked by
    # 10 * (1.00 - 0.50) = +$5.00. Their sale is not part of that number.
    rows = _by_asset(
        compare(
            [_position("A", size=4.0, price=1.0)],
            [_checkpoint("A", size=10.0, price=0.5)],
        )
    )
    row = rows["A"]
    assert row.status is Status.REDUCED
    assert math.isclose(row.change_since_checkpoint, 5.0)
    # Selling cannot change the headline number: the same market move on the
    # same checkpointed position reports the same $5, whatever the user did.
    untouched = _by_asset(
        compare(
            [_position("A", size=10.0, price=1.0)],
            [_checkpoint("A", size=10.0, price=0.5)],
        )
    )
    assert math.isclose(
        row.change_since_checkpoint, untouched["A"].change_since_checkpoint
    )


def test_a_flat_price_cashout_does_not_sort_above_a_real_mover():
    # Sorting keys on abs(change), so a fabricated change does not just mislead,
    # it takes over the top of the table -- the one slot the product exists for.
    current = [_position("cashed", size=1.0, price=1.0), _position("mover", price=0.9)]
    checkpoint = [_checkpoint("cashed", size=100.0, price=1.0), _checkpoint("mover")]
    assert [r.asset for r in compare(current, checkpoint)] == ["mover", "cashed"]


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
    # 20 shares now worth $0.60 each = $12.00 against a $5.00 stake.
    rows = _by_asset(
        compare(
            [_position("A", size=20.0, price=0.6, stake=5.0)],
            [_checkpoint("A", size=20.0)],
        )
    )
    assert math.isclose(rows["A"].since_entry, 7.0)


# --- sorting --------------------------------------------------------------


def test_biggest_absolute_mover_sorts_first():
    # All three hold 10 shares marked at $0.50. Moves: -$5.00, +$3.00, +$1.00.
    current = [
        _position("small", price=0.6),
        _position("big", price=0.0),
        _position("mid", price=0.8),
    ]
    checkpoint = [_checkpoint("small"), _checkpoint("big"), _checkpoint("mid")]
    assert [r.asset for r in compare(current, checkpoint)] == ["big", "mid", "small"]


def test_closed_and_new_rows_sort_last():
    current = [_position("moved", price=0.9), _position("fresh")]
    checkpoint = [_checkpoint("moved"), _checkpoint("gone")]
    ordered = [r.asset for r in compare(current, checkpoint)]
    assert ordered[0] == "moved"
    assert set(ordered[1:]) == {"fresh", "gone"}


# --- summary --------------------------------------------------------------


def test_summary_excludes_closed_rows():
    rows = compare([_position("A", price=1.0, stake=5.0)], [_checkpoint("A"), _checkpoint("gone")])
    summary = summarize(rows)
    assert summary.open_positions == 1
    assert math.isclose(summary.total_stake, 5.0)
    assert math.isclose(summary.current_value, 10.0)
    assert math.isclose(summary.open_pnl, 5.0)


def test_summary_of_no_rows_is_all_zero():
    summary = summarize([])
    assert summary.open_positions == 0
    assert summary.total_stake == 0.0
