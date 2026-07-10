"""Local Polymarket prop tracker. Read-only. Never trades, never signs."""

from __future__ import annotations

import os
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

# Honors POLYMARKET_TRACKER_DB when set (used by the test suite to redirect
# at a throwaway tmp_path so tests never touch the user's real data/*.db),
# defaulting to the real on-disk location otherwise.
DB_PATH = Path(
    os.environ.get("POLYMARKET_TRACKER_DB")
    or (Path(__file__).parent / "data" / "polymarket_tracker.db")
)


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

    # Cached positions are only valid for the exact (wallet, use_fake,
    # scenario) combination they were fetched under. Editing the wallet
    # field, flipping the fake/real toggle, or switching the scenario is a
    # normal Streamlit rerun -- it does NOT refetch. Without this guard,
    # stale positions fetched for a previous wallet/source would silently be
    # compared against -- or saved into -- a checkpoint for whatever
    # wallet/source is currently selected (cross-wallet contamination).
    current_source_key = (wallet, use_fake, scenario)

    if refresh or "positions" not in st.session_state:
        if wallet or use_fake:
            positions = _load_positions(_source(use_fake, scenario), wallet)
            if positions is not None:
                st.session_state["positions"] = positions
                st.session_state["positions_source_key"] = current_source_key
                st.session_state["refreshed_at"] = datetime.now().strftime("%H:%M:%S")

    positions_stale = st.session_state.get("positions_source_key") != current_source_key
    positions: list[Position] = [] if positions_stale else st.session_state.get("positions", [])
    last_refreshed = "" if positions_stale else st.session_state.get("refreshed_at", "")

    if positions_stale and "positions" in st.session_state:
        st.info(
            "Wallet or data source changed since the last fetch. "
            "Click Refresh to load positions for the current selection."
        )

    if save_checkpoint:
        if not label:
            st.warning("Give the checkpoint a label first.")
        elif not wallet:
            st.warning("Enter a wallet first.")
        elif positions_stale:
            st.warning(
                "Positions are stale for the current wallet/source. "
                "Click Refresh before saving a checkpoint."
            )
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
    options = {
        f"{c['label']}  ({c['created_at']})": (c["id"], c["label"])
        for c in checkpoints
    }
    selected = st.selectbox("Compare against", ["(none)"] + list(options))

    checkpoint_rows: list[CheckpointRow] = []
    checkpoint_label = ""
    if selected != "(none)":
        checkpoint_id, checkpoint_label = options[selected]
        checkpoint_rows = db.load_checkpoint_positions(conn, checkpoint_id)

    rows = compare(positions, checkpoint_rows)
    render_summary(
        summarize(rows),
        checkpoint_label=checkpoint_label,
        last_refreshed=last_refreshed,
    )
    render_table(rows)


if __name__ == "__main__":
    main()
