"""Rendering and styling. The table is the product; summary cards are secondary."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from models import Row, Status, Summary

COLUMNS = [
    "Market",
    "Outcome",
    "Stake",
    "Checkpoint Value",
    "Now",
    "Change Since Checkpoint",
    "Since Entry",
    "Realized",
    "Checkpoint Price",
    "Current Price",
    "Price Change",
    "Size at Checkpoint",
    "Current Size",
    "Size Status",
]

MONEY_COLUMNS = [
    "Stake",
    "Checkpoint Value",
    "Now",
    "Change Since Checkpoint",
    "Since Entry",
    "Realized",
]
PRICE_COLUMNS = ["Checkpoint Price", "Current Price", "Price Change"]
SIZE_COLUMNS = ["Size at Checkpoint", "Current Size"]

# Only these carry gain/loss meaning, so only these are colored green/red.
# Stake and Now are positions, not wins or losses. "Realized" IS a win/loss
# (money already banked or lost on a partial cashout), so it is colored too --
# a realized loss must read as a loss, not sit in plain black next to the
# colored unrealized figures.
PNL_COLUMNS = ["Change Since Checkpoint", "Since Entry", "Price Change", "Realized"]

STATUS_BACKGROUND = {
    Status.OPEN.value: "",
    Status.REDUCED.value: "background-color: #fff3cd",
    Status.CLOSED.value: "background-color: #e9ecef; color: #6c757d",
    Status.NEW.value: "background-color: #cfe2ff",
    Status.INCREASED.value: "background-color: #cfe2ff",
}


def rows_to_frame(rows: list[Row]) -> pd.DataFrame:
    """None becomes NaN, which the styler renders as an em-dash."""
    records = [
        {
            "Market": r.market_title,
            "Outcome": r.outcome,
            "Stake": r.stake,
            "Checkpoint Value": r.checkpoint_value,
            "Now": r.current_value,
            "Change Since Checkpoint": r.change_since_checkpoint,
            "Since Entry": r.since_entry,
            "Realized": r.realized_pnl,
            "Checkpoint Price": r.checkpoint_price,
            "Current Price": r.current_price,
            "Price Change": r.price_change,
            "Size at Checkpoint": r.checkpoint_size,
            "Current Size": r.current_size,
            "Size Status": r.status.value,
        }
        for r in rows
    ]
    return pd.DataFrame(records, columns=COLUMNS)


def _colour_pnl(value) -> str:
    if pd.isna(value) or value == 0:
        return ""
    return "color: green" if value > 0 else "color: red"


def _colour_status(value) -> str:
    return STATUS_BACKGROUND.get(value, "")


def style_frame(frame: pd.DataFrame):
    """pandas 3.0 removed Styler.applymap. `Styler.map` is the elementwise API."""
    styler = frame.style

    present = [c for c in PNL_COLUMNS if c in frame.columns]
    if present:
        styler = styler.map(_colour_pnl, subset=present)
    if "Size Status" in frame.columns:
        styler = styler.map(_colour_status, subset=["Size Status"])

    return styler.format(
        {
            **{c: "${:,.2f}" for c in MONEY_COLUMNS},
            **{c: "{:.4f}" for c in PRICE_COLUMNS},
            **{c: "{:,.2f}" for c in SIZE_COLUMNS},
        },
        na_rep="—",
    )


def render_summary(summary: Summary, checkpoint_label: str, last_refreshed: str) -> None:
    columns = st.columns(6)
    columns[0].metric("Open positions", summary.open_positions)
    columns[1].metric("Total stake", f"${summary.total_stake:,.2f}")
    columns[2].metric("Current value", f"${summary.current_value:,.2f}")
    columns[3].metric("Open PnL", f"${summary.open_pnl:,.2f}")
    columns[4].metric("Checkpoint", checkpoint_label or "—")
    columns[5].metric("Last refreshed", last_refreshed or "—")


def render_table(rows: list[Row]) -> None:
    if not rows:
        st.info("No open positions for this wallet.")
        return
    st.dataframe(style_frame(rows_to_frame(rows)), width="stretch", hide_index=True)
    if any(r.status is Status.CLOSED for r in rows):
        st.caption(
            "Closed rows show — because the app cannot see cashout proceeds. "
            "They are excluded from the totals above."
        )
