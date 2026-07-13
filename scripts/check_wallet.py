"""Check whether an address is the Polymarket wallet holding your positions.

Read-only. Hits the same public endpoint the dashboard uses. No key, no secret,
no signing -- the positions endpoint needs no authentication at all.

    .venv/Scripts/python.exe scripts/check_wallet.py 0xYourAddressHere

If it prints your open bets, that address is the one to paste into the app.
If it prints 0 positions, it's probably your MetaMask/EOA address rather than
the Polymarket proxy wallet that actually holds the positions.

The address is never written to disk or committed -- it is only passed as an
argument to this one command.
"""

from __future__ import annotations

import re
import sys

import requests

URL = "https://data-api.polymarket.com/positions"
WALLET_RE = re.compile(r"^0x[0-9a-f]{40}$")


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: check_wallet.py 0x<40 hex chars>\n")
        return 2

    wallet = sys.argv[1].strip().lower()
    if not WALLET_RE.fullmatch(wallet):
        sys.stderr.write(
            f"Not a valid address: {sys.argv[1]!r}\n"
            "Expected 0x followed by 40 hex characters.\n"
        )
        return 2

    try:
        response = requests.get(
            URL,
            params={"user": wallet, "sizeThreshold": 0, "limit": 500},
            timeout=15,
        )
    except requests.RequestException as exc:
        sys.stderr.write(f"Could not reach Polymarket ({type(exc).__name__}).\n")
        return 1

    if response.status_code != 200:
        sys.stderr.write(f"Polymarket returned HTTP {response.status_code}.\n")
        return 1

    positions = response.json()
    if not isinstance(positions, list):
        sys.stderr.write("Unexpected response shape.\n")
        return 1

    if not positions:
        print("0 open positions for this address.")
        print()
        print("If you know you have live bets, this is the wrong address -- most")
        print("likely your MetaMask/EOA rather than the Polymarket proxy wallet.")
        print("Use the address on your Polymarket profile / deposit screen.")
        return 0

    total = sum(float(p.get("currentValue") or 0) for p in positions)
    print(f"{len(positions)} open position(s), worth ${total:,.2f} right now:\n")
    for p in sorted(
        positions, key=lambda p: float(p.get("currentValue") or 0), reverse=True
    )[:15]:
        value = float(p.get("currentValue") or 0)
        pnl = float(p.get("cashPnl") or 0)
        print(
            f"  {p.get('title', '?')[:44]:44} {str(p.get('outcome', '?')):>4}"
            f"  ${value:>9,.2f}  ({pnl:+,.2f})"
        )
    if len(positions) > 15:
        print(f"  ... and {len(positions) - 15} more")

    print("\nIf these are your bets, paste this address into the app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
