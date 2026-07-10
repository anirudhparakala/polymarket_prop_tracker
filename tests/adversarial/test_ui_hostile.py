"""Adversarial probes of ui.py's rendering, hunting for visual lies about money.

Written during the Phase 2/3 adversarial pass (the independent agent was cut
short by a session limit; these encode the properties that pass actually
checked). Every test drives the pure rows_to_frame / style_frame functions with
hostile Row values and inspects the rendered HTML.
"""

import re

import pytest

from models import Row, Status
from ui import rows_to_frame, style_frame


def _row(**kw) -> Row:
    base = dict(
        asset="A", market_title="M", outcome="Yes", status=Status.OPEN,
        stake=5.0, checkpoint_value=5.0, current_value=5.0,
        change_since_checkpoint=0.0, since_entry=0.0, realized_pnl=0.0,
        checkpoint_price=0.5, current_price=0.5, price_change=0.0,
        checkpoint_size=10.0, current_size=10.0, size_change=0.0,
        size_change_percent=0.0,
    )
    base.update(kw)
    return Row(**base)


def _cell_html(row: Row, column: str) -> str:
    """The rendered <td> text for one cell (its displayed value)."""
    frame = rows_to_frame([row])
    tds = re.findall(r"<td[^>]*>.*?</td>", style_frame(frame).to_html())
    return tds[list(frame.columns).index(column)]


def _cell_color(row: Row, column: str) -> str:
    """The CSS applied to one cell. pandas puts colors in a <style> block keyed
    by cell id, not inline on the <td> -- so resolve id -> style rule."""
    frame = rows_to_frame([row])
    html = style_frame(frame).to_html()
    col_idx = list(frame.columns).index(column)
    m = re.search(rf'<td id="([^"]*row0_col{col_idx})"[^>]*>', html)
    if not m:
        return ""
    rule = re.search(rf"#{re.escape(m.group(1))}\s*\{{([^}}]*)\}}", html)
    return rule.group(1).strip() if rule else ""


# --- the core promise: a value the app never measured is "—", never $0.00 ----


def test_closed_now_is_em_dash_not_zero_dollars():
    cell = _cell_html(_row(status=Status.CLOSED, current_value=None), "Now")
    assert "—" in cell
    assert "$0.00" not in cell


def test_closed_change_is_em_dash_not_zero():
    cell = _cell_html(
        _row(status=Status.CLOSED, current_value=None, change_since_checkpoint=None),
        "Change Since Checkpoint",
    )
    assert "—" in cell
    assert "$0.00" not in cell


# --- a missing / non-finite value must never be painted as a loss -----------


def test_nan_change_is_not_colored_red():
    color = _cell_color(_row(change_since_checkpoint=float("nan")), "Change Since Checkpoint")
    assert "red" not in color


def test_negative_zero_change_is_neutral_not_red():
    color = _cell_color(_row(change_since_checkpoint=-0.0), "Change Since Checkpoint")
    assert "red" not in color


def test_a_real_negative_change_is_red_and_a_gain_is_green():
    assert "red" in _cell_color(_row(change_since_checkpoint=-3.0), "Change Since Checkpoint")
    assert "green" in _cell_color(_row(change_since_checkpoint=3.0), "Change Since Checkpoint")


# --- realized pnl is a win/loss and must be colored -------------------------


def test_realized_loss_is_red_and_realized_gain_is_green():
    assert "red" in _cell_color(_row(realized_pnl=-50.0), "Realized")
    assert "green" in _cell_color(_row(realized_pnl=50.0), "Realized")


# --- stake and Now are positions, not wins/losses; they must NOT be colored --


def test_stake_and_now_are_never_colored():
    row = _row(stake=5.0, current_value=9999.0)
    assert _cell_color(row, "Stake") == ""
    assert _cell_color(row, "Now") == ""


# --- no crash / no advice text ----------------------------------------------


def test_hostile_magnitudes_do_not_crash_rendering():
    for row in (
        _row(current_value=1e15, stake=1e-9),
        _row(change_since_checkpoint=-1e-12),
        _row(current_size=1e12),
    ):
        style_frame(rows_to_frame([row])).to_html()  # must not raise


def test_rendered_table_contains_no_advice_text():
    html = style_frame(rows_to_frame([_row(status=Status.CLOSED, current_value=None)])).to_html()
    lowered = html.lower()
    for word in ("hold", "sell now", "buy now", "cash out", "good bet", "bad bet"):
        assert word not in lowered
