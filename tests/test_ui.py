import pandas as pd

from calculations import compare
from models import CheckpointRow, Position, Status
from ui import COLUMNS, rows_to_frame, style_frame


def _position(asset, size=10.0, value=5.0):
    return Position(
        asset=asset, condition_id="0xc", market_title=f"M{asset}", event_slug="e",
        outcome="Yes", size=size, entry_price=0.5, current_price=0.5, stake=5.0,
        current_value=value, open_pnl=value - 5.0, percent_pnl=0.0,
        realized_pnl=0.0, redeemable=False, end_date="2026-07-09",
    )


def test_frame_has_the_documented_columns_in_order():
    frame = rows_to_frame(compare([_position("A")], []))
    assert list(frame.columns) == COLUMNS


def test_closed_row_renders_now_and_change_as_missing_not_zero():
    rows = compare([], [CheckpointRow.from_position(_position("A", value=10.0))])
    frame = rows_to_frame(rows)
    assert pd.isna(frame.loc[0, "Now"])
    assert pd.isna(frame.loc[0, "Change Since Checkpoint"])
    assert frame.loc[0, "Checkpoint Value"] == 10.0
    assert frame.loc[0, "Size Status"] == Status.CLOSED.value


def test_style_frame_colors_gains_green_and_losses_red():
    checkpoint = [CheckpointRow.from_position(_position("A"))]
    # A gain (5.0 -> 10.0) must be green; a loss (5.0 -> 2.0) must be red.
    # Both halves are asserted so a regression in either branch is caught.
    gain_html = style_frame(
        rows_to_frame(compare([_position("A", value=10.0)], checkpoint))
    ).to_html()
    assert "green" in gain_html
    assert "color: red" not in gain_html

    loss_html = style_frame(
        rows_to_frame(compare([_position("A", value=2.0)], checkpoint))
    ).to_html()
    assert "color: red" in loss_html


def test_style_frame_renders_missing_values_as_an_em_dash():
    rows = compare([], [CheckpointRow.from_position(_position("A"))])
    html = style_frame(rows_to_frame(rows)).to_html()
    assert "—" in html


def test_style_frame_never_colors_a_closed_row_red():
    # A cashout must not read as a market loss.
    rows = compare([], [CheckpointRow.from_position(_position("A", value=10.0))])
    html = style_frame(rows_to_frame(rows)).to_html()
    assert "color: red" not in html


def test_style_frame_does_not_use_the_removed_applymap_api():
    # pandas 3.0 removed Styler.applymap. This guards against a regression that
    # would only surface at runtime.
    frame = rows_to_frame(compare([_position("A")], []))
    styler = frame.style
    assert not hasattr(styler, "applymap")
    assert hasattr(styler, "map")
    style_frame(frame).to_html()  # must not raise


def test_empty_rows_produce_an_empty_frame_with_columns():
    frame = rows_to_frame([])
    assert frame.empty
    assert list(frame.columns) == COLUMNS
