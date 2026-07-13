"""Local Polymarket prop tracker. Read-only. Never trades, never signs."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import db
from calculations import compare, summarize
from fixtures import SCENARIOS, FixtureSource
from models import CheckpointRow, Position, PositionSource
from polymarket_client import InvalidWalletError, PolymarketError, PolymarketSource
from polymarket_us_client import PolymarketUSError, PolymarketUSSource
from ui import render_summary, render_table, rows_to_frame, style_frame

# Reads .env (gitignored) so the Polymarket US key/secret never live in code.
load_dotenv()

# Polymarket US accounts have no wallet address at all -- identity comes from the
# API key -- so their checkpoints are scoped by key id instead. The key ID is an
# identifier, not the secret; the secret is never stored.
ACCOUNT_US = "Polymarket US"
ACCOUNT_CRYPTO = "Polymarket (crypto)"
ACCOUNTS = [ACCOUNT_US, ACCOUNT_CRYPTO]

# Honors POLYMARKET_TRACKER_DB when set (used by the test suite to redirect
# at a throwaway tmp_path so tests never touch the user's real data/*.db),
# defaulting to the real on-disk location otherwise.
DB_PATH = Path(
    os.environ.get("POLYMARKET_TRACKER_DB")
    or (Path(__file__).parent / "data" / "polymarket_tracker.db")
)


# Prefix for checkpoints saved while in fake-data mode, so fixture snapshots can
# never appear in (or be compared against) a real wallet's history. A real wallet
# is 0x + 40 hex characters, so this prefix can never collide with one.
FAKE_CHECKPOINT_PREFIX = "fake:"


@st.cache_resource
def _connection() -> sqlite3.Connection:
    return db.init_db(DB_PATH)


def _source(use_fake: bool, scenario: str, account: str) -> PositionSource:
    if use_fake:
        return FixtureSource(scenario)
    if account == ACCOUNT_US:
        # Raises MissingCredentialsError (a PolymarketUSError) if .env is unset.
        return PolymarketUSSource()
    return PolymarketSource()


def _load_positions(
    use_fake: bool, scenario: str, account: str, wallet: str
) -> list[Position] | None:
    """Fetch, turning every expected failure into a readable banner.

    Constructing the source can itself fail (missing US credentials), so it is
    inside the try -- otherwise a blank .env would raise a traceback at startup.
    """
    try:
        source = _source(use_fake, scenario, account)
        return source.fetch(wallet)
    except (InvalidWalletError, PolymarketError, PolymarketUSError) as exc:
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
        # Default to whichever platform is actually configured: if a US key is
        # present in .env, that is the account the user has. Otherwise the
        # crypto platform, which needs no credentials at all.
        default_account = 0 if os.environ.get("POLYMARKET_US_KEY_ID") else 1
        account = (
            ""
            if use_fake
            else st.selectbox("Account", ACCOUNTS, index=default_account)
        )
        if account == ACCOUNT_US:
            st.caption(
                "Identified by your API key in .env -- no wallet address. "
                "Read-only: this app has no order-placing code."
            )

    wallet = st.text_input("Wallet address", value=settings.get("wallet_address", ""))
    if account == ACCOUNT_US:
        st.caption("Wallet address is not used for Polymarket US.")

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
    # Switching account type must invalidate cached positions too, or a US
    # portfolio could be compared against a crypto wallet's checkpoint.
    current_source_key = (wallet, use_fake, scenario, account)

    # Each source gets its own checkpoint namespace, so a snapshot from one can
    # never appear in -- or be compared against -- another's history:
    #   fake:<wallet>   fixture data (a confident, meaningless comparison if mixed)
    #   us:<key id>     Polymarket US has NO wallet; the key identifies the account
    #   <wallet>        the crypto platform, scoped by address
    # A real wallet is 0x + 40 hex, so neither prefix can ever collide with one.
    if use_fake:
        checkpoint_key = f"{FAKE_CHECKPOINT_PREFIX}{wallet}"
    elif account == ACCOUNT_US:
        checkpoint_key = f"us:{os.environ.get('POLYMARKET_US_KEY_ID', '')}"
    else:
        checkpoint_key = wallet

    # Manual refresh ONLY. Never fetch on mount: opening the app must not fire a
    # live API call before the user asks for one. A US account needs no wallet.
    can_fetch = use_fake or account == ACCOUNT_US or bool(wallet)
    if refresh and can_fetch:
        positions = _load_positions(use_fake, scenario, account, wallet)
        if positions is not None:
            st.session_state["positions"] = positions
            st.session_state["positions_source_key"] = current_source_key
            st.session_state["refreshed_at"] = datetime.now().strftime("%H:%M:%S")

    positions_stale = st.session_state.get("positions_source_key") != current_source_key
    positions: list[Position] = [] if positions_stale else st.session_state.get("positions", [])
    last_refreshed = "" if positions_stale else st.session_state.get("refreshed_at", "")

    # Fires for BOTH "fetched for a different wallet/source" (positions IS in
    # session_state, just under a stale key) and "never fetched this session"
    # (a fresh session with a wallet pre-filled from settings; positions is
    # absent entirely). The old `"positions" in st.session_state` gate on this
    # banner suppressed it in exactly the second case -- the returning-user
    # opening screen -- leaving that path with no warning at all right before
    # compare() below could turn every live bet into a phantom "Closed".
    if positions_stale:
        if "positions" in st.session_state:
            st.info(
                "Wallet or data source changed since the last fetch. "
                "Click Refresh to load positions for the current selection."
            )
        else:
            st.info(
                "No positions loaded yet. Click Refresh to load positions "
                "for the current wallet/source before comparing."
            )

    if save_checkpoint:
        clean_label = label.strip()
        if not clean_label:
            # `if not label` alone lets a whitespace-only label through, saving a
            # checkpoint that looks blank in the dropdown.
            st.warning("Give the checkpoint a label first.")
        elif not use_fake and account != ACCOUNT_US and not wallet:
            # Polymarket US has no wallet address -- the API key is the identity.
            st.warning("Enter a wallet first.")
        elif positions_stale:
            # Covers both "fetched for a different wallet/source" and "never
            # fetched at all" -- in either case there is nothing trustworthy to
            # snapshot for the current selection.
            st.warning(
                "Positions are stale or not loaded for the current "
                "wallet/source. Click Refresh before saving a checkpoint."
            )
        else:
            # ATOMIC: one call, one transaction. Creating the checkpoint and
            # saving its positions as two separate calls would commit the
            # checkpoint row first, so a failure storing the positions would
            # strand an empty phantom checkpoint in the dropdown -- one the
            # user was just told had failed to save.
            #
            # Can raise ValueError/TypeError (non-finite or mistyped numeric
            # field) or sqlite3.Error; surface either as a readable banner.
            try:
                db.save_checkpoint(conn, checkpoint_key, clean_label, positions)
            except (ValueError, TypeError, sqlite3.Error) as exc:
                st.error(f"Could not save checkpoint: {exc}")
            else:
                st.success(f"Saved checkpoint: {clean_label}")

    checkpoints = db.list_checkpoints(conn, checkpoint_key) if checkpoint_key else []
    # created_at has only second precision, so two checkpoints saved in the same
    # second with the same label produce the same display string -- and keying a
    # dict on it alone silently collapsed them, leaving one unreachable. Fall
    # back to the (unique) id only when a collision actually occurs, so the
    # common case keeps a clean label.
    options: dict[str, tuple[int, str]] = {}
    for c in checkpoints:
        key = f"{c['label']}  ({c['created_at']})"
        if key in options:
            key = f"{key}  #{c['id']}"
        options[key] = (c["id"], c["label"])
    selected = st.selectbox("Compare against", ["(none)"] + list(options))

    checkpoint_rows: list[CheckpointRow] = []
    checkpoint_label = ""
    if selected != "(none)":
        checkpoint_id, checkpoint_label = options[selected]
        # Don't even bother loading the checkpoint's positions when stale --
        # they can never be safely compared below, so there is nothing to do
        # with them.
        if not positions_stale:
            checkpoint_rows = db.load_checkpoint_positions(conn, checkpoint_id)

    # NEVER compare against positions we do not actually have. compare()
    # cannot distinguish "this position is genuinely gone" from "we have not
    # fetched anything yet" -- both look like an empty `current` list, and an
    # empty `current` makes every checkpoint row read as Closed. positions_stale
    # is True in exactly the cases where `positions` above was forced to []
    # for a reason OTHER than an honestly-empty wallet, so short-circuit here
    # rather than let compare() manufacture a table full of fake cashouts.
    rows = [] if positions_stale else compare(positions, checkpoint_rows)
    render_summary(
        summarize(rows),
        checkpoint_label=checkpoint_label,
        last_refreshed=last_refreshed,
    )
    if positions_stale:
        # Deliberately NOT render_table([]): that renders "No open positions
        # for this wallet.", which implies the wallet was checked and came
        # back empty. Nothing has been checked yet for this selection -- the
        # banner above is the only message the user should see. Render the
        # same empty table shape directly so there is still a table on
        # screen, without render_table's own (wrong-for-this-case) caption.
        st.dataframe(style_frame(rows_to_frame([])), width="stretch", hide_index=True)
    else:
        render_table(rows)


if __name__ == "__main__":
    main()
