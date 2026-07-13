"""Adversarial tests for ui.py's pure rendering functions: rows_to_frame and
style_frame.

Derived independently from models.py's Row/Status contract and general
first-principles expectations for a money-rendering table (no advice text,
None -> em dash, gains green / losses red, a cashout is not a market loss).
Does NOT read initial_plan.md, CLAUDE.md, docs/, .superpowers/, or the other
UI test files -- probes were designed from scratch against the live behavior
of ui.py, then locked in as assertions.

Every REAL bug found is captured as an `xfail(strict=True)` test: if a future
fix makes the assertion pass, pytest turns that into a hard failure (XPASS)
so the mark must be removed deliberately instead of silently going stale.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from models import Row, Status  # noqa: E402
import ui  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Column index within ui.COLUMNS, by name, so tests read by intent.
COL = {name: i for i, name in enumerate(ui.COLUMNS)}


def make_row(**overrides) -> Row:
    """A fully-populated, internally-consistent Open row. Override fields to
    build hostile variants."""
    base = dict(
        asset="asset-1",
        market_title="Morocco vs Spain",
        outcome="Morocco wins",
        status=Status.OPEN,
        stake=100.0,
        checkpoint_value=100.0,
        current_value=100.0,
        change_since_checkpoint=0.0,
        since_entry=0.0,
        realized_pnl=0.0,
        checkpoint_price=0.5,
        current_price=0.5,
        price_change=0.0,
        checkpoint_size=200.0,
        current_size=200.0,
        size_change=0.0,
        size_change_percent=0.0,
    )
    base.update(overrides)
    return Row(**base)


def render_html(rows: list[Row]) -> str:
    frame = ui.rows_to_frame(rows)
    styler = ui.style_frame(frame)
    return styler.to_html()


def cell_text(html: str, row: int, col: int) -> str:
    """Text of the <td> at (row, col), independent of the styler's random
    uuid prefix on cell ids."""
    m = re.search(rf'<td[^>]*id="[^"]*row{row}_col{col}"[^>]*>(.*?)</td>', html, re.S)
    assert m, f"no cell found at row {row} col {col}"
    return m.group(1)


def cell_style(html: str, row: int, col: int) -> str | None:
    """The CSS rule body applying to the <td> at (row, col), or None if the
    styler emitted no rule for that cell (i.e. unstyled)."""
    id_m = re.search(rf'id="([^"]*row{row}_col{col})"', html)
    assert id_m, f"no cell id found at row {row} col {col}"
    cell_id = id_m.group(1)
    block_m = re.search(r'<style type="text/css">(.*?)</style>', html, re.S)
    if not block_m:
        return None
    for rule_m in re.finditer(r"([^{}]+)\{([^{}]+)\}", block_m.group(1)):
        selectors, body = rule_m.group(1), rule_m.group(2)
        if f"#{cell_id}" in selectors:
            normalized = " ".join(body.split())
            return normalized.rstrip(";").strip()
    return None


# ---------------------------------------------------------------------------
# Properties that hold: None handling
# ---------------------------------------------------------------------------


def test_none_renders_as_em_dash_not_dollar_zero():
    """A None (never measured) must render as em dash, never $0.00 or literal
    'None'."""
    row = make_row(
        status=Status.CLOSED,
        current_value=None,
        change_since_checkpoint=None,
        since_entry=None,
        price_change=None,
        current_price=None,
    )
    html = render_html([row])
    assert cell_text(html, 0, COL["Now"]) == "—"
    assert cell_text(html, 0, COL["Change Since Checkpoint"]) == "—"
    assert "$0.00" not in cell_text(html, 0, COL["Now"])
    assert "None" not in html


def test_none_em_dash_holds_even_when_entire_column_is_none():
    """When every row leaves a column None, pandas may store it as an
    object-dtype column of Python ``None`` (not float NaN). na_rep must still
    catch it -- confirmed by inspecting actual codepoints, not eyeballing
    mojibake in a terminal."""
    row = make_row(
        stake=None,
        checkpoint_value=None,
        current_value=None,
        change_since_checkpoint=None,
        since_entry=None,
        realized_pnl=None,
        checkpoint_price=None,
        current_price=None,
        price_change=None,
        checkpoint_size=None,
        current_size=None,
    )
    frame = ui.rows_to_frame([row])
    assert frame["Realized"].dtype == object  # confirms the object-dtype path is hit
    html = render_html([row])
    for col_name in ["Stake", "Checkpoint Value", "Now", "Change Since Checkpoint",
                      "Since Entry", "Realized", "Checkpoint Price", "Current Price",
                      "Price Change", "Size at Checkpoint", "Current Size"]:
        text = cell_text(html, 0, COL[col_name])
        assert text == "—", f"{col_name} rendered {text!r} instead of em dash"
    assert "None" not in html
    assert ">nan<" not in html


def test_literal_nan_float_also_renders_as_em_dash_uncolored():
    """A bare float('nan') (not None) must be treated identically: em dash,
    no color -- pd.isna() catches NaN the same way it catches None."""
    row = make_row(realized_pnl=float("nan"))
    html = render_html([row])
    assert cell_text(html, 0, COL["Realized"]) == "—"
    assert cell_style(html, 0, COL["Realized"]) is None


# ---------------------------------------------------------------------------
# Properties that hold: coloring scope and zero handling
# ---------------------------------------------------------------------------


def test_zero_and_negative_zero_pnl_are_never_colored():
    row = make_row(change_since_checkpoint=0.0, since_entry=-0.0, realized_pnl=0.0)
    html = render_html([row])
    assert cell_style(html, 0, COL["Change Since Checkpoint"]) is None
    assert cell_style(html, 0, COL["Since Entry"]) is None
    assert cell_style(html, 0, COL["Realized"]) is None


def test_only_the_four_pnl_columns_are_ever_colored():
    """Stake, Checkpoint Value, Now, Checkpoint Price, Current Price, and the
    two size columns hold raw positions, not gains/losses -- they must never
    pick up green/red text even when a PNL column in the same row is
    colored."""
    row = make_row(
        change_since_checkpoint=50.0,
        since_entry=-25.0,
        stake=999.0,
        checkpoint_value=999.0,
        current_value=999.0,
        checkpoint_price=0.9,
        current_price=0.1,
        checkpoint_size=500.0,
        current_size=500.0,
    )
    html = render_html([row])
    non_pnl = ["Stake", "Checkpoint Value", "Now", "Checkpoint Price",
               "Current Price", "Size at Checkpoint", "Current Size"]
    for col_name in non_pnl:
        style = cell_style(html, 0, COL[col_name])
        assert style is None, f"{col_name} unexpectedly styled: {style}"
    # sanity: the PNL columns actually did get colored, proving the helper works
    assert cell_style(html, 0, COL["Change Since Checkpoint"]) == "color: green"
    assert cell_style(html, 0, COL["Since Entry"]) == "color: red"


def test_mixed_rows_do_not_bleed_color_across_rows_or_columns():
    """A Closed row (realized loss), an Open gain row, and a Reduced row
    (realized gain but unrealized dip) rendered together -- every color must
    land on its own cell only."""
    rows = [
        make_row(
            asset="closed", status=Status.CLOSED, current_value=None,
            change_since_checkpoint=None, since_entry=None, realized_pnl=-20.0,
            current_price=None, price_change=None, current_size=0.0,
        ),
        make_row(asset="gain", status=Status.OPEN, change_since_checkpoint=25.0,
                  since_entry=25.0, realized_pnl=0.0),
        make_row(asset="reduced", status=Status.REDUCED, change_since_checkpoint=-30.0,
                  since_entry=-30.0, realized_pnl=10.0, current_size=40.0),
    ]
    html = render_html(rows)

    # row 0 (Closed): only Realized is colored, and it's red
    assert cell_style(html, 0, COL["Realized"]) == "color: red"
    assert cell_style(html, 0, COL["Change Since Checkpoint"]) is None
    assert cell_style(html, 0, COL["Size Status"]) == "background-color: #e9ecef; color: #6c757d"

    # row 1 (Open gain): green on both unrealized columns, Realized untouched (0)
    assert cell_style(html, 1, COL["Change Since Checkpoint"]) == "color: green"
    assert cell_style(html, 1, COL["Since Entry"]) == "color: green"
    assert cell_style(html, 1, COL["Realized"]) is None

    # row 2 (Reduced): unrealized columns red, Realized green -- disambiguated
    # within the same row, and Size Status is yellow, not red
    assert cell_style(html, 2, COL["Change Since Checkpoint"]) == "color: red"
    assert cell_style(html, 2, COL["Since Entry"]) == "color: red"
    assert cell_style(html, 2, COL["Realized"]) == "color: green"
    assert cell_style(html, 2, COL["Size Status"]) == "background-color: #fff3cd"


def test_status_backgrounds_never_use_red_or_green():
    """Closed must never read as a market loss (red); Increased must never
    read as an implied gain (green) -- it's a size change, not a P&L
    verdict."""
    red_or_green = {"red", "green", "#dc3545", "#28a745", "#c0392b", "#27ae60"}
    for status in Status:
        style = ui._colour_status(status.value)
        lowered = style.lower()
        assert not any(c in lowered for c in red_or_green), (
            f"{status.value} background uses a win/loss color: {style}"
        )


def test_increased_status_not_visually_distinguished_from_new():
    """Increased (added more size) shares New's neutral blue, not a
    profit-implying green -- adding to a position isn't itself a gain."""
    assert ui._colour_status(Status.INCREASED.value) == ui._colour_status(Status.NEW.value)


def test_no_advice_text_in_rendered_output():
    """Numbers only. A battery of rows across every status must never emit
    counsel."""
    rows = [
        make_row(asset=f"a{s.value}", status=s, change_since_checkpoint=-40.0,
                  since_entry=-40.0, realized_pnl=15.0)
        for s in Status
    ]
    html = render_html(rows)
    banned = ["hold", "sell", "buy", "cash out", "cashout", "good bet",
              "bad bet", "should", "recommend"]
    lowered = html.lower()
    found = [w for w in banned if w in lowered]
    assert not found, f"advice language leaked into rendered table: {found}"


def test_extreme_magnitudes_do_not_crash_to_html():
    """Robustness: absurd but finite values must not raise."""
    row = make_row(stake=1e300, current_value=1e-300, change_since_checkpoint=1e15)
    html = render_html([row])
    assert "<table" in html


# ---------------------------------------------------------------------------
# REAL BUGS
# ---------------------------------------------------------------------------


# FIXED: ui._is_renderable_number treats any non-finite value as unmeasured, so
# an inf renders as an em dash and is never colored as a real gain/loss.
def test_infinite_pnl_is_not_fabricated_as_a_colored_dollar_figure():
    row = make_row(change_since_checkpoint=float("inf"))
    html = render_html([row])
    text = cell_text(html, 0, COL["Change Since Checkpoint"])
    style = cell_style(html, 0, COL["Change Since Checkpoint"])
    # Right behavior: treat non-finite like missing data -- no fabricated
    # number, no color implying a real gain.
    assert "inf" not in text.lower()
    assert style is None


@pytest.mark.xfail(strict=True, reason=(
    "BUG (Major): calculations.py computes size_change and "
    "size_change_percent onto every Row (the size delta and its ratio "
    "since checkpoint), but ui.COLUMNS never lists them and rows_to_frame "
    "drops both fields entirely. The percent-of-size-change the domain "
    "model computes never reaches the rendered table under any input -- "
    "not a formatting bug but a silent, total omission of a computed "
    "column."
))
def test_size_change_percent_is_rendered_somewhere_in_the_table():
    row = make_row(size_change=-120.0, size_change_percent=-0.6)
    frame = ui.rows_to_frame([row])
    assert "Size Change Percent" in frame.columns


# FIXED: _colour_pnl now colours off the *displayed* (2dp) value, so a cell that
# reads as zero is never painted as a loss.
def test_subcent_value_that_displays_as_zero_is_not_colored():
    row = make_row(change_since_checkpoint=-0.004)
    html = render_html([row])
    text = cell_text(html, 0, COL["Change Since Checkpoint"])
    style = cell_style(html, 0, COL["Change Since Checkpoint"])
    # Sign now leads the currency symbol, so a sub-cent negative reads "-$0.00".
    assert text in ("$0.00", "-$0.00")
    # If the displayed text is a zero, it must not carry a loss/gain color.
    assert style is None


# FIXED: ui.money() puts the sign before the currency symbol: -$50.00.
def test_negative_money_uses_conventional_sign_placement():
    row = make_row(change_since_checkpoint=-50.0)
    html = render_html([row])
    text = cell_text(html, 0, COL["Change Since Checkpoint"])
    assert text.startswith("-$"), f"expected '-$50.00'-style formatting, got {text!r}"
