"""Print the RAW Polymarket US position payload, so the field mapping can be
checked against reality instead of against the docs.

    .venv/Scripts/python.exe scripts/dump_us_raw.py

Read-only, one request. Your secret is never printed.
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polymarket_us_client as us  # noqa: E402


def main() -> int:
    load_dotenv()
    source = us.PolymarketUSSource()
    payload = source._fetch_page(None)  # noqa: SLF001 - deliberate: raw shape

    positions = payload.get("positions") or {}
    print(f"{len(positions)} position(s). Raw shape:\n")
    for slug, raw in positions.items():
        print("=" * 70)
        print(f"KEY (slug): {slug}")
        print(json.dumps(raw, indent=2, sort_keys=True))
    print("=" * 70)
    print("\ntop-level keys:", sorted(payload.keys()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
