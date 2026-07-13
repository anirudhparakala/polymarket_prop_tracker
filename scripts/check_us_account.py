"""Verify your Polymarket US credentials and the field mapping.

    .venv/Scripts/python.exe scripts/check_us_account.py

Reads POLYMARKET_US_KEY_ID / POLYMARKET_US_SECRET_KEY from .env (gitignored).
Read-only: it makes exactly one request, GET /v1/portfolio/positions.

WHY RUN THIS FIRST
------------------
The US API has no price field, so the dashboard DERIVES prices and maps
`cost` -> your stake. That mapping is reasoned, not guessed -- but on the crypto
side an equally reasonable-looking guess once produced a -$103,707 "loss" on a
winning position. So check these numbers against what your Polymarket app shows
before you trust the dashboard with real money.

Nothing is saved. Your secret is never printed.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polymarket_us_client import (  # noqa: E402
    MissingCredentialsError,
    PolymarketUSError,
    PolymarketUSSource,
)


def main() -> int:
    load_dotenv()

    try:
        source = PolymarketUSSource()
    except MissingCredentialsError as exc:
        print(f"{exc}\n")
        print("Copy .env.example to .env and fill in SECTION A.")
        return 2

    try:
        positions = source.fetch()
    except PolymarketUSError as exc:
        print(f"Could not read your positions:\n\n  {exc}\n")
        return 1

    if not positions:
        print("Connected fine -- but the account has no open positions.")
        return 0

    print(f"Connected. {len(positions)} open position(s).\n")
    print("Check these against your Polymarket app. They must MATCH.\n")

    total_stake = sum(p.stake for p in positions)
    total_value = sum(p.current_value for p in positions)

    for p in sorted(positions, key=lambda p: p.current_value, reverse=True):
        print(f"  {p.market_title[:58]}")
        print(f"      outcome     : {p.outcome}")
        print(f"      shares held : {p.size:,.2f}")
        print(f"      you paid    : ${p.stake:,.2f}      <- 'stake' (from cost)")
        print(f"      worth now   : ${p.current_value:,.2f}      <- (from cashValue)")
        print(f"      open P&L    : ${p.open_pnl:+,.2f}")
        print(f"      realized    : ${p.realized_pnl:+,.2f}")
        print(f"      price now   : {p.current_price:.4f}   <- DERIVED: value / shares")
        print()

    print(f"  TOTAL paid : ${total_stake:,.2f}")
    print(f"  TOTAL now  : ${total_value:,.2f}")
    print(f"  TOTAL P&L  : ${total_value - total_stake:+,.2f}")
    print()
    print("If 'you paid' / 'worth now' / 'price now' match your app, the mapping")
    print("is right and you can trust the dashboard. If any number looks wrong,")
    print("STOP and say so -- do not trust the dashboard until it is fixed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
