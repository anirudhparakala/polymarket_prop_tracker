"""Adversarial tests for polymarket_client.py and models.Position.from_api.

This suite treats the remote Polymarket server as hostile: broken, malicious,
or merely unusual. Every test is fully offline -- `requests.Session` is
replaced with a hand-built fake (or, where fidelity to the real JSON/HTTP
exception types matters, a genuine `requests.Response` object populated
in-memory with no socket ever opened). No test in this file makes a network
call.

Every loop-bearing fake session has a hard call cap so a pagination bug in
the code under test can never hang the suite -- it fails loudly instead.

Findings are written up in .superpowers/adversarial/client-findings.md.
Real bugs are encoded here as `@pytest.mark.xfail(strict=True, ...)` tests:
the assertion inside states the CORRECT behavior, which currently fails
against the actual (buggy) behavior. If the bug is ever fixed, the test
starts passing and pytest reports XPASS (a hard failure under strict=True),
which is the point: the fix is caught automatically.

Run:
    .venv/Scripts/python.exe -m pytest tests/adversarial/test_client_hostile.py -v --basetemp=.pytest_tmp/client
"""

from __future__ import annotations

import math

import pytest
import requests

import polymarket_client as pc
from models import Position

VALID_WALLET = "0x" + "a" * 40


# ---------------------------------------------------------------------------
# Fakes. Nothing here touches a socket.
# ---------------------------------------------------------------------------


class FakeResponse:
    """Stand-in for requests.Response exposing only what the client touches:
    .status_code and .json(). Can be told to raise from .json() to simulate
    a body that fails to parse (bad JSON, truncated stream, etc.).
    """

    def __init__(self, status_code, payload=None, json_exc: Exception | None = None):
        self.status_code = status_code
        self._payload = payload
        self._json_exc = json_exc

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload


class ScriptedSession:
    """Fake requests.Session. Returns FakeResponse objects from a fixed
    script, one per call (the last entry repeats if more calls happen than
    scripted responses). Records every call's params/timeout for inspection.
    Hard-capped so a pagination bug cannot hang the test process.
    """

    def __init__(self, responses, hard_cap: int = 200):
        self.responses = list(responses)
        self.hard_cap = hard_cap
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(
            {"url": url, "params": dict(params) if params else {}, "timeout": timeout}
        )
        if len(self.calls) > self.hard_cap:
            raise RuntimeError(
                f"SAFETY STOP: session.get called {len(self.calls)} times "
                f"(hard_cap={self.hard_cap}) -- pagination under test did not "
                "terminate on its own."
            )
        if not self.responses:
            raise RuntimeError("ScriptedSession has no responses configured")
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


class RepeatingFullPageSession:
    """The most hostile pagination case: a server that ALWAYS returns a full
    page, no matter the offset -- as if it ignores the offset parameter
    entirely and always hands back "page 1". Never returns a short page, so
    the client can only stop because of its own internal cap. Hard-capped as
    a last-resort safety net; the real assertion under test is that the
    client stops well before the hard cap is reached.
    """

    def __init__(self, page_limit: int, hard_cap: int = 200):
        self.page_limit = page_limit
        self.hard_cap = hard_cap
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params) if params else {})
        if len(self.calls) > self.hard_cap:
            raise RuntimeError(
                "SAFETY STOP: hostile always-full-page server was not "
                f"bounded by the client after {self.hard_cap} calls."
            )
        row = {"asset": "dup-asset", "conditionId": "dup-condition", "size": 1}
        return FakeResponse(200, [row] * self.page_limit)


class HonestPagedDataset:
    """A well-behaved server backing a wallet with `total` real positions.
    Correctly honors offset/limit. Used to check what the client does when
    the true dataset is larger than the client's own offset cap.
    """

    def __init__(self, total: int):
        self.total = total
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        offset, limit = params["offset"], params["limit"]
        chunk = [
            {"asset": f"pos-{i}", "conditionId": f"cond-{i}"}
            for i in range(offset, min(offset + limit, self.total))
        ]
        return FakeResponse(200, chunk)


class RaisingSession:
    """Fake session whose .get() always raises a pre-built exception."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        raise self.exc


def make_source(session) -> pc.PolymarketSource:
    return pc.PolymarketSource(session=session)


# ===========================================================================
# 1. Pagination boundaries
# ===========================================================================


class TestPaginationBoundaries:
    def test_exactly_one_page_stops_after_one_call(self, monkeypatch):
        monkeypatch.setattr(pc, "PAGE_LIMIT", 5)
        monkeypatch.setattr(pc, "MAX_OFFSET", 100)
        session = ScriptedSession([FakeResponse(200, [{"asset": "a"}, {"asset": "b"}])])
        rows = make_source(session)._fetch_all_pages(VALID_WALLET)
        assert len(session.calls) == 1
        assert len(rows) == 2

    def test_exactly_one_full_page_makes_one_confirmation_call(self, monkeypatch):
        """A result set exactly PAGE_LIMIT long: the client cannot know it's
        the end without asking again, so a second call at the next offset is
        expected and correct (not a bug)."""
        monkeypatch.setattr(pc, "PAGE_LIMIT", 3)
        monkeypatch.setattr(pc, "MAX_OFFSET", 100)
        full_page = [{"asset": str(i)} for i in range(3)]
        session = ScriptedSession([FakeResponse(200, full_page), FakeResponse(200, [])])
        rows = make_source(session)._fetch_all_pages(VALID_WALLET)
        assert [c["params"]["offset"] for c in session.calls] == [0, 3]
        assert len(rows) == 3

    def test_one_page_plus_one_record(self, monkeypatch):
        monkeypatch.setattr(pc, "PAGE_LIMIT", 3)
        monkeypatch.setattr(pc, "MAX_OFFSET", 100)
        page1 = [{"asset": str(i)} for i in range(3)]
        page2 = [{"asset": "extra"}]
        session = ScriptedSession([FakeResponse(200, page1), FakeResponse(200, page2)])
        rows = make_source(session)._fetch_all_pages(VALID_WALLET)
        assert len(session.calls) == 2
        assert len(rows) == 4

    def test_hostile_never_short_page_server_is_still_bounded(self, monkeypatch):
        """The API-hostile case: server always returns a full page, never a
        short one. Proves the loop terminates anyway, bounded purely by the
        client's own MAX_OFFSET / PAGE_LIMIT cap, and pins down the exact
        offset sequence requested."""
        monkeypatch.setattr(pc, "PAGE_LIMIT", 3)
        monkeypatch.setattr(pc, "MAX_OFFSET", 9)
        session = RepeatingFullPageSession(page_limit=3, hard_cap=50)
        rows = make_source(session)._fetch_all_pages(VALID_WALLET)
        # (MAX_OFFSET / PAGE_LIMIT) + 1 = 4 calls: offsets 0, 3, 6, 9.
        assert [c["offset"] for c in session.calls] == [0, 3, 6, 9]
        assert len(rows) == 12  # all duplicates -- see TestSilentDataLoss

    def test_one_call_past_the_cap_is_never_made(self, monkeypatch):
        monkeypatch.setattr(pc, "PAGE_LIMIT", 3)
        monkeypatch.setattr(pc, "MAX_OFFSET", 9)
        session = RepeatingFullPageSession(page_limit=3, hard_cap=50)
        make_source(session)._fetch_all_pages(VALID_WALLET)
        assert 12 not in [c["offset"] for c in session.calls]
        assert len(session.calls) == 4

    def test_fewer_than_asked_but_nonzero_repeatedly_terminates_immediately(self, monkeypatch):
        """A short (but nonzero) page is itself the termination signal --
        confirm the client doesn't keep pulling more pages 'just in case'."""
        monkeypatch.setattr(pc, "PAGE_LIMIT", 5)
        monkeypatch.setattr(pc, "MAX_OFFSET", 100)
        session = ScriptedSession([FakeResponse(200, [{"asset": "only-one"}])])
        rows = make_source(session)._fetch_all_pages(VALID_WALLET)
        assert len(session.calls) == 1
        assert len(rows) == 1

    def test_real_constants_bound_to_21_calls_worst_case(self):
        """Uses the real, un-monkeypatched PAGE_LIMIT=500 / MAX_OFFSET=10_000
        against a server that always returns a full page. Confirms the
        production constants yield a small, finite, provable call count."""
        session = RepeatingFullPageSession(page_limit=pc.PAGE_LIMIT, hard_cap=1000)
        make_source(session)._fetch_all_pages(VALID_WALLET)
        assert len(session.calls) == 21

    def test_timeout_is_set_on_every_page_of_a_multipage_fetch(self, monkeypatch):
        monkeypatch.setattr(pc, "PAGE_LIMIT", 2)
        monkeypatch.setattr(pc, "MAX_OFFSET", 100)
        pages = [
            FakeResponse(200, [{"asset": "a"}, {"asset": "b"}]),
            FakeResponse(200, [{"asset": "c"}, {"asset": "d"}]),
            FakeResponse(200, []),
        ]
        session = ScriptedSession(pages)
        make_source(session)._fetch_all_pages(VALID_WALLET)
        assert len(session.calls) == 3
        assert all(c["timeout"] == pc.TIMEOUT_SECONDS for c in session.calls)


# ===========================================================================
# 2. Silent data loss (the most serious category: no exception, wrong result)
# ===========================================================================


class TestSilentDataLoss:
    # FIXED: fetch() deduplicates by asset (see test body).
    def test_duplicate_rows_across_pages_are_deduplicated(self, monkeypatch):
        # FIXED at the public boundary: _fetch_all_pages stays faithful to the
        # raw transport (it returns every row the server sent), but fetch()
        # deduplicates by asset. A server that ignores offset and re-emits the
        # same record on every page collapses to a single Position instead of
        # being multiplied -- which also protects the checkpoint save's
        # UNIQUE(checkpoint_id, asset) constraint.
        monkeypatch.setattr(pc, "PAGE_LIMIT", 3)
        monkeypatch.setattr(pc, "MAX_OFFSET", 9)
        session = RepeatingFullPageSession(page_limit=3, hard_cap=50)
        positions = make_source(session).fetch(VALID_WALLET)
        assert len(positions) == 1

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: a wallet with more positions than MAX_OFFSET/PAGE_LIMIT "
        "covers (>10,500) is silently truncated with no exception, no "
        "warning, and no way for the caller to detect data is missing.",
    )
    def test_wallet_with_more_than_max_offset_positions_is_not_silently_truncated(self):
        dataset = HonestPagedDataset(total=50_000)
        rows = make_source(dataset)._fetch_all_pages(VALID_WALLET)
        # A correct client should either fetch everything or raise/signal
        # that it could not. It should not silently hand back a partial
        # result indistinguishable from "the wallet really only has this
        # many positions".
        assert len(rows) == dataset.total

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: when a single page contains more rows than the "
        "requested `limit`, the client accepts them unconditionally instead "
        "of treating it as a protocol violation. Combined with no "
        "deduplication, a server that pads/repeats rows silently inflates "
        "the result.",
    )
    def test_oversized_page_is_rejected_or_truncated(self, monkeypatch):
        monkeypatch.setattr(pc, "PAGE_LIMIT", 5)
        monkeypatch.setattr(pc, "MAX_OFFSET", 100)
        # Server hands back 2x what was asked for on the very first page.
        oversized = [{"asset": str(i)} for i in range(10)]
        session = ScriptedSession([FakeResponse(200, oversized), FakeResponse(200, [])])
        rows = make_source(session)._fetch_all_pages(VALID_WALLET)
        assert len(rows) <= pc.PAGE_LIMIT * 1  # first page alone already violates this


# ===========================================================================
# 3. Malformed payloads
# ===========================================================================


class TestMalformedPayloads:
    def test_non_array_json_object_is_rejected(self):
        session = ScriptedSession([FakeResponse(200, {"not": "a list"})])
        with pytest.raises(pc.PolymarketError):
            make_source(session)._fetch_page(VALID_WALLET, 0)

    def test_json_null_is_rejected(self):
        session = ScriptedSession([FakeResponse(200, None)])
        with pytest.raises(pc.PolymarketError):
            make_source(session)._fetch_page(VALID_WALLET, 0)

    def test_json_scalar_is_rejected(self):
        session = ScriptedSession([FakeResponse(200, 42)])
        with pytest.raises(pc.PolymarketError):
            make_source(session)._fetch_page(VALID_WALLET, 0)

    def test_objects_missing_every_field_normalize_to_defaults(self):
        # Distinct assets so the by-asset dedup keeps both rows; the point here
        # is that every *other* missing field normalizes to its default.
        session = ScriptedSession([FakeResponse(200, [{"asset": "a"}, {"asset": "b"}])])
        positions = make_source(session).fetch(VALID_WALLET)
        assert len(positions) == 2
        assert positions[0] == Position(
            asset="a",
            condition_id="",
            market_title="",
            event_slug="",
            outcome="",
            size=0.0,
            entry_price=0.0,
            current_price=0.0,
            stake=0.0,
            current_value=0.0,
            open_pnl=0.0,
            percent_pnl=0.0,
            realized_pnl=0.0,
            redeemable=False,
            end_date="",
        )

    def test_unknown_extra_fields_are_ignored_not_crashed_on(self):
        raw = {"asset": "abc", "totallyUnknownField": {"nested": [1, 2, 3]}}
        p = Position.from_api(raw)
        assert p.asset == "abc"

    def test_explicit_null_values_use_defaults_not_typeerror(self):
        raw = {"size": None, "avgPrice": None, "title": None, "redeemable": None}
        p = Position.from_api(raw)
        assert p.size == 0.0
        assert p.entry_price == 0.0
        assert p.market_title == ""
        assert p.redeemable is False

    # FIXED: the client rejects a non-object array element with PolymarketError.
    def test_array_of_non_object_elements_raises_polymarket_error(self):
        session = ScriptedSession([FakeResponse(200, [1, 2, "abc", None])])
        with pytest.raises(pc.PolymarketError):
            make_source(session).fetch(VALID_WALLET)

    # FIXED: response.json() is now wrapped, so a non-JSON body raises
    # PolymarketError instead of a raw JSONDecodeError.
    def test_non_json_body_raises_polymarket_error(self):
        # A genuine requests.Response with a body that isn't JSON at all --
        # constructed entirely in memory, no socket involved.
        response = requests.Response()
        response.status_code = 200
        response._content = b"<html>502 Bad Gateway from some proxy</html>"

        class OneShotSession:
            def get(self, *a, **k):
                return response

        with pytest.raises(pc.PolymarketError):
            make_source(OneShotSession())._fetch_page(VALID_WALLET, 0)

    # FIXED: a RequestException raised while reading the body is now caught and
    # re-raised as PolymarketError.
    def test_body_that_dies_mid_stream_raises_polymarket_error(self):
        response = FakeResponse(200, json_exc=requests.exceptions.ChunkedEncodingError(
            "Connection broken: body ended mid-stream"
        ))

        class OneShotSession:
            def get(self, *a, **k):
                return response

        with pytest.raises(pc.PolymarketError):
            make_source(OneShotSession())._fetch_page(VALID_WALLET, 0)


# ===========================================================================
# 4. Type coercion in Position.from_api
# ===========================================================================


class TestTypeCoercion:
    def test_string_number_coerces_correctly(self):
        p = Position.from_api({"size": "1.5", "avgPrice": "0.42"})
        assert p.size == 1.5
        assert p.entry_price == 0.42

    def test_nan_string_does_not_produce_a_nan_field(self):
        # FIXED: models._f now rejects non-finite results (NaN/Inf) and falls
        # back to the default rather than poisoning a Position field.
        p = Position.from_api({"size": "NaN"})
        assert not math.isnan(p.size)
        assert p.size == 0.0

    def test_json_literal_nan_infinity_does_not_reach_a_position_unvalidated(self):
        # FIXED: even if the parser yields NaN/Infinity from the raw tokens,
        # models._f screens them out at the boundary.
        response = requests.Response()
        response.status_code = 200
        response._content = (
            b'[{"asset": "x", "size": NaN, "currentValue": Infinity}]'
        )

        class OneShotSession:
            def get(self, *a, **k):
                return response

        positions = make_source(OneShotSession()).fetch(VALID_WALLET)
        p = positions[0]
        assert not math.isnan(p.size)
        assert math.isfinite(p.current_value)

    def test_string_false_does_not_become_boolean_true(self):
        # FIXED: models._b interprets a boolean-ish string by content instead
        # of by truthiness, so the string "false" no longer flips to True.
        p = Position.from_api({"redeemable": "false"})
        assert p.redeemable is False

    def test_real_boolean_values_round_trip_correctly(self):
        assert Position.from_api({"redeemable": True}).redeemable is True
        assert Position.from_api({"redeemable": False}).redeemable is False
        assert Position.from_api({}).redeemable is False

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: a JSON boolean where a number is expected is silently "
        "accepted -- Python's float(True) == 1.0 and float(False) == 0.0 -- "
        "masking a field-type mismatch as a plausible-looking numeric value "
        "instead of surfacing the malformed data.",
    )
    def test_boolean_where_number_expected_is_rejected_not_coerced(self):
        with pytest.raises((TypeError, ValueError)):
            Position.from_api({"size": True})

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: size/entry_price/current_price/stake accept negative "
        "values with no validation. These are share counts and per-share "
        "prices/dollar amounts that can never legitimately be negative; a "
        "corrupted or hostile server value passes through silently.",
    )
    def test_negative_size_and_price_are_rejected(self):
        with pytest.raises(ValueError):
            Position.from_api({"size": -50, "avgPrice": -1})

    def test_empty_string_defaults_rather_than_crashing_the_whole_fetch(self):
        # Design choice (over the original "should raise"): a single malformed
        # field must not abort the entire fetch/save. The db layer proved that
        # letting one bad field raise destroys every sibling position in the
        # batch, so models._f contains the damage to the one field instead.
        p = Position.from_api({"size": ""})
        assert p.size == 0.0

    def test_list_value_for_numeric_field_defaults_rather_than_crashing(self):
        # Same containment policy: a structurally wrong value degrades that one
        # field to the default instead of taking down the whole response.
        p = Position.from_api({"size": [1, 2, 3]})
        assert p.size == 0.0

    # FIXED: models._f catches OverflowError (an outsized int has no float
    # form) and falls back to the default.
    def test_huge_integer_does_not_crash_with_overflowerror(self):
        try:
            Position.from_api({"size": 10**400})
        except OverflowError:
            pytest.fail("OverflowError leaked out of Position.from_api")

    def test_number_where_string_expected_is_stringified_not_crashed(self):
        """Documented current behavior, not asserted as ideal: `_s` calls
        str() unconditionally, so a stray number/dict in a string field
        becomes a syntactically valid (if semantically wrong) string rather
        than crashing. See findings report -- flagged Minor."""
        p = Position.from_api({"outcome": 123, "title": {"nested": "x"}})
        assert p.outcome == "123"
        assert "nested" in p.market_title


# ===========================================================================
# 5. HTTP semantics
# ===========================================================================


class TestHttpSemantics:
    @pytest.mark.parametrize(
        "status_code",
        [201, 204, 300, 301, 302, 304, 400, 401, 403, 404, 409, 418, 422, 451],
    )
    def test_non_200_2xx_3xx_4xx_never_silently_treated_as_success(self, status_code):
        session = ScriptedSession([FakeResponse(status_code, [])])
        with pytest.raises(pc.PolymarketError):
            make_source(session)._fetch_page(VALID_WALLET, 0)

    @pytest.mark.parametrize("status_code", [500, 502, 503, 504])
    def test_5xx_raises_polymarket_error(self, status_code):
        session = ScriptedSession([FakeResponse(status_code, [])])
        with pytest.raises(pc.PolymarketError):
            make_source(session)._fetch_page(VALID_WALLET, 0)

    def test_429_raises_distinct_rate_limit_message(self):
        session = ScriptedSession([FakeResponse(429, [])])
        with pytest.raises(pc.PolymarketError, match="rate limit"):
            make_source(session)._fetch_page(VALID_WALLET, 0)

    def test_200_with_empty_array_is_a_normal_success(self):
        session = ScriptedSession([FakeResponse(200, [])])
        assert make_source(session)._fetch_page(VALID_WALLET, 0) == []

    # FIXED: the transport-failure message reports only the exception type, not
    # its text (which embeds the request URL with the wallet as a query param).
    def test_wallet_address_does_not_leak_into_error_message_on_connection_failure(self):
        wallet = "0x" + "b" * 40
        exc = requests.exceptions.ConnectionError(
            f"Failed to establish a new connection: GET https://data-api.polymarket.com"
            f"/positions?user={wallet}&sizeThreshold=0"
        )
        session = RaisingSession(exc)
        with pytest.raises(pc.PolymarketError) as excinfo:
            make_source(session)._fetch_page(wallet, 0)
        assert wallet not in str(excinfo.value)


# ===========================================================================
# 6. Transport failures
# ===========================================================================


class TestTransportFailures:
    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.Timeout("timed out"),
            requests.exceptions.ConnectionError("connection reset by peer"),
            requests.exceptions.SSLError("certificate verify failed"),
            requests.exceptions.TooManyRedirects("exceeded redirect limit"),
            requests.exceptions.RequestException("generic transport failure"),
        ],
    )
    def test_transport_failures_are_wrapped_as_polymarket_error(self, exc):
        session = RaisingSession(exc)
        with pytest.raises(pc.PolymarketError):
            make_source(session)._fetch_page(VALID_WALLET, 0)

    def test_transport_failure_does_not_leave_raw_requests_exception_type(self):
        session = RaisingSession(requests.exceptions.ConnectionError("boom"))
        with pytest.raises(pc.PolymarketError) as excinfo:
            make_source(session)._fetch_page(VALID_WALLET, 0)
        assert not isinstance(excinfo.value, requests.exceptions.RequestException)


# ===========================================================================
# 7. Input validation: the wallet address
# ===========================================================================


class TestWalletValidation:
    def test_valid_address_accepted(self):
        assert pc.validate_wallet(VALID_WALLET) == VALID_WALLET

    def test_mixed_case_hex_accepted_and_normalized(self):
        mixed = "0x" + "aB3F" * 10
        # Accepted, and returned in canonical lowercase form so the same
        # account always compares equal regardless of display casing.
        assert pc.validate_wallet(mixed) == mixed.lower()

    def test_missing_0x_prefix_rejected(self):
        with pytest.raises(pc.InvalidWalletError):
            pc.validate_wallet("a" * 42)

    def test_duplicated_0x_prefix_rejected(self):
        with pytest.raises(pc.InvalidWalletError):
            pc.validate_wallet("0x0x" + "a" * 38)

    def test_39_char_address_rejected(self):
        with pytest.raises(pc.InvalidWalletError):
            pc.validate_wallet("0x" + "a" * 39)

    def test_41_char_address_rejected(self):
        with pytest.raises(pc.InvalidWalletError):
            pc.validate_wallet("0x" + "a" * 41)

    def test_leading_whitespace_stripped_and_accepted(self):
        # A pasted leading space is a copy-paste artifact, not an invalid
        # address: it is stripped and the clean address accepted, rather than
        # rejected or (worse) passed through raw to the network.
        assert pc.validate_wallet(" " + VALID_WALLET) == VALID_WALLET.lower()

    def test_trailing_space_stripped_and_accepted(self):
        assert pc.validate_wallet(VALID_WALLET + " ") == VALID_WALLET.lower()

    def test_fullwidth_unicode_zero_rejected(self):
        """A full-width '0' (U+FF10) is accepted by str.isdigit() but is NOT
        in the [0-9a-fA-F] character class the regex actually uses --
        confirms the validator resists this classic Unicode-digit trick."""
        sneaky = "0x" + "０" * 40
        with pytest.raises(pc.InvalidWalletError):
            pc.validate_wallet(sneaky)

    def test_non_string_input_rejected(self):
        with pytest.raises(pc.InvalidWalletError):
            pc.validate_wallet(12345)  # type: ignore[arg-type]

    def test_none_input_rejected(self):
        with pytest.raises(pc.InvalidWalletError):
            pc.validate_wallet(None)  # type: ignore[arg-type]

    def test_invalid_wallet_never_reaches_the_network(self):
        session = ScriptedSession([FakeResponse(200, [])])
        with pytest.raises(pc.InvalidWalletError):
            make_source(session).fetch("not-a-wallet")
        assert len(session.calls) == 0

    def test_trailing_newline_is_stripped_not_sent_to_the_network(self):
        # FIXED: validate_wallet now strips and uses fullmatch, so a trailing
        # newline is removed and the returned value is a clean address that
        # cannot reach the query string with an embedded newline.
        result = pc.validate_wallet(VALID_WALLET + "\n")
        assert "\n" not in result
        assert result == VALID_WALLET.lower()


# ===========================================================================
# 8. Resource safety
# ===========================================================================


class TestResourceSafety:
    def test_session_is_reused_not_recreated_across_fetch_calls(self):
        session = ScriptedSession([FakeResponse(200, [])])
        source = make_source(session)
        source.fetch(VALID_WALLET)
        source.fetch(VALID_WALLET)
        assert source._session is session
        assert len(session.calls) == 2

    def test_default_session_created_lazily_when_none_provided(self):
        source = pc.PolymarketSource()
        assert isinstance(source._session, requests.Session)
