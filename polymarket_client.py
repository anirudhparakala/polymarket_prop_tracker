"""Read-only client for the Polymarket Data API.

Issues GET requests only. Never signs, never trades, never sees a private key.
"""

from __future__ import annotations

import re

import requests

from models import Position

BASE_URL = "https://data-api.polymarket.com"
POSITIONS_PATH = "/positions"

# API default is 100 (max 500). Without full pagination, position 101 vanishes
# and the comparison reports it Closed.
PAGE_LIMIT = 500

# API caps offset at 10000.
MAX_OFFSET = 10_000

TIMEOUT_SECONDS = 10

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


class PolymarketError(RuntimeError):
    """The API could not be reached, or answered with something unusable."""


class InvalidWalletError(ValueError):
    """The wallet address is not a 0x-prefixed 40-hex-character address."""


def validate_wallet(wallet: str) -> str:
    if not isinstance(wallet, str) or not WALLET_RE.match(wallet):
        raise InvalidWalletError(
            f"Not a valid wallet address: {wallet!r}. "
            "Expected 0x followed by 40 hex characters."
        )
    return wallet


class PolymarketSource:
    """Live PositionSource. Satisfies models.PositionSource."""

    def __init__(self, session: requests.Session | None = None, base_url: str = BASE_URL):
        self._session = session or requests.Session()
        self._base_url = base_url

    def fetch(self, wallet: str) -> list[Position]:
        validate_wallet(wallet)
        return [Position.from_api(raw) for raw in self._fetch_all_pages(wallet)]

    def _fetch_all_pages(self, wallet: str) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        while True:
            page = self._fetch_page(wallet, offset)
            rows.extend(page)
            if len(page) < PAGE_LIMIT or offset >= MAX_OFFSET:
                return rows
            offset += PAGE_LIMIT

    def _fetch_page(self, wallet: str, offset: int) -> list[dict]:
        params = {
            "user": wallet,
            # Explicit 0: the API default of 1 drops sub-1-share positions.
            "sizeThreshold": 0,
            "limit": PAGE_LIMIT,
            "offset": offset,
        }
        try:
            response = self._session.get(
                self._base_url + POSITIONS_PATH,
                params=params,
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise PolymarketError(f"Could not reach Polymarket: {exc}") from exc

        if response.status_code == 429:
            raise PolymarketError(
                "Polymarket rate limit hit. Wait a moment, then refresh again."
            )
        if response.status_code >= 500:
            raise PolymarketError(
                f"Polymarket is having trouble (HTTP {response.status_code}). "
                "Try again shortly."
            )
        if response.status_code != 200:
            raise PolymarketError(
                f"Unexpected response from Polymarket (HTTP {response.status_code})."
            )

        payload = response.json()
        if not isinstance(payload, list):
            raise PolymarketError("Expected a JSON array of positions.")
        return payload
