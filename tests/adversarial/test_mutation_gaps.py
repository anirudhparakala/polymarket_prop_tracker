"""Tests that close coverage holes found by hand mutation testing.

Each test here corresponds to a mutant that SURVIVED the original suite: a
deliberate bug that no existing test caught. Every test below PASSES against
the real, unmutated code (it encodes correct behavior the suite left
unconstrained) and FAILS against its specific mutant.

Provenance: see .superpowers/adversarial/test-integrity-findings.md for the
exact before/after of every mutant and the surviving-suite output.

All new tests are plain passing tests (not xfail): in every case the CURRENT
behavior is correct; it was simply untested. No production bug was found -- the
finding is that the safety net had holes.
"""

from __future__ import annotations

import sqlite3

import pytest
import requests

import db
import polymarket_client
from calculations import compare, sort_rows
from models import CheckpointRow, Position, Row, Status
from polymarket_client import MAX_OFFSET, PAGE_LIMIT, PolymarketError, PolymarketSource


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _position(asset, size=10.0, value=5.0, price=0.5, stake=5.0, realized=3.0):
    return Position(
        asset=asset,
        condition_id="0xcond",
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


def _row(status: Status, change):
    """A minimal Row for sort tests; only status + change matter to the key."""
    return Row(
        asset="x",
        market_title="m",
        outcome="Yes",
        status=status,
        stake=None,
        checkpoint_value=None,
        current_value=None,
        change_since_checkpoint=change,
        since_entry=None,
        realized_pnl=None,
        checkpoint_price=None,
        current_price=None,
        price_change=None,
        checkpoint_size=None,
        current_size=None,
        size_change=None,
        size_change_percent=None,
    )


# --------------------------------------------------------------------------- #
# calculations.py -- the None-vs-0.0 promise on closed/new rows
#
# The Row contract says "None means the app does not know". A closed position's
# open_pnl / realized_pnl were never re-measured; a new position has no
# checkpoint side. Reporting 0.0 there would invent a number. The existing
# suite only asserted None for SOME closed/new fields, leaving these unpinned.
# --------------------------------------------------------------------------- #
def test_closed_row_since_entry_is_none_not_zero():
    # Mutant C-none-since_entry: `... if current else None` -> `else 0.0`.
    rows = _by_asset(compare([], [_checkpoint("A", value=10.0)]))
    assert rows["A"].status is Status.CLOSED
    assert rows["A"].since_entry is None


def test_closed_row_realized_pnl_is_none_not_zero():
    # Mutant C-none-realized_pnl: `... if current else None` -> `else 0.0`.
    # The app never saw the closed position's realized pnl; it must not claim 0.
    rows = _by_asset(compare([], [_checkpoint("A", value=10.0)]))
    assert rows["A"].status is Status.CLOSED
    assert rows["A"].realized_pnl is None


def test_new_row_checkpoint_size_is_none_not_zero():
    # Mutant C-none-checkpoint_size: `... if checkpoint else None` -> `else 0.0`.
    # A brand-new position had no checkpoint, so its checkpoint size is unknown,
    # NOT zero shares.
    rows = _by_asset(compare([_position("A", size=7.0)], []))
    assert rows["A"].status is Status.NEW
    assert rows["A"].checkpoint_size is None


def test_new_and_closed_rows_have_none_size_change_not_zero():
    # Mutant C-none-size_change: `... if both else None` -> `else 0.0`.
    # size_change is a difference against a checkpoint that does not exist on
    # one side; 0.0 would falsely read as "size did not change".
    rows = _by_asset(
        compare([_position("NEW", size=7.0)], [_checkpoint("GONE", size=4.0)])
    )
    assert rows["NEW"].status is Status.NEW
    assert rows["NEW"].size_change is None
    assert rows["GONE"].status is Status.CLOSED
    assert rows["GONE"].size_change is None


# --------------------------------------------------------------------------- #
# calculations.py -- sort tiering: live rows always outrank closed/new
# --------------------------------------------------------------------------- #
def test_zero_change_live_row_still_sorts_above_a_closed_row():
    # Mutant C-sort-tier-none-zero: the "no change" tier key (1, 0.0) -> (0, 0.0).
    # A live (Open) row whose value happens not to have moved (change == 0.0)
    # must still rank ABOVE a Closed row (change is None). With the mutant they
    # tie and a stable sort would leave the Closed row wherever it started.
    closed = _row(Status.CLOSED, None)
    open_zero = _row(Status.OPEN, 0.0)
    ordered = sort_rows([closed, open_zero])  # closed deliberately first in input
    assert ordered.index(open_zero) < ordered.index(closed)


# --------------------------------------------------------------------------- #
# models.py -- field-mapping fidelity for fields the suite never asserted
# --------------------------------------------------------------------------- #
def _distinct_raw():
    """A raw row whose every value is unique, so a swapped key is detectable."""
    return {
        "asset": "ASSET_ID",
        "conditionId": "COND_ID",
        "title": "Some Market",
        "eventSlug": "some-event",
        "outcome": "No",
        "size": 12.0,
        "avgPrice": 0.3,
        "curPrice": 0.7,
        "initialValue": 3.6,
        "currentValue": 8.4,
        "cashPnl": 4.8,
        "percentPnl": 133.3,
        "realizedPnl": -1.5,
        "redeemable": False,
        "endDate": "2024-11-05",
    }


def test_from_api_maps_outcome_from_the_outcome_field():
    # Mutant M-outcome-key: outcome=_s(raw,"outcome") -> _s(raw,"title").
    p = Position.from_api(_distinct_raw())
    assert p.outcome == "No"
    assert p.outcome != p.market_title


def test_from_api_maps_condition_id_from_condition_id_not_asset():
    # Mutant M-conditionid-key: condition_id=_s(raw,"conditionId") -> _s(raw,"asset").
    # asset and conditionId are DIFFERENT identifiers; confusing them corrupts
    # the on-chain reference stored with every checkpoint.
    p = Position.from_api(_distinct_raw())
    assert p.condition_id == "COND_ID"
    assert p.condition_id != p.asset


def test_from_api_maps_percent_pnl_from_percent_pnl_field():
    # Mutant M-percentpnl-key: percent_pnl=_f(raw,"percentPnl") -> _f(raw,"realizedPnl").
    p = Position.from_api(_distinct_raw())
    assert p.percent_pnl == 133.3
    assert p.percent_pnl != p.realized_pnl


def test_from_api_maps_end_date_from_end_date_field():
    # Mutant M-enddate-key: end_date=_s(raw,"endDate") -> _s(raw,"eventSlug").
    p = Position.from_api(_distinct_raw())
    assert p.end_date == "2024-11-05"
    assert p.end_date != p.event_slug


# --------------------------------------------------------------------------- #
# polymarket_client.py
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = [] if payload is None else payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _wallet():
    return "0x" + "0" * 40


def test_page_limit_is_the_documented_api_maximum_of_500():
    # Mutants P-pagelimit-501 and P-pagelimit-100. The existing tests only ever
    # compare against the CONSTANT itself, so any value survives. The API caps
    # limit at 500; 501 would be rejected, 100 silently under-fetches.
    assert PAGE_LIMIT == 500


def test_http_500_is_reported_as_a_server_problem_not_a_generic_error():
    # Mutant P-500-ge-to-gt: `>= 500` -> `> 500`. Exactly-500 responses would
    # fall through to the generic "Unexpected response" branch instead of the
    # friendly, actionable "having trouble ... try again" message.
    session = _FakeSession([_FakeResponse(status_code=500)])
    with pytest.raises(PolymarketError, match="having trouble"):
        PolymarketSource(session=session).fetch(_wallet())


def test_pagination_never_requests_an_offset_beyond_the_api_cap():
    # Mutant P-offset-ge-to-gt: `offset >= MAX_OFFSET` -> `offset > MAX_OFFSET`.
    # Feed enough full pages to reach offset == MAX_OFFSET. The real code must
    # STOP at that page (offset never exceeds the API's 10000 cap); the mutant
    # would ask for offset 10500 and pop past the queued responses.
    full = [{"asset": "a", "size": 1.0}] * PAGE_LIMIT
    n_full_pages = MAX_OFFSET // PAGE_LIMIT + 1  # offsets 0,500,...,10000
    session = _FakeSession([_FakeResponse(full) for _ in range(n_full_pages)])
    PolymarketSource(session=session).fetch(_wallet())
    # Assert on the offset sequence, not the position count: fetch() now dedupes
    # by asset, so counting returned positions would collapse regardless of how
    # many pages ran. The offsets prove pagination stopped exactly at the cap.
    offsets = [c["params"]["offset"] for c in session.calls]
    assert len(offsets) == n_full_pages
    assert offsets[-1] == MAX_OFFSET
    assert max(offsets) <= MAX_OFFSET


# --------------------------------------------------------------------------- #
# db.py -- ordering and, above all, DURABILITY (real commit, cross-connection)
#
# Every existing db test reads back through the SAME connection that wrote, so
# uncommitted writes are visible and a missing conn.commit() goes unnoticed.
# These tests open a SECOND connection to the same file -- the only way to prove
# the data was actually committed to disk.
# --------------------------------------------------------------------------- #
WALLET_A = "0x" + "a" * 40


def _second_conn(path) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _position_row(asset, size=10.0):
    return Position.from_api(
        {
            "asset": asset,
            "conditionId": "0xc",
            "title": "Morocco wins",
            "eventSlug": "morocco-france",
            "outcome": "Yes",
            "size": size,
            "avgPrice": 0.5,
            "initialValue": 5.0,
            "currentValue": 5.0,
            "percentPnl": 0.0,
            "curPrice": 0.5,
            "realizedPnl": 0.0,
        }
    )


def test_saved_settings_are_committed_to_disk(tmp_path):
    # Mutant D-drop-commit-settings: a missing commit is invisible to the same
    # connection but lost to any other reader / after a crash.
    path = tmp_path / "s.db"
    writer = db.init_db(path)
    db.save_settings(writer, WALLET_A, 100.0)
    reader = _second_conn(path)
    row = reader.execute("SELECT wallet_address FROM settings WHERE id = 1").fetchone()
    writer.close()
    reader.close()
    assert row is not None and row["wallet_address"] == WALLET_A


def test_created_checkpoint_is_committed_to_disk(tmp_path):
    # Mutant D-drop-commit-checkpoint. Read the checkpoint from a fresh
    # connection with NO other committing call in between to isolate it.
    path = tmp_path / "c.db"
    writer = db.init_db(path)
    db.create_checkpoint(writer, WALLET_A, "Before match")
    reader = _second_conn(path)
    labels = [
        r["label"]
        for r in reader.execute(
            "SELECT label FROM checkpoints WHERE wallet_address = ?", (WALLET_A,)
        )
    ]
    writer.close()
    reader.close()
    assert labels == ["Before match"]


def test_saved_checkpoint_positions_are_committed_to_disk(tmp_path):
    # Mutant D-drop-commit-savepos: the trailing conn.commit() in
    # save_checkpoint_positions. Without it the batch is never persisted.
    path = tmp_path / "p.db"
    writer = db.init_db(path)
    cid = db.create_checkpoint(writer, WALLET_A, "cp")
    db.save_checkpoint_positions(writer, cid, [_position_row("AST1"), _position_row("AST2")])
    reader = _second_conn(path)
    count = reader.execute(
        "SELECT COUNT(*) FROM checkpoint_positions WHERE checkpoint_id = ?", (cid,)
    ).fetchone()[0]
    writer.close()
    reader.close()
    assert count == 2
