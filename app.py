"""Local Polymarket prop tracker. Read-only. Never trades, never signs."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import streamlit as st

import db
from calculations import compare, summarize
from fixtures import SCENARIOS, FixtureSource
from models import CheckpointRow, Position, PositionSource
from polymarket_client import InvalidWalletError, PolymarketError, PolymarketSource
from ui import render_summary, render_table

DB_PATH = Path(__file__).parent / "data" / "polymarket_tracker.db"


@st.cache_resource
def _connection() -> sqlite3.Connection:
    return db.init_db(DB_PATH)


def _source(use_fake: bool, scenario: str) -> PositionSource:
    return FixtureSource(scenario) if use_fake else PolymarketSource()


def _load_positions(source: PositionSource, wallet: str) -> list[Position] | None:
    try:
        return source.fetch(wallet)
    except InvalidWalletError as exc:
        st.error(str(exc))
    except PolymarketError as exc:
        st.error(str(exc))
    return None


def main() -> None:
    st.set_page_config(page_title="Polymarket Prop Tracker", layout="wide")
    st.title("Polymarket Prop Tracker")

    conn = _connection()
    settings = db.load_settings(conn) or {}

    with st.sidebar:
        st.header("Data source")
        use_fake = st.toggle("Use fake data", value=False)
        scenario = st.selectbox("Scenario", SCENARIOS) if use_fake else ""

    wallet = st.text_input("Wallet address", value=settings.get("wallet_address", ""))

    controls = st.columns(4)
    if controls[0].button("Save settings") and wallet:
        # save_settings can raise ValueError/TypeError on non-finite or
        # mistyped data (see db._require_finite) and sqlite3.Error on a
        # constraint problem. Surface a readable banner, not a traceback.
        try:
            db.save_settings(conn, wallet)
        except (ValueError, TypeError, sqlite3.Error) as exc:
            st.error(f"Could not save settings: {exc}")
        else:
            st.success("Saved.")

    refresh = controls[1].button("Refresh", type="primary")
    label = controls[2].text_input("Checkpoint label", placeholder="Before match")
    save_checkpoint = controls[3].button("Save checkpoint")

    if refresh or "positions" not in st.session_state:
        if wallet or use_fake:
            positions = _load_positions(_source(use_fake, scenario), wallet)
            if positions is not None:
                st.session_state["positions"] = positions
                st.session_state["refreshed_at"] = datetime.now().strftime("%H:%M:%S")

    positions: list[Position] = st.session_state.get("positions", [])

    if save_checkpoint:
        if not label:
            st.warning("Give the checkpoint a label first.")
        elif not wallet:
            st.warning("Enter a wallet first.")
        else:
            # create_checkpoint / save_checkpoint_positions can raise
            # ValueError/TypeError (non-finite or mistyped numeric field) or
            # sqlite3.Error (e.g. a rolled-back constraint violation).
            try:
                checkpoint_id = db.create_checkpoint(conn, wallet, label)
                db.save_checkpoint_positions(conn, checkpoint_id, positions)
            except (ValueError, TypeError, sqlite3.Error) as exc:
                st.error(f"Could not save checkpoint: {exc}")
            else:
                st.success(f"Saved checkpoint: {label}")

    checkpoints = db.list_checkpoints(conn, wallet) if wallet else []
    options = {f"{c['label']}  ({c['created_at']})": c["id"] for c in checkpoints}
    selected = st.selectbox("Compare against", ["(none)"] + list(options))

    checkpoint_rows: list[CheckpointRow] = []
    if selected != "(none)":
        checkpoint_rows = db.load_checkpoint_positions(conn, options[selected])

    rows = compare(positions, checkpoint_rows)
    render_summary(
        summarize(rows),
        checkpoint_label="" if selected == "(none)" else selected,
        last_refreshed=st.session_state.get("refreshed_at", ""),
    )
    render_table(rows)


if __name__ == "__main__":
    main()
