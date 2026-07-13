"""Regression tests that pin behaviour left UNCONSTRAINED by the new ui.py /
app.py suite, discovered by mutation testing.

Each test here was written against a SURVIVING mutant: a deliberate one-line bug
in ui.py / app.py that the existing tests (tests/test_ui.py,
tests/test_acceptance.py, tests/adversarial/test_ui_hostile.py) did NOT catch.
Every test below FAILS on its mutant and PASSES on the real code, so it kills the
gap. None of the survivors is an actual bug in the shipped code -- the code is
correct, the tests just never pinned it -- so these are plain passing tests, not
xfail.

Mapping (mutant -> test):
  M7b  Closed row given a red BACKGROUND (#ff0000) -> test_closed_row_background_is_grey_never_a_red_hue
  M9   Reduced row painted Bootstrap-danger red     -> test_reduced_row_background_is_the_yellow_warning_hue
  M8a  COLUMNS reordered (self-referential test)     -> test_columns_match_a_literal_spec_not_the_module_constant
  A1   staleness guard disabled                      -> test_switching_source_without_refresh_marks_positions_stale
  A3   Save-checkpoint proceeds while stale          -> test_save_checkpoint_is_blocked_and_not_persisted_when_stale
  A2   POLYMARKET_TRACKER_DB env seam bypassed       -> test_db_writes_are_isolated_to_the_env_var_path
  A6   fetch guard 'wallet or use_fake' -> 'and'     -> test_fake_data_loads_with_an_empty_wallet
"""

from __future__ import annotations

import re
import sqlite3

import streamlit as st

from models import Row, Status
from ui import rows_to_frame, style_frame
from streamlit.testing.v1 import AppTest

import app as app_module

WALLET = "0x" + "0" * 40
OTHER_WALLET = "0x" + "1" * 40


# ---------------------------------------------------------------------------
# ui.py: status-colour hues (M7b, M9) and column spec (M8a)
# ---------------------------------------------------------------------------


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


def _status_cell_style(row: Row) -> str:
    """The CSS rule pandas attaches to the Size Status cell."""
    frame = rows_to_frame([row])
    html = style_frame(frame).to_html()
    col_idx = list(frame.columns).index("Size Status")
    m = re.search(rf'<td id="([^"]*row0_col{col_idx})"[^>]*>', html)
    if not m:
        return ""
    rule = re.search(rf"#{re.escape(m.group(1))}\s*\{{([^}}]*)\}}", html)
    return rule.group(1).strip().lower() if rule else ""


# A red/danger hue in ANY form -- the literal word or a hex close to pure red.
# The existing test only searched for the literal string "color: red", so a red
# BACKGROUND expressed as a hex (#ff0000, Bootstrap's #f8d7da) slipped through.
_RED_HUES = ("red", "#ff0000", "#f8d7da", "#dc3545", "#f00")


def test_closed_row_background_is_grey_never_a_red_hue():
    # A cashout is not a loss: a Closed row must be neutral/grey, never painted
    # with a loss colour of any kind. Kills M7b (Closed -> #ff0000 background).
    style = _status_cell_style(_row(status=Status.CLOSED, current_value=None))
    assert "#e9ecef" in style, f"Closed lost its grey background: {style!r}"
    for hue in _RED_HUES:
        assert hue not in style, f"Closed row carries a red hue {hue!r}: {style!r}"


def test_reduced_row_background_is_the_yellow_warning_hue():
    # CLAUDE.md: colour Reduced yellow/orange (not red). A partial cashout is not
    # a market loss. Kills M9 (Reduced -> Bootstrap danger red #f8d7da).
    style = _status_cell_style(_row(status=Status.REDUCED, current_size=4.0))
    assert "#fff3cd" in style, f"Reduced is not the yellow warning hue: {style!r}"
    for hue in _RED_HUES:
        assert hue not in style, f"Reduced row carries a red hue {hue!r}: {style!r}"


def test_columns_match_a_literal_spec_not_the_module_constant():
    # The shipped test asserts frame.columns == ui.COLUMNS, comparing the frame
    # against the very constant that built it -- reordering/renaming COLUMNS
    # passes it unchanged. Pin the order to a hard-coded spec instead. Kills M8a.
    expected = [
        "Market", "Outcome", "Stake", "Checkpoint Value", "Now",
        "Change Since Checkpoint", "Since Entry", "Realized",
        "Checkpoint Price", "Current Price", "Price Change",
        "Size at Checkpoint", "Current Size", "Size Status",
    ]
    frame = rows_to_frame([_row()])
    assert list(frame.columns) == expected


# ---------------------------------------------------------------------------
# app.py: wallet/source staleness + DB isolation (A1, A3, A2, A6)
# ---------------------------------------------------------------------------


def _fresh_app(tmp_path, monkeypatch) -> AppTest:
    monkeypatch.setenv("POLYMARKET_TRACKER_DB", str(tmp_path / "acc.db"))
    st.cache_resource.clear()
    at = AppTest.from_file(app_module.__file__, default_timeout=30)
    at.run()
    return at


def _box(at, label):
    return next(s for s in at.selectbox if s.label == label)


def _btn(at, label):
    return next(b for b in at.button if b.label == label)


def _txt(at, label):
    return next(t for t in at.text_input if t.label == label)


def _enable_fake_data_and_refresh(at, wallet=WALLET):
    at.toggle[0].set_value(True)
    at.run()
    if wallet is not None:
        _txt(at, "Wallet address").set_value(wallet)
    _btn(at, "Refresh").click()
    at.run()
    assert not at.exception


def test_switching_source_without_refresh_marks_positions_stale(tmp_path, monkeypatch):
    """Kills A1 (staleness guard hardwired off). Editing the scenario is a plain
    rerun that does NOT refetch; stale positions must NOT keep rendering as if
    fresh, and the stale banner must appear."""
    at = _fresh_app(tmp_path, monkeypatch)
    _enable_fake_data_and_refresh(at)  # before_match -> 3 rows
    assert len(at.dataframe) == 1 and len(at.dataframe[0].value) == 3

    # Switch scenario WITHOUT clicking Refresh.
    _box(at, "Scenario").set_value("after_goal")
    at.run()

    # Real code blanks the stale table and warns. The A1 mutant would keep the
    # 3 before_match rows on screen under the after_goal selection.
    assert len(at.dataframe) == 0, "stale positions were still rendered"
    assert any("changed since the last fetch" in i.value.lower() for i in at.info), \
        "stale-data banner was not shown"


def test_save_checkpoint_is_blocked_and_not_persisted_when_stale(tmp_path, monkeypatch):
    """Kills A3 (Save-checkpoint proceeds while stale). Changing the wallet
    without refreshing makes the cached positions stale; saving a checkpoint
    then must be refused, and nothing may be written under the NEW wallet."""
    at = _fresh_app(tmp_path, monkeypatch)
    _enable_fake_data_and_refresh(at)  # positions fetched for WALLET

    # Change the wallet WITHOUT refreshing -> cached positions are now stale.
    _txt(at, "Wallet address").set_value(OTHER_WALLET)
    at.run()

    _txt(at, "Checkpoint label").set_value("Sneaky")
    _btn(at, "Save checkpoint").click()
    at.run()
    assert not at.exception

    # It must be refused (warning) and, crucially, NOT persisted under the new
    # wallet -- otherwise WALLET's positions leak into OTHER_WALLET's history.
    assert any("stale" in w.value.lower() for w in at.warning), "no stale warning"
    compare_box = _box(at, "Compare against")
    assert not any("Sneaky" in o for o in compare_box.options), \
        "a checkpoint was saved under the new wallet from stale positions"


def test_db_writes_are_isolated_to_the_env_var_path(tmp_path, monkeypatch):
    """Kills A2 (POLYMARKET_TRACKER_DB seam bypassed / real path hardcoded).
    Proves writes land in the env-var-designated file. Asserts only on the
    tmp_path DB, never on the real data/ path, so it is safe on any machine."""
    db_path = tmp_path / "acc.db"
    at = _fresh_app(tmp_path, monkeypatch)
    _enable_fake_data_and_refresh(at)
    _txt(at, "Checkpoint label").set_value("Iso")
    _btn(at, "Save checkpoint").click()
    at.run()
    assert not at.exception

    assert db_path.exists(), "env-var DB path was ignored; nothing written there"
    con = sqlite3.connect(db_path)
    try:
        labels = [r[0] for r in con.execute("SELECT label FROM checkpoints")]
    finally:
        con.close()
    assert "Iso" in labels, "checkpoint did not land in the env-var DB file"


def test_fake_data_loads_with_an_empty_wallet(tmp_path, monkeypatch):
    """Kills A6 (fetch guard 'wallet or use_fake' -> 'and'). Fake data ignores
    the wallet, so enabling it with an EMPTY wallet must still load the 3 props;
    the 'and' mutant would fetch nothing."""
    at = _fresh_app(tmp_path, monkeypatch)
    at.toggle[0].set_value(True)  # fake data on, wallet left blank
    at.run()
    _btn(at, "Refresh").click()
    at.run()
    assert not at.exception

    assert len(at.dataframe) == 1, "no table rendered for fake data + empty wallet"
    assert len(at.dataframe[0].value) == 3
