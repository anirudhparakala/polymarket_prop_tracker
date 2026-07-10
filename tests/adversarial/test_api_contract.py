"""Adversarial audit: does polymarket_client.py / models.py match the REAL,
LIVE Polymarket Data API, not just this repo's own account of it?

Ground truth for every assertion in this file comes from sources OUTSIDE this
repository:

  [DOC] https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user
        (fetched 2026-07-09). Documents, for `GET /positions`:
          query params -- user (required, Address), market (Array[Hash64]),
          eventId (Array[integer], >=1), sizeThreshold (number, default 1,
          >=0), redeemable (boolean, default false), mergeable (boolean,
          default false), limit (integer, default 100, range 0-500),
          offset (integer, default 0, range 0-10000), sortBy (enum, default
          TOKENS; options CURRENT/INITIAL/TOKENS/CASHPNL/PERCENTPNL/TITLE/
          RESOLVING/PRICE/AVGPRICE -- no id/timestamp/asset option),
          sortDirection (enum, default DESC), title (string, max 100).
          response -- bare JSON array of Position objects with fields
          proxyWallet, asset, conditionId, size, avgPrice, initialValue,
          currentValue, cashPnl, percentPnl, totalBought, realizedPnl,
          percentRealizedPnl, curPrice, redeemable, mergeable, title, slug,
          icon, eventSlug, outcome, outcomeIndex, oppositeOutcome,
          oppositeAsset, endDate, negativeRisk.
          documented error codes -- 400, 401, 500.

  [LIVE] Actually-executed, read-only GET requests made directly against
        https://data-api.polymarket.com during this audit (2026-07-09),
        using only the zero address (0x0000...0000) and wallet addresses
        observed as the public `proxyWallet` field in the public,
        unauthenticated /trades feed (never a private or user-supplied
        wallet). Raw responses/command output are pasted verbatim in the
        comments next to the tests that rely on them, so every number here
        is reproducible evidence, not speculation. See
        .superpowers/adversarial/api-contract-findings.md for the full
        transcript of every probe.

No test in this file makes a live network call -- all HTTP interaction is
stubbed via FakeSession/FakeResponse below, exactly like
tests/test_polymarket_client.py, so the suite is offline and deterministic.

Run:
  .venv/Scripts/python.exe -m pytest tests/adversarial/test_api_contract.py -v --basetemp=.pytest_tmp/api
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from models import Position
from polymarket_client import (
    BASE_URL,
    MAX_OFFSET,
    PAGE_LIMIT,
    POSITIONS_PATH,
    PolymarketError,
    PolymarketSource,
    validate_wallet,
)

WALLET = "0x" + "0" * 40


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else []
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    """Records every request; replays a queue of responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _row(asset: str, **overrides) -> dict:
    row = {
        "asset": asset,
        "conditionId": "0xc",
        "title": "Will Morocco win?",
        "eventSlug": "morocco-france",
        "outcome": "Yes",
        "size": 10.0,
        "avgPrice": 0.5,
        "initialValue": 5.0,
        "currentValue": 5.0,
        "cashPnl": 0.0,
        "percentPnl": 0.0,
        "totalBought": 10.0,
        "realizedPnl": 0.0,
        "curPrice": 0.5,
        "redeemable": False,
        "mergeable": False,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Real, verbatim rows captured live during this audit (2026-07-09) via:
#   curl -s "https://data-api.polymarket.com/positions?user=0xdddddddddddddddddddddddddddddddddddddddd&sizeThreshold=0&limit=100"
# `0xdddd...dddd` was observed as a `proxyWallet` in the public /trades feed,
# never supplied by a person. These are pinned so field-unit assertions are
# grounded in real server output, not in this repo's own fixtures.
# ---------------------------------------------------------------------------

REAL_ROW_OPEN_LOSS = {
    "proxyWallet": "0xdddddddddddddddddddddddddddddddddddddddd",
    "asset": "55115078421062885512539156303747803058407616201213034911037320915726138659123",
    "conditionId": "0x5db999fad322cea2914535aae5517060c3f80ad6d8c0231cde2124a434d16846",
    "size": 59099.8984,
    "avgPrice": 0.3315,
    "initialValue": 19596.9944,
    "currentValue": 9160.4842,
    "cashPnl": -10436.5102,
    "percentPnl": -53.2556,
    "totalBought": 59099.8984,
    "realizedPnl": 0,
    "percentRealizedPnl": -53.2556,
    "curPrice": 0.155,
    "redeemable": False,
    "mergeable": False,
    "title": "Will the U.S. invade Iran before 2027?",
    "eventSlug": "will-the-us-invade-iran-before-2027",
    "outcome": "Yes",
    "endDate": "2026-12-31",
    "negativeRisk": False,
}

# Same live pull: a position sized 0.91 shares. With the API's documented
# `sizeThreshold` default of 1, this row would never have been returned at
# all -- proof, on real data, of what polymarket_client.py:70-71's explicit
# `sizeThreshold: 0` override is actually preventing.
REAL_ROW_SUB_ONE_SHARE = {
    "asset": "5991830738606807897858383667717700309647789278773561135365744492801459043454",
    "conditionId": "0xef015f561b0c7af6abb7202e7a25749f168c5fcd1f43a7a4a64f1b93909f1b38",
    "size": 0.91,
    "avgPrice": 0.49,
    "initialValue": 0.4459,
    "currentValue": 0,
    "cashPnl": -0.4459,
    "percentPnl": -100,
    "totalBought": 0.91,
    "realizedPnl": 0,
    "percentRealizedPnl": -100,
    "curPrice": 0,
    "redeemable": True,
    "mergeable": False,
    "title": "Ethereum Up or Down - June 26, 6:55PM-7:00PM ET",
    "eventSlug": "eth-updown-5m-1782514500",
    "outcome": "Down",
    "endDate": "2026-06-26",
}

# From a different real wallet (0x0000000000000000000000000000000000000000,
# also sourced only from the public /trades feed): totalBought (515.2571)
# is roughly 2.4x the currently-held size (215.7171) because ~300 of the
# originally-bought shares were sold off. This is the live proof that
# totalBought is a SHARE count, and a *lifetime cumulative* one at that --
# never a dollar figure and never "shares currently held".
REAL_ROW_PARTIALLY_SOLD = {
    "asset": "26218726019311053184755697987866017884925318332399265731080389538226679668554",
    "conditionId": "0x0891e4792a016a079765dd25029639cd731f7dc0bde3d65d6002b2b19dae2777",
    "size": 215.7171,
    "avgPrice": 0.7398,
    "initialValue": 159.5901,
    "currentValue": 158.8109,
    "cashPnl": -0.7792,
    "percentPnl": -0.4882,
    "totalBought": 515.2571,
    "realizedPnl": -2.9951,
    "percentRealizedPnl": -58.3384,
    "curPrice": 0.7362,
    "redeemable": False,
    "mergeable": True,
    "title": "Argentina vs. Switzerland: Team to Advance",
    "eventSlug": "fifwc-arg-che-2026-07-11-more-markets",
    "outcome": "Argentina",
    "endDate": "2026-07-12",
}


# ===========================================================================
# 1. Endpoint, method, envelope shape
# ===========================================================================


def test_endpoint_matches_the_documented_url():
    # [DOC] "Full URL: https://data-api.polymarket.com/positions"
    assert BASE_URL == "https://data-api.polymarket.com"
    assert POSITIONS_PATH == "/positions"
    assert BASE_URL + POSITIONS_PATH == "https://data-api.polymarket.com/positions"


def test_fetch_issues_a_get_not_a_post_or_anything_else():
    # FakeSession only implements .get(); if the client tried any other
    # verb this test would raise AttributeError instead of passing.
    session = FakeSession([FakeResponse([])])
    PolymarketSource(session=session).fetch(WALLET)
    assert len(session.calls) == 1


def test_response_envelope_is_a_bare_array_not_a_wrapped_object():
    # [LIVE] curl -s https://data-api.polymarket.com/positions?user=0x0000000000000000000000000000000000000000
    #        -> `[]`  (HTTP 200, Content-Type: application/json)
    # [LIVE] curl -s .../positions?user=0xdddd...dddd&sizeThreshold=0
    #        -> `[{...}, {...}, ...]` -- a top-level array, never
    #        `{"data": [...]}` or `{"positions": [...]}`.
    session = FakeSession([FakeResponse([REAL_ROW_OPEN_LOSS])])
    positions = PolymarketSource(session=session).fetch(WALLET)
    assert len(positions) == 1

    # A wrapped envelope is correctly rejected, matching the documented
    # bare-array schema.
    session2 = FakeSession([FakeResponse({"data": [REAL_ROW_OPEN_LOSS]})])
    with pytest.raises(PolymarketError, match="array"):
        PolymarketSource(session=session2).fetch(WALLET)


def test_empty_wallet_and_nonexistent_wallet_are_indistinguishable_200_empty_array():
    # [LIVE] curl -s -i "https://data-api.polymarket.com/positions?user=0x0000000000000000000000000000000000000000"
    #        -> HTTP/1.1 200 OK, Content-Length: 2, body `[]`.
    # There is no 404 path for "this wallet never existed" -- Ethereum
    # addresses don't have a server-side existence check, so a wallet that
    # never traded and a wallet that traded and has zero open positions are
    # the same response. The client correctly does not special-case either.
    session = FakeSession([FakeResponse([], status_code=200)])
    assert PolymarketSource(session=session).fetch(WALLET) == []


# ===========================================================================
# 2. Query parameters
# ===========================================================================


def test_size_threshold_override_matches_a_real_observed_94_out_of_95_drop():
    # [DOC] sizeThreshold: "number, default 1, >= 0".
    # [LIVE] the SAME real wallet, same instant, two queries:
    #   .../positions?user=0xdddd...dddd                    -> 1 row
    #   .../positions?user=0xdddd...dddd&sizeThreshold=0     -> 95 rows
    # i.e. the documented default of 1 silently discarded 94 of 95 real
    # open positions for that wallet (most of its positions are small
    # stakes in 5-minute crypto up/down markets, size well under 1 share).
    # This is not a hypothetical: REAL_ROW_SUB_ONE_SHARE (size=0.91) is one
    # of the 94 rows that only sizeThreshold=0 recovers.
    session = FakeSession([FakeResponse([])])
    PolymarketSource(session=session).fetch(WALLET)
    assert session.calls[0]["params"]["sizeThreshold"] == 0


def test_page_limit_never_exceeds_the_documented_maximum_of_500():
    # [DOC] limit: "integer, default 100, range 0-500".
    # If PAGE_LIMIT were ever bumped above 500 (e.g. someone "optimizing"
    # round trips), a server that clamps oversized limit requests to its
    # real max (500) would silently hand back fewer rows than the client's
    # own offset arithmetic assumes per page, opening a gap: the client
    # would still do `offset += PAGE_LIMIT`, skipping over whatever the
    # server didn't actually deliver on that page.
    assert PAGE_LIMIT == 500


def test_max_offset_matches_the_documented_cap_of_10000():
    # [DOC] offset: "integer, default 0, range 0-10000".
    assert MAX_OFFSET == 10_000


def test_redeemable_and_mergeable_filters_are_never_sent_by_the_client():
    # [DOC] redeemable/mergeable are documented as optional filters with
    # "default: false" in the OpenAPI-style schema.
    # [LIVE] that "default: false" annotation does NOT mean "omitting the
    # param behaves like passing false". Actually executed, same wallet,
    # same instant:
    #   .../positions?user=0xdddd...dddd&sizeThreshold=0                      -> 95 rows (94 redeemable=true)
    #   .../positions?user=0xdddd...dddd&sizeThreshold=0&redeemable=false     -> 1 row  (the only redeemable=false one)
    #   .../positions?user=0xdddd...dddd&sizeThreshold=0&redeemable=true      -> 94 rows
    # Explicitly sending `redeemable: false` is a STRICT equality filter
    # that silently discards 94/95 real positions -- nothing like the
    # "don't filter" behavior of omitting the parameter. If a future
    # change "helpfully" added `"redeemable": False` to make the request
    # explicit, it would silently wipe out almost the whole portfolio with
    # no error. This test pins that the client must keep omitting both
    # keys entirely.
    session = FakeSession([FakeResponse([])])
    PolymarketSource(session=session).fetch(WALLET)
    params = session.calls[0]["params"]
    assert "redeemable" not in params
    assert "mergeable" not in params


def test_no_market_or_event_filter_is_applied_all_markets_are_requested():
    # [DOC] `market` (conditionId) and `eventId` are optional, mutually
    # exclusive filters that narrow results to specific markets/events.
    # The client wants a wallet's ENTIRE position list, so it must never
    # set either -- confirmed by inspecting the exact param set sent.
    session = FakeSession([FakeResponse([])])
    PolymarketSource(session=session).fetch(WALLET)
    params = session.calls[0]["params"]
    assert set(params.keys()) == {"user", "sizeThreshold", "limit", "offset"}


# ===========================================================================
# 3. Pagination correctness at the documented boundaries
# ===========================================================================


def test_offset_progression_walks_in_page_limit_steps_from_zero():
    full = [_row(f"a{i}") for i in range(PAGE_LIMIT)]
    session = FakeSession([FakeResponse(full), FakeResponse([_row("last")])])
    PolymarketSource(session=session).fetch(WALLET)
    assert [c["params"]["offset"] for c in session.calls] == [0, PAGE_LIMIT]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: a wallet with more open positions than the documented offset "
        "cap (10000) can reach is silently truncated -- fetch() returns a "
        "partial list with no exception, no flag, nothing to tell the "
        "caller the total is incomplete. [LIVE] confirmed the server "
        "itself never errors at/beyond this boundary either (curl "
        ".../positions?user=<real wallet>&offset=10001 and &offset=50000 "
        "both returned HTTP 200 with `[]`, no 400), so the client has no "
        "signal to hang this detection off of except its own MAX_OFFSET "
        "constant -- and today it doesn't use it for anything but silent "
        "termination. Independently found (via hostile-server fuzzing "
        "rather than live-API research) and already xfailed as C3 in "
        "tests/adversarial/test_client_hostile.py -- this test corroborates "
        "the same defect from real, documented API bounds."
    ),
)
def test_fetch_signals_when_truncated_by_the_offset_cap():
    # A wallet with far more than 10,500 concurrently open positions: every
    # page through offset=10000 (documented max) comes back completely
    # full, i.e. there is almost certainly more data the client could not
    # reach.
    n_pages = MAX_OFFSET // PAGE_LIMIT + 1  # offsets 0, 500, ..., 10000
    responses = [FakeResponse([_row(f"a{i}") for i in range(PAGE_LIMIT)]) for _ in range(n_pages)]
    session = FakeSession(responses)

    # Correct behavior: the client should not let this look like an
    # ordinary, complete result. It should raise (or otherwise flag) so
    # nothing downstream mistakes 10,500 rows for "all of them".
    with pytest.raises(PolymarketError):
        PolymarketSource(session=session).fetch(WALLET)


# FIXED: fetch() deduplicates by asset. The documented sortBy enum has no
# secondary tiebreaker, so offset-pagination drift can serve the same asset on
# two pages; fetch() keeps the last (freshest) copy so it is never double-
# counted downstream. _fetch_all_pages stays faithful to the raw response.
def test_fetch_deduplicates_a_position_repeated_across_pages():
    page1 = [_row(f"a{i}") for i in range(PAGE_LIMIT - 1)] + [_row("shared-asset")]
    page2 = [_row("shared-asset"), _row("tail")]
    session = FakeSession([FakeResponse(page1), FakeResponse(page2)])

    positions = PolymarketSource(session=session).fetch(WALLET)

    assets = [p.asset for p in positions]
    assert assets.count("shared-asset") == 1


# ===========================================================================
# 4. Field units and meaning (the highest-value area)
# ===========================================================================


def test_open_pnl_matches_cash_pnl_on_a_real_captured_position():
    # [LIVE] cashPnl == currentValue - initialValue held EXACTLY across all
    # 95 real rows sampled from 0xdddd...dddd (0/95 mismatches, tolerance
    # 0.01). This single row is one of them:
    #   currentValue(9160.4842) - initialValue(19596.9944) = -10436.5102 == cashPnl
    p = Position.from_api(REAL_ROW_OPEN_LOSS)
    assert math.isclose(p.open_pnl, REAL_ROW_OPEN_LOSS["cashPnl"], abs_tol=1e-6)
    assert math.isclose(p.open_pnl, p.current_value - p.stake, abs_tol=1e-9)


def test_percent_pnl_is_stored_as_a_percentage_not_a_fraction():
    # [DOC/LIVE] percentPnl = cashPnl / initialValue * 100, i.e. -53.2556
    # means "down 53.2556%", not "down 0.532556x". Verified live:
    #   cashPnl(-10436.5102) / initialValue(19596.9944) * 100 == -53.2555... ~ -53.2556
    # models.py must copy this through UNCHANGED. If a future change tried
    # to "normalize" it to a fraction (divide by 100) or format it with
    # Python's `%`-style spec (which multiplies by 100 again), either
    # direction is a 100x unit bug.
    p = Position.from_api(REAL_ROW_OPEN_LOSS)
    assert p.percent_pnl == pytest.approx(-53.2556, abs=1e-6)
    live_cash_over_initial_pct = (
        REAL_ROW_OPEN_LOSS["cashPnl"] / REAL_ROW_OPEN_LOSS["initialValue"] * 100
    )
    assert p.percent_pnl == pytest.approx(live_cash_over_initial_pct, abs=0.01)
    # Sanity guard against the inverse bug: it must NOT look like a
    # fraction (would be ~-0.53, comfortably inside [-1, 1] alongside
    # legitimate small percentages -- so assert against the specific
    # known-wrong transposed value instead of a range check).
    assert p.percent_pnl != pytest.approx(-0.532556, abs=1e-6)


def test_total_bought_is_a_lifetime_share_count_and_is_correctly_ignored_for_stake():
    # [LIVE] REAL_ROW_PARTIALLY_SOLD: totalBought=515.2571 vs currently-held
    # size=215.7171 -- totalBought is ~2.4x size because ~300 shares from
    # this position were already sold off. It tracks cumulative SHARES
    # ever bought, in share units (same order of magnitude as `size`, NOT
    # dollars: initialValue for the same row is only 159.59). Using it as
    # a dollar stake would be both the wrong unit and the wrong quantity
    # (lifetime gross, not current cost basis).
    p = Position.from_api(REAL_ROW_PARTIALLY_SOLD)
    assert p.stake == REAL_ROW_PARTIALLY_SOLD["initialValue"]
    assert p.stake != REAL_ROW_PARTIALLY_SOLD["totalBought"]
    assert p.size != REAL_ROW_PARTIALLY_SOLD["totalBought"]


def test_sub_one_share_position_survives_normalization():
    # [LIVE] REAL_ROW_SUB_ONE_SHARE (size=0.91) is a real row that the
    # documented sizeThreshold default (1) would have excluded entirely at
    # the HTTP layer. Once it does arrive, models.py must not additionally
    # drop or distort it for being small.
    p = Position.from_api(REAL_ROW_SUB_ONE_SHARE)
    assert p.size == 0.91
    assert p.redeemable is True
    assert p.current_price == 0.0
    assert p.current_value == 0.0
    assert p.open_pnl == pytest.approx(-0.4459, abs=1e-9)


def test_current_value_is_taken_from_the_api_field_not_recomputed():
    # size * curPrice reconstructs currentValue only up to the display
    # rounding of curPrice/avgPrice (confirmed live: avgPrice/curPrice are
    # rounded to ~3-4 decimals while initialValue/currentValue carry full
    # precision). The code must trust the authoritative dollar field
    # rather than reconstructing it and compounding rounding error.
    raw = _row(
        "x",
        size=100.0,
        curPrice=0.333,  # rounded for display
        currentValue=33.4501,  # authoritative, NOT exactly size*curPrice (33.3)
    )
    p = Position.from_api(raw)
    assert p.current_value == 33.4501
    assert p.current_value != p.size * p.current_price


def test_asset_and_condition_id_survive_as_strings_never_coerced_to_numbers():
    # [LIVE] `asset` is a ~77-digit ERC-1155 token id, `conditionId` a
    # 66-char 0x hash -- both arrive JSON-quoted (strings) in the real
    # payload, and both are FAR beyond float64's 15-17 significant digits
    # of precision. Coercing either to a number would silently corrupt the
    # identifier used to join positions across snapshots.
    p = Position.from_api(REAL_ROW_OPEN_LOSS)
    assert p.asset == REAL_ROW_OPEN_LOSS["asset"]
    assert isinstance(p.asset, str)
    assert p.condition_id == REAL_ROW_OPEN_LOSS["conditionId"]
    assert isinstance(p.condition_id, str)


# ===========================================================================
# 5. Fields the API returns that the code ignores
# ===========================================================================


def test_position_does_not_expose_fields_the_client_never_reads():
    # [DOC] full response schema also includes: proxyWallet, cashPnl,
    # totalBought, percentRealizedPnl, mergeable, slug, icon, eventId,
    # outcomeIndex, oppositeOutcome, oppositeAsset, negativeRisk.
    # None of these change the dollar/share arithmetic the code performs
    # on the fields it DOES read (verified above: cashPnl/percentPnl/
    # currentValue/initialValue relationships hold identically whether or
    # not a row has mergeable=true or negativeRisk=true -- 0/95 mismatches
    # on live data covering both). This test simply pins which fields
    # models.Position exposes, as a map of the ignored surface for anyone
    # auditing this again later.
    field_names = {f.name for f in dataclasses.fields(Position)}
    never_exposed = {
        "proxy_wallet",
        "cash_pnl",
        "total_bought",
        "percent_realized_pnl",
        "mergeable",
        "slug",
        "icon",
        "event_id",
        "outcome_index",
        "opposite_outcome",
        "opposite_asset",
        "negative_risk",
    }
    assert field_names.isdisjoint(never_exposed)


def test_negative_risk_markets_use_identical_dollar_arithmetic():
    # [LIVE] "Will Norway win the 2026 FIFA World Cup?" -- negativeRisk:true
    # -- still satisfies cashPnl == currentValue - initialValue exactly
    # (initialValue=13553.033, currentValue=18169.242, cashPnl=4616.2089).
    # Confirms ignoring `negativeRisk` is safe: it does not change what
    # size/avgPrice/curPrice/initialValue/currentValue mean for a position.
    raw = _row(
        "y",
        size=305365.4119,
        avgPrice=0.0443,
        initialValue=13553.033,
        currentValue=18169.242,
        cashPnl=4616.2089,
        curPrice=0.0595,
    )
    raw["negativeRisk"] = True
    p = Position.from_api(raw)
    assert math.isclose(p.open_pnl, raw["cashPnl"], abs_tol=1e-3)


# ===========================================================================
# 6. Wallet address handling
# ===========================================================================


def test_wallet_validation_accepts_mixed_case_matching_live_api_case_insensitivity():
    # [LIVE] queried the SAME wallet lowercase and fully-uppercase-hex:
    #   user=0xdddddddddddddddddddddddddddddddddddddddd  -> 95 rows
    #   user=0xDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD  -> 95 rows (identical)
    # confirming the live API is case-insensitive on `user`, as Ethereum
    # addresses are at the protocol level. validate_wallet must not be
    # stricter than the server and reject a valid mixed-case address.
    lower = "0xdddddddddddddddddddddddddddddddddddddddd"
    upper = "0xDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
    # Both accepted (not rejected), and both canonicalized to the same
    # lowercase form -- so the same account never fragments into two identities.
    assert validate_wallet(lower) == lower
    assert validate_wallet(upper) == lower


def test_malformed_wallet_never_reaches_the_network():
    # [LIVE] curl .../positions?user=notawallet -> HTTP 400
    #   {"error":"required query param 'user' not provided"}
    # i.e. the server itself rejects it (400) with no distinct message from
    # a truly missing param. The client is correctly stricter: it rejects
    # client-side before ever making the request, so this failure mode is
    # never observed against the live server at all.
    session = FakeSession([FakeResponse([])])
    with pytest.raises(Exception):
        validate_wallet("notawallet")
    assert session.calls == []
