"""Read-only client for the Polymarket US Portfolio API.

WHY THIS EXISTS
---------------
Polymarket US (the CFTC-regulated US platform) is NOT the crypto Polymarket.
US accounts have no wallet address at all -- funding is by card/bank -- so the
public `data-api.polymarket.com/positions?user=0x...` endpoint that
`polymarket_client.py` uses can never see a US user's positions. The only way
to read them is the authenticated Portfolio API.

THE SECURITY POSITION -- READ THIS BEFORE EDITING
-------------------------------------------------
Polymarket US does NOT offer read-only API keys. The same credential that reads
your positions can also PLACE AND CANCEL ORDERS. We cannot scope it down; that
is their design.

So the containment is structural, not a promise:

    THIS MODULE CONTAINS EXACTLY ONE REQUEST: GET /v1/portfolio/positions.

There is no order-placing code anywhere in this repository. Not behind a flag,
not commented out, not in a helper. A bug in this app therefore cannot trade --
not because we remembered to be careful, but because the capability does not
exist in the source. Deliberately NOT using the official SDK, whose client
exposes `orders.place()` on the same object as `portfolio.positions()`.

If you are about to add a POST, a PUT, an order, a cancel, or anything that
moves money: STOP. That is a different program. This one only ever reads.

The Ed25519 signing here signs an HTTP request for authentication (like an
HMAC). It does NOT sign a blockchain transaction and cannot move funds.

The secret is read from the environment, never stored, never logged, and never
interpolated into an exception message.
"""

from __future__ import annotations

import base64
import os
import time

import requests
from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from models import Position

BASE_URL = "https://api.polymarket.us"
POSITIONS_PATH = "/v1/portfolio/positions"
METHOD = "GET"

PAGE_LIMIT = 100  # the API's documented default/limit for this endpoint
MAX_PAGES = 200  # hard stop; a broken cursor must never loop forever
TIMEOUT_SECONDS = 15

KEY_ID_ENV = "POLYMARKET_US_KEY_ID"
SECRET_ENV = "POLYMARKET_US_SECRET_KEY"


class PolymarketUSError(RuntimeError):
    """The US API could not be reached, or rejected us, or answered oddly.

    Its message NEVER contains the secret, and never wraps the underlying
    exception's text (which could echo request headers).
    """


class MissingCredentialsError(PolymarketUSError):
    """No key id / secret configured."""


def _sign(secret_key: str, timestamp: str) -> str:
    """Ed25519 signature of `{timestamp}{METHOD}{PATH}`, base64-encoded.

    The secret is a base64 Ed25519 private key. Some encodings carry the 32-byte
    seed followed by the 32-byte public key; Ed25519PrivateKey wants the seed, so
    take the first 32 bytes.
    """
    try:
        raw = base64.b64decode(secret_key, validate=True)
    except Exception as exc:  # noqa: BLE001 - never surface the secret
        raise PolymarketUSError(
            f"{SECRET_ENV} is not valid base64. Copy it again from "
            "polymarket.us/developer."
        ) from None

    if len(raw) < 32:
        raise PolymarketUSError(
            f"{SECRET_ENV} is too short to be an Ed25519 key "
            f"({len(raw)} bytes decoded, need at least 32)."
        )

    try:
        private_key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    except (InvalidKey, ValueError):
        raise PolymarketUSError(
            f"{SECRET_ENV} is not a usable Ed25519 key."
        ) from None

    message = f"{timestamp}{METHOD}{POSITIONS_PATH}".encode()
    return base64.b64encode(private_key.sign(message)).decode()


class PolymarketUSSource:
    """Live PositionSource for Polymarket US. Satisfies models.PositionSource.

    Identity comes from the API key, not from a wallet, so `fetch(wallet)`
    ignores its argument -- the key already says who you are. The parameter
    exists only to satisfy the PositionSource protocol.
    """

    def __init__(
        self,
        key_id: str | None = None,
        secret_key: str | None = None,
        session: requests.Session | None = None,
        base_url: str = BASE_URL,
    ):
        self._key_id = key_id or os.environ.get(KEY_ID_ENV, "")
        self._secret = secret_key or os.environ.get(SECRET_ENV, "")
        self._session = session or requests.Session()
        self._base_url = base_url

        if not self._key_id or not self._secret:
            raise MissingCredentialsError(
                f"Polymarket US needs {KEY_ID_ENV} and {SECRET_ENV}. "
                "Copy .env.example to .env and paste them in "
                "(generate them at polymarket.us/developer)."
            )

    def _headers(self) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        return {
            "X-PM-Access-Key": self._key_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": _sign(self._secret, timestamp),
        }

    def fetch(self, wallet: str = "") -> list[Position]:  # noqa: ARG002
        """Every position in the account the key belongs to."""
        positions: list[Position] = []
        cursor: str | None = None

        for _ in range(MAX_PAGES):
            payload = self._fetch_page(cursor)

            raw_positions = payload.get("positions") or {}
            if not isinstance(raw_positions, dict):
                raise PolymarketUSError(
                    "Expected the US API to return positions keyed by market slug."
                )
            for slug, raw in raw_positions.items():
                if isinstance(raw, dict):
                    positions.append(Position.from_us_api(str(slug), raw))

            cursor = payload.get("nextCursor") or None
            if payload.get("eof") or not cursor:
                return positions

        raise PolymarketUSError(
            "Polymarket US kept returning pages; stopped to avoid looping forever."
        )

    def _fetch_page(self, cursor: str | None) -> dict:
        params: dict[str, object] = {"limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor

        try:
            response = self._session.get(
                self._base_url + POSITIONS_PATH,
                params=params,
                headers=self._headers(),
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            # Report only the failure KIND. The exception's text can echo the
            # request (headers included), and the signature/key must never leak.
            raise PolymarketUSError(
                f"Could not reach Polymarket US ({type(exc).__name__})."
            ) from None

        if response.status_code in (401, 403):
            raise PolymarketUSError(
                "Polymarket US rejected the API key. Check "
                f"{KEY_ID_ENV}/{SECRET_ENV} in .env, and that your computer's "
                "clock is correct (requests are rejected if the timestamp is "
                "more than 30 seconds off). If in doubt, revoke and regenerate "
                "the key at polymarket.us/developer."
            )
        if response.status_code == 429:
            raise PolymarketUSError(
                "Polymarket US rate limit hit. Wait a moment, then refresh again."
            )
        if response.status_code >= 500:
            raise PolymarketUSError(
                f"Polymarket US is having trouble (HTTP {response.status_code})."
            )
        if response.status_code != 200:
            raise PolymarketUSError(
                f"Unexpected response from Polymarket US "
                f"(HTTP {response.status_code})."
            )

        try:
            payload = response.json()
        except ValueError:
            raise PolymarketUSError(
                "Polymarket US returned a body that could not be read as JSON."
            ) from None

        if not isinstance(payload, dict):
            raise PolymarketUSError("Expected a JSON object from Polymarket US.")
        return payload
