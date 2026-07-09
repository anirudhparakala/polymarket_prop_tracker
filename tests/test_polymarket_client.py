import pytest
import requests

from models import Position
from polymarket_client import (
    PAGE_LIMIT,
    InvalidWalletError,
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


def _row(asset: str) -> dict:
    return {
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
        "curPrice": 0.5,
        "realizedPnl": 0.0,
    }


def test_validate_wallet_accepts_a_well_formed_address():
    assert validate_wallet(WALLET) == WALLET


@pytest.mark.parametrize("bad", ["", "0x123", "abc", None, "0x" + "z" * 40])
def test_validate_wallet_rejects_malformed_addresses(bad):
    with pytest.raises(InvalidWalletError):
        validate_wallet(bad)


def test_fetch_always_sends_size_threshold_zero():
    # API default is 1, which silently drops sub-1-share positions and makes
    # them look Closed.
    session = FakeSession([FakeResponse([])])
    PolymarketSource(session=session).fetch(WALLET)
    assert session.calls[0]["params"]["sizeThreshold"] == 0


def test_fetch_requests_the_maximum_page_size():
    session = FakeSession([FakeResponse([])])
    PolymarketSource(session=session).fetch(WALLET)
    assert session.calls[0]["params"]["limit"] == PAGE_LIMIT
    assert session.calls[0]["params"]["user"] == WALLET


def test_fetch_paginates_until_a_short_page_arrives():
    full = [_row(f"a{i}") for i in range(PAGE_LIMIT)]
    session = FakeSession([FakeResponse(full), FakeResponse([_row("last")])])
    positions = PolymarketSource(session=session).fetch(WALLET)
    assert len(positions) == PAGE_LIMIT + 1
    assert [c["params"]["offset"] for c in session.calls] == [0, PAGE_LIMIT]


def test_fetch_stops_after_one_page_when_page_is_short():
    session = FakeSession([FakeResponse([_row("a")])])
    positions = PolymarketSource(session=session).fetch(WALLET)
    assert len(positions) == 1
    assert len(session.calls) == 1


def test_fetch_returns_normalized_positions():
    session = FakeSession([FakeResponse([_row("a")])])
    (position,) = PolymarketSource(session=session).fetch(WALLET)
    assert isinstance(position, Position)
    assert position.stake == 5.0
    assert position.market_title == "Will Morocco win?"


def test_fetch_returns_empty_list_for_a_wallet_with_no_positions():
    session = FakeSession([FakeResponse([])])
    assert PolymarketSource(session=session).fetch(WALLET) == []


def test_fetch_never_calls_the_api_for_an_invalid_wallet():
    session = FakeSession([FakeResponse([])])
    with pytest.raises(InvalidWalletError):
        PolymarketSource(session=session).fetch("nope")
    assert session.calls == []


def test_rate_limit_raises_a_readable_error():
    session = FakeSession([FakeResponse(status_code=429)])
    with pytest.raises(PolymarketError, match="rate limit"):
        PolymarketSource(session=session).fetch(WALLET)


def test_server_error_raises_a_readable_error():
    session = FakeSession([FakeResponse(status_code=503)])
    with pytest.raises(PolymarketError, match="503"):
        PolymarketSource(session=session).fetch(WALLET)


def test_network_failure_raises_a_readable_error():
    session = FakeSession([requests.RequestException("boom")])
    with pytest.raises(PolymarketError, match="Could not reach"):
        PolymarketSource(session=session).fetch(WALLET)


def test_non_array_payload_raises():
    session = FakeSession([FakeResponse({"error": "nope"})])
    with pytest.raises(PolymarketError, match="array"):
        PolymarketSource(session=session).fetch(WALLET)
