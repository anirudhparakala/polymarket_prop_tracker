"""Tests for the read-only Polymarket US client.

No network: every test drives a fake session. The Ed25519 key is generated in
the test itself -- never a real credential.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import polymarket_us_client as us
from models import Position
from polymarket_us_client import (
    MissingCredentialsError,
    PolymarketUSError,
    PolymarketUSSource,
)

KEY_ID = "3f8a1c2e-9b4d-4a17-8e0f-5c6d7e8f9a0b"


@pytest.fixture
def keypair():
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return key, base64.b64encode(seed).decode()


class FakeResponse:
    def __init__(self, payload=None, status_code=200, bad_json=False):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _position_payload(slug="fra-2-1-esp", net="40", cost="12.00", cash="30.00"):
    return {
        "positions": {
            slug: {
                "netPositionDecimal": net,
                "qtyBoughtDecimal": "40",
                "cost": {"value": cost, "currency": "USD"},
                "cashValue": {"value": cash, "currency": "USD"},
                "realized": {"value": "0", "currency": "USD"},
                "expired": False,
                "marketMetadata": {
                    "slug": slug,
                    "title": "Exact Score: France 2 - 1 Spain?",
                    "outcome": "Yes",
                    "eventSlug": "fifwc-fra-esp-2026-07-14-exact-score",
                },
            }
        },
        "eof": True,
    }


def _source(session, secret):
    return PolymarketUSSource(key_id=KEY_ID, secret_key=secret, session=session)


# --- the structural guarantee: this app CANNOT trade -----------------------


def test_module_contains_only_one_request_and_only_the_positions_path():
    """The containment is structural, not a promise. Polymarket US has no
    read-only key -- the same credential can place orders -- so the guarantee
    is that no order-placing code exists at all."""
    src = Path(us.__file__).read_text(encoding="utf-8")

    verbs = set(re.findall(r"self\._session\.(\w+)\(", src))
    assert verbs == {"get"}, f"only GET may exist, found: {verbs}"

    paths = set(re.findall(r'"(/v1/[^"]*)"', src))
    assert paths == {"/v1/portfolio/positions"}, f"unexpected paths: {paths}"


def test_no_order_placing_code_anywhere_in_the_app():
    root = Path(us.__file__).parent
    for module in ("app.py", "polymarket_us_client.py", "polymarket_client.py"):
        src = (root / module).read_text(encoding="utf-8").lower()
        for forbidden in ("session.post(", "session.put(", "session.delete("):
            assert forbidden not in src, f"{module} contains {forbidden}"


# --- auth -------------------------------------------------------------------


def test_signature_verifies_against_timestamp_method_path(keypair):
    key, secret = keypair
    session = FakeSession([FakeResponse(_position_payload())])
    _source(session, secret).fetch()

    headers = session.calls[0]["headers"]
    assert headers["X-PM-Access-Key"] == KEY_ID

    message = f"{headers['X-PM-Timestamp']}GET/v1/portfolio/positions".encode()
    # Raises if the signature is wrong -- this is real verification, not a mock.
    key.public_key().verify(base64.b64decode(headers["X-PM-Signature"]), message)


def test_missing_credentials_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("POLYMARKET_US_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_SECRET_KEY", raising=False)
    with pytest.raises(MissingCredentialsError, match="POLYMARKET_US_KEY_ID"):
        PolymarketUSSource()


def test_a_malformed_secret_never_leaks_into_the_error(keypair):
    with pytest.raises(PolymarketUSError) as exc:
        PolymarketUSSource(key_id=KEY_ID, secret_key="!!!not-base64!!!").fetch()
    assert "!!!not-base64!!!" not in str(exc.value)


def test_rejected_key_gives_actionable_message(keypair):
    _, secret = keypair
    session = FakeSession([FakeResponse(status_code=401)])
    with pytest.raises(PolymarketUSError, match="rejected the API key"):
        _source(session, secret).fetch()


# --- normalization ----------------------------------------------------------


def test_stake_is_cost_in_dollars_not_the_share_count(keypair):
    _, secret = keypair
    session = FakeSession([FakeResponse(_position_payload())])
    (p,) = _source(session, secret).fetch()

    assert isinstance(p, Position)
    assert p.stake == 12.00  # cost, in dollars
    assert p.stake != 40.0  # NOT qtyBought/netPosition, which are SHARES
    assert p.size == 40.0
    assert p.current_value == 30.00
    assert p.open_pnl == 18.00
    assert p.market_title == "Exact Score: France 2 - 1 Spain?"
    assert p.outcome == "Yes"


def test_prices_are_derived_and_a_zero_share_count_does_not_explode(keypair):
    _, secret = keypair
    session = FakeSession([FakeResponse(_position_payload())])
    (p,) = _source(session, secret).fetch()
    assert p.entry_price == pytest.approx(12.0 / 40.0)
    assert p.current_price == pytest.approx(30.0 / 40.0)

    session = FakeSession(
        [FakeResponse(_position_payload(net="0", cost="0", cash="0"))]
    )
    (zero,) = _source(session, secret).fetch()
    assert zero.entry_price == 0.0
    assert zero.current_price == 0.0  # not a ZeroDivisionError, not an Inf


def test_the_market_slug_is_the_join_key(keypair):
    _, secret = keypair
    session = FakeSession([FakeResponse(_position_payload(slug="arg-win-wc"))])
    (p,) = _source(session, secret).fetch()
    assert p.asset == "arg-win-wc"


def test_empty_portfolio_is_an_empty_list_not_an_error(keypair):
    _, secret = keypair
    session = FakeSession([FakeResponse({"positions": {}, "eof": True})])
    assert _source(session, secret).fetch() == []


# --- pagination / transport -------------------------------------------------


def test_follows_the_cursor_until_eof(keypair):
    _, secret = keypair
    page1 = _position_payload(slug="a")
    page1["eof"] = False
    page1["nextCursor"] = "CURSOR2"
    page2 = _position_payload(slug="b")

    session = FakeSession([FakeResponse(page1), FakeResponse(page2)])
    positions = _source(session, secret).fetch()

    assert {p.asset for p in positions} == {"a", "b"}
    assert session.calls[1]["params"]["cursor"] == "CURSOR2"


def test_a_cursor_that_never_ends_is_bounded(keypair, monkeypatch):
    _, secret = keypair
    monkeypatch.setattr(us, "MAX_PAGES", 3)
    endless = _position_payload()
    endless["eof"] = False
    endless["nextCursor"] = "SAME"
    session = FakeSession([FakeResponse(endless) for _ in range(10)])
    with pytest.raises(PolymarketUSError, match="looping forever"):
        _source(session, secret).fetch()


def test_network_failure_reports_the_kind_not_the_request(keypair):
    _, secret = keypair
    session = FakeSession([requests.ConnectionError("boom https://api...?key=leak")])
    with pytest.raises(PolymarketUSError) as exc:
        _source(session, secret).fetch()
    assert "ConnectionError" in str(exc.value)
    assert "leak" not in str(exc.value)  # the request text never surfaces


def test_non_json_body_raises_polymarket_us_error(keypair):
    _, secret = keypair
    session = FakeSession([FakeResponse(bad_json=True)])
    with pytest.raises(PolymarketUSError, match="could not be read as JSON"):
        _source(session, secret).fetch()
