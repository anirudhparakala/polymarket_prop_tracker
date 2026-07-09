# Polymarket Prop Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local Streamlit dashboard that shows how each individual Polymarket prop position has moved since a user-saved checkpoint, correctly distinguishing a cashout from a market loss.

**Architecture:** A dependency DAG rooted at `models.py`, which defines frozen dataclasses and a `PositionSource` protocol. Two interchangeable sources (live HTTP, JSON fixtures) feed a pure comparison module that knows nothing about network or database. Phase 0 freezes the contracts; Phase 1 then builds four modules in parallel with zero shared files.

**Tech Stack:** Python 3.13, Streamlit 1.59, pandas 3.0.3, requests 2.34, SQLite (stdlib `sqlite3`), pytest 9.

**Design spec:** [docs/superpowers/specs/2026-07-09-polymarket-prop-tracker-design.md](../specs/2026-07-09-polymarket-prop-tracker-design.md)

## Global Constraints

Every task's requirements implicitly include this section.

- **Python interpreter is always `.venv/Scripts/python.exe`.** Never bare `python`. This machine has four Pythons on PATH.
- **`pandas.Styler.applymap` does not exist.** pandas 3.0.3 removed it, along with `DataFrame.applymap`. Use `Styler.map`. Nearly every example and recalled snippet uses the removed name.
- **Never commit a wallet address.** Fixtures and tests use `0x` + 40 zeros. A pre-commit hook (`scripts/check_no_secrets.py`) blocks real addresses. Never use `git commit --no-verify`.
- **Read-only.** No trading, order placement, signing, or private keys. The app issues `GET` requests only.
- **No advice text in the UI.** Numbers only. Never "cash out now", "hold", "good bet".
- **`stake = initialValue`.** `open_pnl = currentValue - initialValue`, which equals the API's `cashPnl`. `totalBought` is a **share count**, not dollars — never use it as a stake.
- **The client always sends `sizeThreshold=0`** (API default is `1`, which silently drops sub-1-share positions) **and paginates with `limit=500` + `offset`** (API default is `100`). Both defaults manufacture phantom `Closed` rows.
- **Join on `asset`.** Never on `title`, `outcome`, or `condition_id` — one `condition_id` spans many outcomes.
- **Status derives from `size`, never from value.** Float sizes compare with `math.isclose(rel_tol=1e-9)`, never `==`.
- **Modules live at the repo root**, flat: `models.py`, `db.py`, etc. Tests live in `tests/`.
- API base URL: `https://data-api.polymarket.com`, path `/positions`, returns a **bare JSON array**.

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `models.py` | Frozen dataclasses, `Status` enum, `PositionSource` protocol, `Position.from_api` | 0 |
| `schema.sql` | The three tables, as a frozen contract | 0 |
| `conftest.py` | Puts repo root on `sys.path` for pytest | 0 |
| `polymarket_client.py` | `PolymarketSource`: HTTP, pagination, wallet validation, error mapping | 1 |
| `fixtures.py` | `FixtureSource`: loads JSON scenarios | 1 |
| `db.py` | `init_db`, settings/checkpoint CRUD. **All SQL lives here.** | 1 |
| `calculations.py` | Pure: `compare`, `sort_rows`, `summarize` | 1 |
| `ui.py` | `render_summary`, `render_table`, coloring | 2 |
| `app.py` | Streamlit entrypoint, session state, wiring | 2 |
| `tests/fixtures/*.json` | Raw-API-shaped scenario data | 1 |
| `README.md` | Setup for the user and friends | 3 |

Phase 1's four tasks touch four disjoint file sets. No agent edits a sibling's file. If one needs to, the boundary was drawn wrong — stop and report.

---

# Phase 0 — Frozen Contracts (serial, must land first)

## Task 1: Data models and the source protocol

**Files:**
- Create: `models.py`
- Create: `conftest.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Position`, `CheckpointRow`, `Status`, `Row`, `Summary`, `PositionSource`. `Position.from_api(raw: dict) -> Position`. `CheckpointRow.from_position(p: Position) -> CheckpointRow`. Every later task imports from here and none may change it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
import math

from models import CheckpointRow, Position, Status

# Polymarket's own documented example row, used to pin field semantics.
RAW = {
    "asset": "71321045679252212594626385532706912750332728571942532289631379312455583992563",
    "conditionId": "0xd007d71fd17b0913b9d7ff198f617caa96a9e4aab1bed7d6f9abd76bb17dd507",
    "title": "Will Morocco win?",
    "eventSlug": "morocco-france",
    "outcome": "Yes",
    "size": 90548.087076,
    "avgPrice": 0.020628,
    "initialValue": 1867.825940203728,
    "currentValue": 5840.351616402,
    "cashPnl": 3972.525676198273,
    "percentPnl": 212.6817917393834,
    "totalBought": 109548.077076,
    "realizedPnl": -894.398503,
    "curPrice": 0.0645,
    "redeemable": False,
    "endDate": "2024-11-05",
}


def test_from_api_maps_stake_to_initial_value_not_total_bought():
    p = Position.from_api(RAW)
    assert p.stake == RAW["initialValue"]
    # totalBought is a SHARE COUNT. Using it as stake yields -103707 open_pnl.
    assert p.stake != RAW["totalBought"]


def test_from_api_open_pnl_equals_cash_pnl():
    p = Position.from_api(RAW)
    assert math.isclose(p.open_pnl, RAW["cashPnl"], rel_tol=1e-9)
    assert math.isclose(p.open_pnl, p.current_value - p.stake, rel_tol=1e-9)


def test_from_api_renames_fields_to_internal_shape():
    p = Position.from_api(RAW)
    assert p.market_title == "Will Morocco win?"
    assert p.entry_price == RAW["avgPrice"]
    assert p.current_price == RAW["curPrice"]
    assert p.event_slug == "morocco-france"


def test_from_api_tolerates_missing_fields():
    p = Position.from_api({"asset": "abc"})
    assert p.asset == "abc"
    assert p.size == 0.0
    assert p.market_title == ""
    assert p.redeemable is False


def test_position_is_frozen():
    p = Position.from_api(RAW)
    try:
        p.size = 1.0
    except Exception:
        return
    raise AssertionError("Position must be immutable")


def test_checkpoint_row_from_position_round_trips_join_key():
    p = Position.from_api(RAW)
    c = CheckpointRow.from_position(p)
    assert c.asset == p.asset
    assert c.current_value == p.current_value
    assert c.size == p.size


def test_status_values_are_the_five_documented_labels():
    assert {s.value for s in Status} == {
        "Open",
        "Reduced",
        "Increased",
        "Closed",
        "New",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'models'`

- [ ] **Step 3: Write minimal implementation**

Create `conftest.py` at the repo root (its presence puts the root on `sys.path` for pytest):

```python
"""Present so pytest adds the repo root to sys.path; modules live flat at root."""
```

Create `models.py`:

```python
"""Frozen data contracts shared by every module.

Phase 0 artifact. Once this lands, Phase 1 agents code against it in parallel
and none of them may change it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


def _f(raw: dict, key: str, default: float = 0.0) -> float:
    value = raw.get(key)
    return default if value is None else float(value)


def _s(raw: dict, key: str, default: str = "") -> str:
    value = raw.get(key)
    return default if value is None else str(value)


@dataclass(frozen=True, slots=True)
class Position:
    """A live position, normalized. Raw API names never escape this class."""

    asset: str
    condition_id: str
    market_title: str
    event_slug: str
    outcome: str
    size: float
    entry_price: float
    current_price: float
    stake: float
    current_value: float
    open_pnl: float
    percent_pnl: float
    realized_pnl: float
    redeemable: bool
    end_date: str

    @classmethod
    def from_api(cls, raw: dict) -> Position:
        # stake is initialValue (dollars), NOT totalBought (shares).
        stake = _f(raw, "initialValue")
        current_value = _f(raw, "currentValue")
        return cls(
            asset=_s(raw, "asset"),
            condition_id=_s(raw, "conditionId"),
            market_title=_s(raw, "title"),
            event_slug=_s(raw, "eventSlug"),
            outcome=_s(raw, "outcome"),
            size=_f(raw, "size"),
            entry_price=_f(raw, "avgPrice"),
            current_price=_f(raw, "curPrice"),
            stake=stake,
            current_value=current_value,
            open_pnl=current_value - stake,
            percent_pnl=_f(raw, "percentPnl"),
            realized_pnl=_f(raw, "realizedPnl"),
            redeemable=bool(raw.get("redeemable", False)),
            end_date=_s(raw, "endDate"),
        )


@dataclass(frozen=True, slots=True)
class CheckpointRow:
    """One prop as it stood at a saved checkpoint."""

    asset: str
    condition_id: str
    market_title: str
    event_slug: str
    outcome: str
    size: float
    entry_price: float
    current_price: float
    stake: float
    current_value: float
    open_pnl: float
    percent_pnl: float
    realized_pnl: float

    @classmethod
    def from_position(cls, p: Position) -> CheckpointRow:
        return cls(
            asset=p.asset,
            condition_id=p.condition_id,
            market_title=p.market_title,
            event_slug=p.event_slug,
            outcome=p.outcome,
            size=p.size,
            entry_price=p.entry_price,
            current_price=p.current_price,
            stake=p.stake,
            current_value=p.current_value,
            open_pnl=p.open_pnl,
            percent_pnl=p.percent_pnl,
            realized_pnl=p.realized_pnl,
        )


class Status(str, Enum):
    OPEN = "Open"
    REDUCED = "Reduced"
    INCREASED = "Increased"
    CLOSED = "Closed"
    NEW = "New"


@dataclass(frozen=True, slots=True)
class Row:
    """One rendered table row. `None` means "the app does not know"."""

    asset: str
    market_title: str
    outcome: str
    status: Status
    stake: float | None
    checkpoint_value: float | None
    current_value: float | None
    change_since_checkpoint: float | None
    since_entry: float | None
    realized_pnl: float | None
    checkpoint_price: float | None
    current_price: float | None
    price_change: float | None
    checkpoint_size: float | None
    current_size: float | None
    size_change: float | None
    size_change_percent: float | None


@dataclass(frozen=True, slots=True)
class Summary:
    open_positions: int
    total_stake: float
    current_value: float
    open_pnl: float


class PositionSource(Protocol):
    """Implemented by PolymarketSource (HTTP) and FixtureSource (JSON)."""

    def fetch(self, wallet: str) -> list[Position]: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_models.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add models.py conftest.py tests/test_models.py
git commit -m "feat: add frozen data models and PositionSource protocol"
```

---

## Task 2: Database schema

**Files:**
- Create: `schema.sql`
- Test: `tests/test_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `schema.sql`, executed via `sqlite3.Connection.executescript`. Table and column names are frozen; Task 5 (`db.py`) reads them and Task 8 depends on `checkpoints.wallet_address`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schema.py`:

```python
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).parent.parent / "schema.sql"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_three_tables_exist():
    conn = _conn()
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"settings", "checkpoints", "checkpoint_positions"} <= names


def test_checkpoints_are_scoped_to_a_wallet():
    conn = _conn()
    assert "wallet_address" in _columns(conn, "checkpoints")


def test_checkpoint_positions_has_every_documented_column():
    conn = _conn()
    assert _columns(conn, "checkpoint_positions") >= {
        "id", "checkpoint_id", "asset", "condition_id", "title", "event_slug",
        "outcome", "size", "avg_price", "stake", "current_value", "cur_price",
        "cash_pnl", "percent_pnl", "realized_pnl", "created_at",
    }


def test_one_row_per_asset_per_checkpoint_is_enforced():
    conn = _conn()
    conn.execute("INSERT INTO checkpoints (wallet_address, label) VALUES ('0x0', 'a')")
    args = (1, "AST", "0xc", "t", "e", "Yes", 1.0, 0.5, 0.5, 0.5, 0.5, 0.0, 0.0, 0.0)
    sql = (
        "INSERT INTO checkpoint_positions (checkpoint_id, asset, condition_id, title,"
        " event_slug, outcome, size, avg_price, stake, current_value, cur_price,"
        " cash_pnl, percent_pnl, realized_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    conn.execute(sql, args)
    try:
        conn.execute(sql, args)
    except sqlite3.IntegrityError:
        return
    raise AssertionError("duplicate (checkpoint_id, asset) must be rejected")


def test_deleting_a_checkpoint_cascades_to_its_positions():
    conn = _conn()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO checkpoints (wallet_address, label) VALUES ('0x0', 'a')")
    conn.execute(
        "INSERT INTO checkpoint_positions (checkpoint_id, asset, condition_id, title,"
        " event_slug, outcome, size, avg_price, stake, current_value, cur_price,"
        " cash_pnl, percent_pnl, realized_pnl)"
        " VALUES (1,'AST','0xc','t','e','Yes',1.0,0.5,0.5,0.5,0.5,0.0,0.0,0.0)"
    )
    conn.execute("DELETE FROM checkpoints WHERE id = 1")
    remaining = conn.execute("SELECT COUNT(*) FROM checkpoint_positions").fetchone()[0]
    assert remaining == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_schema.py -v`
Expected: FAIL — `FileNotFoundError: schema.sql`

- [ ] **Step 3: Write minimal implementation**

Create `schema.sql`:

```sql
-- Frozen contract. db.py is the only module allowed to execute SQL against it.

CREATE TABLE IF NOT EXISTS settings (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    wallet_address    TEXT    NOT NULL,
    starting_bankroll REAL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- wallet_address is what makes cross-wallet comparison impossible by
-- construction. Without it, pasting a second wallet renders every old row
-- Closed and every new row New, with all numbers technically correct.
CREATE TABLE IF NOT EXISTS checkpoints (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT    NOT NULL,
    label          TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_wallet
    ON checkpoints (wallet_address);

CREATE TABLE IF NOT EXISTS checkpoint_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id INTEGER NOT NULL REFERENCES checkpoints (id) ON DELETE CASCADE,
    asset         TEXT    NOT NULL,
    condition_id  TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    event_slug    TEXT    NOT NULL,
    outcome       TEXT    NOT NULL,
    size          REAL    NOT NULL,
    avg_price     REAL    NOT NULL,
    stake         REAL    NOT NULL,
    current_value REAL    NOT NULL,
    cur_price     REAL    NOT NULL,
    cash_pnl      REAL    NOT NULL,
    percent_pnl   REAL    NOT NULL,
    realized_pnl  REAL    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cp_positions_checkpoint
    ON checkpoint_positions (checkpoint_id);

-- asset is the join key; a checkpoint holds exactly one row per prop.
CREATE UNIQUE INDEX IF NOT EXISTS idx_cp_positions_asset
    ON checkpoint_positions (checkpoint_id, asset);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_schema.py -v`
Expected: PASS, 5 passed

Note: `test_deleting_a_checkpoint_cascades_to_its_positions` only passes because the test issues `PRAGMA foreign_keys = ON`. SQLite defaults it **off** per connection. Task 5 must issue it in `init_db`.

- [ ] **Step 5: Commit**

```bash
git add schema.sql tests/test_schema.py
git commit -m "feat: add SQLite schema with wallet-scoped checkpoints"
```

---

# Phase 1 — Four parallel agents (no shared files)

Dispatch Tasks 3, 4, 5, 6 concurrently. Each imports `models.py` and nothing from its siblings.

## Task 3: Polymarket API client

**Files:**
- Create: `polymarket_client.py`
- Test: `tests/test_polymarket_client.py`

**Interfaces:**
- Consumes: `models.Position`.
- Produces: `PolymarketSource(session=None, base_url=BASE_URL)` with `.fetch(wallet) -> list[Position]`; `validate_wallet(wallet) -> str`; exceptions `InvalidWalletError`, `PolymarketError`; constants `BASE_URL`, `PAGE_LIMIT = 500`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_polymarket_client.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_polymarket_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polymarket_client'`

- [ ] **Step 3: Write minimal implementation**

Create `polymarket_client.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_polymarket_client.py -v`
Expected: PASS, 17 passed (the parametrized wallet test contributes 5)

- [ ] **Step 5: Commit**

```bash
git add polymarket_client.py tests/test_polymarket_client.py
git commit -m "feat: add read-only Polymarket client with pagination and sizeThreshold=0"
```

---

## Task 4: Fixture source and scenario data

**Files:**
- Create: `fixtures.py`
- Create: `tests/fixtures/before_match.json`
- Create: `tests/fixtures/after_goal.json`
- Create: `tests/fixtures/after_cashout.json`
- Test: `tests/test_fixtures.py`

**Interfaces:**
- Consumes: `models.Position`.
- Produces: `FixtureSource(scenario: str, fixture_dir: Path | None = None)` with `.fetch(wallet) -> list[Position]`; `SCENARIOS: tuple[str, ...]` equal to `("before_match", "after_goal", "after_cashout")`.

Fixture files hold **raw API shape** (a bare JSON array with `curPrice`, `initialValue`, …) so they exercise `Position.from_api` exactly as live data does.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fixtures.py`:

```python
import math

import pytest

from fixtures import SCENARIOS, FixtureSource

WALLET = "0x" + "0" * 40


def _by_title(scenario: str) -> dict[str, object]:
    return {p.market_title: p for p in FixtureSource(scenario).fetch(WALLET)}


def test_scenarios_are_the_three_documented_ones():
    assert SCENARIOS == ("before_match", "after_goal", "after_cashout")


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError, match="Unknown scenario"):
        FixtureSource("nonsense")


def test_before_match_has_three_props_each_worth_its_stake():
    positions = _by_title("before_match")
    assert len(positions) == 3
    for p in positions.values():
        assert math.isclose(p.current_value, p.stake, rel_tol=1e-9)
        assert math.isclose(p.open_pnl, 0.0, abs_tol=1e-9)


def test_before_match_values_match_the_spec():
    positions = _by_title("before_match")
    assert positions["Morocco wins"].stake == 5.0
    assert positions["Morocco wins"].size == 10.0
    assert positions["0-0 first half"].stake == 2.0
    assert positions["France 2-1"].stake == 5.0
    assert positions["France 2-1"].size == 25.0


def test_after_goal_moves_values_as_the_spec_describes():
    positions = _by_title("after_goal")
    assert math.isclose(positions["Morocco wins"].current_value, 10.0, rel_tol=1e-9)
    assert math.isclose(positions["0-0 first half"].current_value, 0.0, abs_tol=1e-9)
    assert math.isclose(positions["France 2-1"].current_value, 3.0, rel_tol=1e-9)


def test_after_cashout_drops_morocco_entirely():
    # A fully cashed-out position disappears from /positions. It does not
    # linger with size 0.
    positions = _by_title("after_cashout")
    assert "Morocco wins" not in positions
    assert len(positions) == 2


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_fixture_obeys_size_times_price_equals_value(scenario):
    for p in FixtureSource(scenario).fetch(WALLET):
        assert math.isclose(p.size * p.current_price, p.current_value, abs_tol=1e-6)
        assert math.isclose(p.size * p.entry_price, p.stake, abs_tol=1e-6)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_fixture_obeys_cash_pnl_equals_current_minus_initial(scenario):
    # Guard test. If Polymarket ever changes these semantics, this fails loudly
    # instead of the dashboard quietly lying.
    for p in FixtureSource(scenario).fetch(WALLET):
        assert math.isclose(p.open_pnl, p.current_value - p.stake, abs_tol=1e-9)


def test_fetch_ignores_the_wallet_argument():
    assert FixtureSource("before_match").fetch("anything") == FixtureSource(
        "before_match"
    ).fetch(WALLET)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fixtures.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fixtures'`

- [ ] **Step 3: Write minimal implementation**

Create `tests/fixtures/before_match.json`:

```json
[
  {
    "asset": "10000000000000000000000000000000000000000000000000000000000000001",
    "conditionId": "0x1111111111111111111111111111111111111111111111111111111111111111",
    "title": "Morocco wins",
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
    "redeemable": false,
    "endDate": "2026-07-09"
  },
  {
    "asset": "10000000000000000000000000000000000000000000000000000000000000002",
    "conditionId": "0x2222222222222222222222222222222222222222222222222222222222222222",
    "title": "0-0 first half",
    "eventSlug": "morocco-france",
    "outcome": "Yes",
    "size": 5.0,
    "avgPrice": 0.4,
    "initialValue": 2.0,
    "currentValue": 2.0,
    "cashPnl": 0.0,
    "percentPnl": 0.0,
    "totalBought": 5.0,
    "realizedPnl": 0.0,
    "curPrice": 0.4,
    "redeemable": false,
    "endDate": "2026-07-09"
  },
  {
    "asset": "10000000000000000000000000000000000000000000000000000000000000003",
    "conditionId": "0x3333333333333333333333333333333333333333333333333333333333333333",
    "title": "France 2-1",
    "eventSlug": "morocco-france",
    "outcome": "Yes",
    "size": 25.0,
    "avgPrice": 0.2,
    "initialValue": 5.0,
    "currentValue": 5.0,
    "cashPnl": 0.0,
    "percentPnl": 0.0,
    "totalBought": 25.0,
    "realizedPnl": 0.0,
    "curPrice": 0.2,
    "redeemable": false,
    "endDate": "2026-07-09"
  }
]
```

Create `tests/fixtures/after_goal.json` (same assets; prices moved):

```json
[
  {
    "asset": "10000000000000000000000000000000000000000000000000000000000000001",
    "conditionId": "0x1111111111111111111111111111111111111111111111111111111111111111",
    "title": "Morocco wins",
    "eventSlug": "morocco-france",
    "outcome": "Yes",
    "size": 10.0,
    "avgPrice": 0.5,
    "initialValue": 5.0,
    "currentValue": 10.0,
    "cashPnl": 5.0,
    "percentPnl": 100.0,
    "totalBought": 10.0,
    "realizedPnl": 0.0,
    "curPrice": 1.0,
    "redeemable": false,
    "endDate": "2026-07-09"
  },
  {
    "asset": "10000000000000000000000000000000000000000000000000000000000000002",
    "conditionId": "0x2222222222222222222222222222222222222222222222222222222222222222",
    "title": "0-0 first half",
    "eventSlug": "morocco-france",
    "outcome": "Yes",
    "size": 5.0,
    "avgPrice": 0.4,
    "initialValue": 2.0,
    "currentValue": 0.0,
    "cashPnl": -2.0,
    "percentPnl": -100.0,
    "totalBought": 5.0,
    "realizedPnl": 0.0,
    "curPrice": 0.0,
    "redeemable": false,
    "endDate": "2026-07-09"
  },
  {
    "asset": "10000000000000000000000000000000000000000000000000000000000000003",
    "conditionId": "0x3333333333333333333333333333333333333333333333333333333333333333",
    "title": "France 2-1",
    "eventSlug": "morocco-france",
    "outcome": "Yes",
    "size": 25.0,
    "avgPrice": 0.2,
    "initialValue": 5.0,
    "currentValue": 3.0,
    "cashPnl": -2.0,
    "percentPnl": -40.0,
    "totalBought": 25.0,
    "realizedPnl": 0.0,
    "curPrice": 0.12,
    "redeemable": false,
    "endDate": "2026-07-09"
  }
]
```

Create `tests/fixtures/after_cashout.json` — identical to `after_goal.json` **with the `Morocco wins` object removed entirely**:

```json
[
  {
    "asset": "10000000000000000000000000000000000000000000000000000000000000002",
    "conditionId": "0x2222222222222222222222222222222222222222222222222222222222222222",
    "title": "0-0 first half",
    "eventSlug": "morocco-france",
    "outcome": "Yes",
    "size": 5.0,
    "avgPrice": 0.4,
    "initialValue": 2.0,
    "currentValue": 0.0,
    "cashPnl": -2.0,
    "percentPnl": -100.0,
    "totalBought": 5.0,
    "realizedPnl": 0.0,
    "curPrice": 0.0,
    "redeemable": false,
    "endDate": "2026-07-09"
  },
  {
    "asset": "10000000000000000000000000000000000000000000000000000000000000003",
    "conditionId": "0x3333333333333333333333333333333333333333333333333333333333333333",
    "title": "France 2-1",
    "eventSlug": "morocco-france",
    "outcome": "Yes",
    "size": 25.0,
    "avgPrice": 0.2,
    "initialValue": 5.0,
    "currentValue": 3.0,
    "cashPnl": -2.0,
    "percentPnl": -40.0,
    "totalBought": 25.0,
    "realizedPnl": 0.0,
    "curPrice": 0.12,
    "redeemable": false,
    "endDate": "2026-07-09"
  }
]
```

Create `fixtures.py`:

```python
"""Offline PositionSource. Lets the dashboard be exercised with no network."""

from __future__ import annotations

import json
from pathlib import Path

from models import Position

SCENARIOS: tuple[str, ...] = ("before_match", "after_goal", "after_cashout")

DEFAULT_FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures"


class FixtureSource:
    """Satisfies models.PositionSource. The wallet argument is ignored."""

    def __init__(self, scenario: str, fixture_dir: Path | None = None):
        if scenario not in SCENARIOS:
            raise ValueError(
                f"Unknown scenario {scenario!r}. Expected one of {SCENARIOS}."
            )
        self._scenario = scenario
        self._dir = fixture_dir or DEFAULT_FIXTURE_DIR

    def fetch(self, wallet: str) -> list[Position]:
        path = self._dir / f"{self._scenario}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Position.from_api(row) for row in raw]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fixtures.py -v`
Expected: PASS, 13 passed

- [ ] **Step 5: Commit**

```bash
git add fixtures.py tests/fixtures/ tests/test_fixtures.py
git commit -m "feat: add offline fixture source with the three spec scenarios"
```

---

## Task 5: SQLite persistence

**Files:**
- Create: `db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `models.Position`, `models.CheckpointRow`; `schema.sql`.
- Produces: `init_db(path) -> sqlite3.Connection`; `save_settings(conn, wallet, bankroll=None)`; `load_settings(conn) -> dict | None`; `create_checkpoint(conn, wallet, label) -> int`; `save_checkpoint_positions(conn, checkpoint_id, positions)`; `list_checkpoints(conn, wallet) -> list[dict]`; `load_checkpoint_positions(conn, checkpoint_id) -> list[CheckpointRow]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_db.py`:

```python
import pytest

import db
from models import CheckpointRow, Position

WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


def _position(asset: str, size: float = 10.0, value: float = 5.0) -> Position:
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
            "currentValue": value,
            "percentPnl": 0.0,
            "curPrice": 0.5,
            "realizedPnl": 0.0,
        }
    )


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "test.db")
    yield connection
    connection.close()


def test_init_db_creates_the_file_and_tables(tmp_path):
    path = tmp_path / "nested" / "test.db"
    connection = db.init_db(path)
    assert path.exists()
    names = {
        r[0]
        for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"settings", "checkpoints", "checkpoint_positions"} <= names


def test_init_db_enables_foreign_keys(conn):
    # SQLite defaults foreign_keys OFF per connection; ON CASCADE is inert without it.
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_init_db_is_idempotent(tmp_path):
    db.init_db(tmp_path / "t.db").close()
    db.init_db(tmp_path / "t.db").close()


def test_load_settings_returns_none_when_unset(conn):
    assert db.load_settings(conn) is None


def test_save_then_load_settings_round_trips(conn):
    db.save_settings(conn, WALLET_A, 100.0)
    loaded = db.load_settings(conn)
    assert loaded["wallet_address"] == WALLET_A
    assert loaded["starting_bankroll"] == 100.0


def test_save_settings_overwrites_rather_than_appending(conn):
    db.save_settings(conn, WALLET_A)
    db.save_settings(conn, WALLET_B)
    assert db.load_settings(conn)["wallet_address"] == WALLET_B
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1


def test_create_checkpoint_returns_its_id(conn):
    first = db.create_checkpoint(conn, WALLET_A, "Before match")
    second = db.create_checkpoint(conn, WALLET_A, "Halftime")
    assert second > first


def test_list_checkpoints_only_returns_the_given_wallets(conn):
    db.create_checkpoint(conn, WALLET_A, "A one")
    db.create_checkpoint(conn, WALLET_B, "B one")
    labels = [c["label"] for c in db.list_checkpoints(conn, WALLET_A)]
    assert labels == ["A one"]


def test_list_checkpoints_is_newest_first(conn):
    db.create_checkpoint(conn, WALLET_A, "first")
    db.create_checkpoint(conn, WALLET_A, "second")
    labels = [c["label"] for c in db.list_checkpoints(conn, WALLET_A)]
    assert labels == ["second", "first"]


def test_save_and_load_checkpoint_positions_round_trip(conn):
    checkpoint_id = db.create_checkpoint(conn, WALLET_A, "Before match")
    db.save_checkpoint_positions(conn, checkpoint_id, [_position("AST1"), _position("AST2")])
    rows = db.load_checkpoint_positions(conn, checkpoint_id)
    assert len(rows) == 2
    assert all(isinstance(r, CheckpointRow) for r in rows)
    assert {r.asset for r in rows} == {"AST1", "AST2"}
    assert rows[0].market_title == "Morocco wins"


def test_saving_the_same_asset_twice_in_one_checkpoint_is_rejected(conn):
    checkpoint_id = db.create_checkpoint(conn, WALLET_A, "Before match")
    with pytest.raises(Exception):
        db.save_checkpoint_positions(
            conn, checkpoint_id, [_position("AST1"), _position("AST1")]
        )


def test_load_checkpoint_positions_of_unknown_checkpoint_is_empty(conn):
    assert db.load_checkpoint_positions(conn, 999) == []


def test_saving_an_empty_position_list_is_allowed(conn):
    checkpoint_id = db.create_checkpoint(conn, WALLET_A, "Empty")
    db.save_checkpoint_positions(conn, checkpoint_id, [])
    assert db.load_checkpoint_positions(conn, checkpoint_id) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: Write minimal implementation**

Create `db.py`:

```python
"""SQLite persistence. Every SQL statement in this project lives in this file."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from models import CheckpointRow, Position

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # SQLite defaults this OFF per connection; ON DELETE CASCADE is inert without it.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def save_settings(
    conn: sqlite3.Connection, wallet: str, bankroll: float | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO settings (id, wallet_address, starting_bankroll)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            wallet_address    = excluded.wallet_address,
            starting_bankroll = excluded.starting_bankroll,
            updated_at        = datetime('now')
        """,
        (wallet, bankroll),
    )
    conn.commit()


def load_settings(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return dict(row) if row else None


def create_checkpoint(conn: sqlite3.Connection, wallet: str, label: str) -> int:
    cursor = conn.execute(
        "INSERT INTO checkpoints (wallet_address, label) VALUES (?, ?)",
        (wallet, label),
    )
    conn.commit()
    return int(cursor.lastrowid)


def save_checkpoint_positions(
    conn: sqlite3.Connection, checkpoint_id: int, positions: list[Position]
) -> None:
    conn.executemany(
        """
        INSERT INTO checkpoint_positions (
            checkpoint_id, asset, condition_id, title, event_slug, outcome,
            size, avg_price, stake, current_value, cur_price,
            cash_pnl, percent_pnl, realized_pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                checkpoint_id,
                p.asset,
                p.condition_id,
                p.market_title,
                p.event_slug,
                p.outcome,
                p.size,
                p.entry_price,
                p.stake,
                p.current_value,
                p.current_price,
                p.open_pnl,
                p.percent_pnl,
                p.realized_pnl,
            )
            for p in positions
        ],
    )
    conn.commit()


def list_checkpoints(conn: sqlite3.Connection, wallet: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, label, created_at
        FROM checkpoints
        WHERE wallet_address = ?
        ORDER BY id DESC
        """,
        (wallet,),
    ).fetchall()
    return [dict(r) for r in rows]


def load_checkpoint_positions(
    conn: sqlite3.Connection, checkpoint_id: int
) -> list[CheckpointRow]:
    rows = conn.execute(
        "SELECT * FROM checkpoint_positions WHERE checkpoint_id = ? ORDER BY asset",
        (checkpoint_id,),
    ).fetchall()
    return [
        CheckpointRow(
            asset=r["asset"],
            condition_id=r["condition_id"],
            market_title=r["title"],
            event_slug=r["event_slug"],
            outcome=r["outcome"],
            size=r["size"],
            entry_price=r["avg_price"],
            current_price=r["cur_price"],
            stake=r["stake"],
            current_value=r["current_value"],
            open_pnl=r["cash_pnl"],
            percent_pnl=r["percent_pnl"],
            realized_pnl=r["realized_pnl"],
        )
        for r in rows
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py -v`
Expected: PASS, 13 passed

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat: add SQLite persistence with wallet-scoped checkpoint listing"
```

---

## Task 6: Comparison logic

**Files:**
- Create: `calculations.py`
- Test: `tests/test_calculations.py`

**Interfaces:**
- Consumes: `models.Position`, `models.CheckpointRow`, `models.Row`, `models.Status`, `models.Summary`.
- Produces: `compare(current: list[Position], checkpoint: list[CheckpointRow]) -> list[Row]` (already sorted); `sort_rows(rows) -> list[Row]`; `summarize(rows) -> Summary`; `SIZE_REL_TOL`.

This module imports **only** `models`. No `requests`, no `sqlite3`, no `streamlit`. Its tests must run with the network unplugged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_calculations.py`:

```python
import math

from calculations import compare, summarize
from models import CheckpointRow, Position, Status


def _position(asset, size=10.0, value=5.0, price=0.5, stake=5.0, realized=0.0):
    return Position(
        asset=asset,
        condition_id="0xc",
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


# --- status derives from size, never from value ---------------------------


def test_same_size_is_open_even_when_value_collapses_to_zero():
    rows = _by_asset(
        compare([_position("A", size=10.0, value=0.0)], [_checkpoint("A", size=10.0)])
    )
    assert rows["A"].status is Status.OPEN


def test_smaller_size_is_reduced():
    rows = _by_asset(
        compare([_position("A", size=4.0)], [_checkpoint("A", size=10.0)])
    )
    assert rows["A"].status is Status.REDUCED


def test_larger_size_is_increased():
    rows = _by_asset(
        compare([_position("A", size=12.0)], [_checkpoint("A", size=10.0)])
    )
    assert rows["A"].status is Status.INCREASED


def test_absent_now_is_closed():
    rows = _by_asset(compare([], [_checkpoint("A")]))
    assert rows["A"].status is Status.CLOSED


def test_absent_at_checkpoint_is_new():
    rows = _by_asset(compare([_position("A")], []))
    assert rows["A"].status is Status.NEW


def test_a_resolved_market_is_open_not_closed():
    # A resolved-but-unredeemed market still appears in /positions with price
    # 1.0 or 0.0 and redeemable=True. That is a real market outcome, not a
    # cashout, and it must not be labelled Closed.
    resolved = _position("A", size=10.0, value=10.0, price=1.0)
    rows = _by_asset(compare([resolved], [_checkpoint("A", size=10.0, value=5.0)]))
    assert rows["A"].status is Status.OPEN
    assert math.isclose(rows["A"].change_since_checkpoint, 5.0)


def test_float_noise_does_not_flip_open_to_reduced():
    # Exact == would report Reduced here.
    rows = _by_asset(
        compare(
            [_position("A", size=90548.087076)],
            [_checkpoint("A", size=90548.08707600001)],
        )
    )
    assert rows["A"].status is Status.OPEN


# --- the union, not just current rows -------------------------------------


def test_compare_iterates_the_union_of_both_sides():
    rows = _by_asset(compare([_position("B")], [_checkpoint("A")]))
    assert set(rows) == {"A", "B"}
    assert rows["A"].status is Status.CLOSED
    assert rows["B"].status is Status.NEW


# --- closed rows never assert numbers the app did not measure -------------


def test_closed_row_reports_no_current_value_and_no_change():
    rows = _by_asset(compare([], [_checkpoint("A", value=10.0)]))
    row = rows["A"]
    assert row.current_value is None
    assert row.change_since_checkpoint is None
    assert row.current_price is None
    assert row.price_change is None
    assert row.checkpoint_value == 10.0  # what we did measure, we keep
    assert row.current_size == 0.0


def test_new_row_has_no_checkpoint_side():
    rows = _by_asset(compare([_position("A", value=7.0)], []))
    row = rows["A"]
    assert row.checkpoint_value is None
    assert row.change_since_checkpoint is None
    assert row.checkpoint_price is None
    assert row.current_value == 7.0


# --- arithmetic -----------------------------------------------------------


def test_change_is_current_value_minus_checkpoint_value():
    rows = _by_asset(
        compare([_position("A", value=10.0)], [_checkpoint("A", value=5.0)])
    )
    assert math.isclose(rows["A"].change_since_checkpoint, 5.0)


def test_price_change_and_size_change_are_computed():
    rows = _by_asset(
        compare(
            [_position("A", size=4.0, price=0.9)],
            [_checkpoint("A", size=10.0, price=0.5)],
        )
    )
    row = rows["A"]
    assert math.isclose(row.price_change, 0.4)
    assert math.isclose(row.size_change, -6.0)
    assert math.isclose(row.size_change_percent, -0.6)


def test_size_change_percent_is_none_when_checkpoint_size_is_zero():
    rows = _by_asset(
        compare([_position("A", size=5.0)], [_checkpoint("A", size=0.0)])
    )
    assert rows["A"].size_change_percent is None


def test_since_entry_is_the_positions_open_pnl():
    rows = _by_asset(
        compare([_position("A", value=12.0, stake=5.0)], [_checkpoint("A")])
    )
    assert math.isclose(rows["A"].since_entry, 7.0)


# --- sorting --------------------------------------------------------------


def test_biggest_absolute_mover_sorts_first():
    current = [
        _position("small", value=6.0),
        _position("big", value=0.0),
        _position("mid", value=8.0),
    ]
    checkpoint = [_checkpoint("small"), _checkpoint("big"), _checkpoint("mid")]
    assert [r.asset for r in compare(current, checkpoint)] == ["big", "mid", "small"]


def test_closed_and_new_rows_sort_last():
    current = [_position("moved", value=9.0), _position("fresh")]
    checkpoint = [_checkpoint("moved"), _checkpoint("gone")]
    ordered = [r.asset for r in compare(current, checkpoint)]
    assert ordered[0] == "moved"
    assert set(ordered[1:]) == {"fresh", "gone"}


# --- summary --------------------------------------------------------------


def test_summary_excludes_closed_rows():
    rows = compare([_position("A", value=10.0, stake=5.0)], [_checkpoint("A"), _checkpoint("gone")])
    summary = summarize(rows)
    assert summary.open_positions == 1
    assert math.isclose(summary.total_stake, 5.0)
    assert math.isclose(summary.current_value, 10.0)
    assert math.isclose(summary.open_pnl, 5.0)


def test_summary_of_no_rows_is_all_zero():
    summary = summarize([])
    assert summary.open_positions == 0
    assert summary.total_stake == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_calculations.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'calculations'`

- [ ] **Step 3: Write minimal implementation**

Create `calculations.py`:

```python
"""Pure comparison logic. Imports models and nothing else.

No network, no database, no Streamlit. Keep it that way: this is the module
that has to be right, and purity is what makes it cheap to test.
"""

from __future__ import annotations

import math

from models import CheckpointRow, Position, Row, Status, Summary

# size is a float share count (e.g. 90548.087076). Exact == flakes.
SIZE_REL_TOL = 1e-9


def _status(current: Position | None, checkpoint: CheckpointRow | None) -> Status:
    if current is None:
        return Status.CLOSED
    if checkpoint is None:
        return Status.NEW
    if math.isclose(current.size, checkpoint.size, rel_tol=SIZE_REL_TOL):
        return Status.OPEN
    return Status.REDUCED if current.size < checkpoint.size else Status.INCREASED


def _build_row(current: Position | None, checkpoint: CheckpointRow | None) -> Row:
    status = _status(current, checkpoint)
    reference = current or checkpoint
    assert reference is not None  # compare() never passes two Nones

    both = current is not None and checkpoint is not None

    size_change = current.size - checkpoint.size if both else None
    if both and not math.isclose(checkpoint.size, 0.0, abs_tol=1e-12):
        size_change_percent = size_change / checkpoint.size
    else:
        size_change_percent = None

    return Row(
        asset=reference.asset,
        market_title=reference.market_title,
        outcome=reference.outcome,
        status=status,
        # A closed position's stake is what it was; there is nothing at risk now.
        stake=current.stake if current else (checkpoint.stake if checkpoint else None),
        checkpoint_value=checkpoint.current_value if checkpoint else None,
        current_value=current.current_value if current else None,
        change_since_checkpoint=(
            current.current_value - checkpoint.current_value if both else None
        ),
        since_entry=current.open_pnl if current else None,
        realized_pnl=current.realized_pnl if current else None,
        checkpoint_price=checkpoint.current_price if checkpoint else None,
        current_price=current.current_price if current else None,
        price_change=(
            current.current_price - checkpoint.current_price if both else None
        ),
        checkpoint_size=checkpoint.size if checkpoint else None,
        # The position is gone from /positions, so it holds zero shares. That
        # is a measurement, unlike its sale proceeds, which we never saw.
        current_size=current.size if current else 0.0,
        size_change=size_change,
        size_change_percent=size_change_percent,
    )


def sort_rows(rows: list[Row]) -> list[Row]:
    """Biggest absolute mover first. Rows with no change (Closed, New) last."""

    def key(row: Row) -> tuple[int, float]:
        if row.change_since_checkpoint is None:
            return (1, 0.0)
        return (0, -abs(row.change_since_checkpoint))

    return sorted(rows, key=key)


def compare(current: list[Position], checkpoint: list[CheckpointRow]) -> list[Row]:
    """Join on asset over the union of both sides.

    Iterating only `current` can never discover a Closed position.
    """
    current_by_asset = {p.asset: p for p in current}
    checkpoint_by_asset = {c.asset: c for c in checkpoint}

    rows = [
        _build_row(current_by_asset.get(asset), checkpoint_by_asset.get(asset))
        for asset in current_by_asset.keys() | checkpoint_by_asset.keys()
    ]
    return sort_rows(rows)


def summarize(rows: list[Row]) -> Summary:
    """Totals over live rows only. Closed rows are excluded: the app never saw
    the cashout proceeds, so including them would invent a number."""
    live = [r for r in rows if r.status is not Status.CLOSED]
    return Summary(
        open_positions=len(live),
        total_stake=sum(r.stake or 0.0 for r in live),
        current_value=sum(r.current_value or 0.0 for r in live),
        open_pnl=sum(r.since_entry or 0.0 for r in live),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_calculations.py -v`
Expected: PASS, 18 passed

- [ ] **Step 5: Verify the module is genuinely pure**

Run: `.venv/Scripts/python.exe -c "import ast,sys; tree=ast.parse(open('calculations.py').read()); mods={n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names}; banned=mods & {'requests','sqlite3','streamlit','db','polymarket_client','fixtures'}; print('IMPURE:',banned) if banned else print('pure: imports only', sorted(mods)); sys.exit(1 if banned else 0)"`
Expected: `pure: imports only ['math', 'models']`

- [ ] **Step 6: Commit**

```bash
git add calculations.py tests/test_calculations.py
git commit -m "feat: add pure checkpoint comparison with size-derived status"
```

---

# Phase 2 — UI (serial)

## Task 7: Table rendering and styling

**Files:**
- Create: `ui.py`
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `models.Row`, `models.Status`, `models.Summary`.
- Produces: `rows_to_frame(rows) -> pd.DataFrame`; `style_frame(frame) -> pd.io.formats.style.Styler`; `render_summary(summary)`; `render_table(rows)`; `COLUMNS: list[str]`.

Split deliberately: `rows_to_frame` and `style_frame` are pure and testable; only `render_*` touch Streamlit.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ui.py`:

```python
import pandas as pd

from calculations import compare
from models import CheckpointRow, Position, Status
from ui import COLUMNS, rows_to_frame, style_frame


def _position(asset, size=10.0, value=5.0):
    return Position(
        asset=asset, condition_id="0xc", market_title=f"M{asset}", event_slug="e",
        outcome="Yes", size=size, entry_price=0.5, current_price=0.5, stake=5.0,
        current_value=value, open_pnl=value - 5.0, percent_pnl=0.0,
        realized_pnl=0.0, redeemable=False, end_date="2026-07-09",
    )


def test_frame_has_the_documented_columns_in_order():
    frame = rows_to_frame(compare([_position("A")], []))
    assert list(frame.columns) == COLUMNS


def test_closed_row_renders_now_and_change_as_missing_not_zero():
    rows = compare([], [CheckpointRow.from_position(_position("A", value=10.0))])
    frame = rows_to_frame(rows)
    assert pd.isna(frame.loc[0, "Now"])
    assert pd.isna(frame.loc[0, "Change Since Checkpoint"])
    assert frame.loc[0, "Checkpoint Value"] == 10.0
    assert frame.loc[0, "Size Status"] == Status.CLOSED.value


def test_style_frame_colors_gains_green_and_losses_red():
    checkpoint = [CheckpointRow.from_position(_position("A"))]
    frame = rows_to_frame(compare([_position("A", value=10.0)], checkpoint))
    html = style_frame(frame).to_html()
    assert "green" in html


def test_style_frame_renders_missing_values_as_an_em_dash():
    rows = compare([], [CheckpointRow.from_position(_position("A"))])
    html = style_frame(rows_to_frame(rows)).to_html()
    assert "—" in html


def test_style_frame_never_colors_a_closed_row_red():
    # A cashout must not read as a market loss.
    rows = compare([], [CheckpointRow.from_position(_position("A", value=10.0))])
    html = style_frame(rows_to_frame(rows)).to_html()
    assert "color: red" not in html


def test_style_frame_does_not_use_the_removed_applymap_api():
    # pandas 3.0 removed Styler.applymap. This guards against a regression that
    # would only surface at runtime.
    frame = rows_to_frame(compare([_position("A")], []))
    styler = frame.style
    assert not hasattr(styler, "applymap")
    assert hasattr(styler, "map")
    style_frame(frame).to_html()  # must not raise


def test_empty_rows_produce_an_empty_frame_with_columns():
    frame = rows_to_frame([])
    assert frame.empty
    assert list(frame.columns) == COLUMNS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ui.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ui'`

- [ ] **Step 3: Write minimal implementation**

Create `ui.py`:

```python
"""Rendering and styling. The table is the product; summary cards are secondary."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from models import Row, Status, Summary

COLUMNS = [
    "Market",
    "Outcome",
    "Stake",
    "Checkpoint Value",
    "Now",
    "Change Since Checkpoint",
    "Since Entry",
    "Realized",
    "Checkpoint Price",
    "Current Price",
    "Price Change",
    "Size at Checkpoint",
    "Current Size",
    "Size Status",
]

MONEY_COLUMNS = [
    "Stake",
    "Checkpoint Value",
    "Now",
    "Change Since Checkpoint",
    "Since Entry",
    "Realized",
]
PRICE_COLUMNS = ["Checkpoint Price", "Current Price", "Price Change"]
SIZE_COLUMNS = ["Size at Checkpoint", "Current Size"]

# Only these carry gain/loss meaning. Stake and Now are not wins or losses.
PNL_COLUMNS = ["Change Since Checkpoint", "Since Entry", "Price Change"]

STATUS_BACKGROUND = {
    Status.OPEN.value: "",
    Status.REDUCED.value: "background-color: #fff3cd",
    Status.CLOSED.value: "background-color: #e9ecef; color: #6c757d",
    Status.NEW.value: "background-color: #cfe2ff",
    Status.INCREASED.value: "background-color: #cfe2ff",
}


def rows_to_frame(rows: list[Row]) -> pd.DataFrame:
    """None becomes NaN, which the styler renders as an em-dash."""
    records = [
        {
            "Market": r.market_title,
            "Outcome": r.outcome,
            "Stake": r.stake,
            "Checkpoint Value": r.checkpoint_value,
            "Now": r.current_value,
            "Change Since Checkpoint": r.change_since_checkpoint,
            "Since Entry": r.since_entry,
            "Realized": r.realized_pnl,
            "Checkpoint Price": r.checkpoint_price,
            "Current Price": r.current_price,
            "Price Change": r.price_change,
            "Size at Checkpoint": r.checkpoint_size,
            "Current Size": r.current_size,
            "Size Status": r.status.value,
        }
        for r in rows
    ]
    return pd.DataFrame(records, columns=COLUMNS)


def _colour_pnl(value) -> str:
    if pd.isna(value) or value == 0:
        return ""
    return "color: green" if value > 0 else "color: red"


def _colour_status(value) -> str:
    return STATUS_BACKGROUND.get(value, "")


def style_frame(frame: pd.DataFrame):
    """pandas 3.0 removed Styler.applymap. `Styler.map` is the elementwise API."""
    styler = frame.style

    present = [c for c in PNL_COLUMNS if c in frame.columns]
    if present:
        styler = styler.map(_colour_pnl, subset=present)
    if "Size Status" in frame.columns:
        styler = styler.map(_colour_status, subset=["Size Status"])

    return styler.format(
        {
            **{c: "${:,.2f}" for c in MONEY_COLUMNS},
            **{c: "{:.4f}" for c in PRICE_COLUMNS},
            **{c: "{:,.2f}" for c in SIZE_COLUMNS},
        },
        na_rep="—",
    )


def render_summary(summary: Summary, checkpoint_label: str, last_refreshed: str) -> None:
    columns = st.columns(6)
    columns[0].metric("Open positions", summary.open_positions)
    columns[1].metric("Total stake", f"${summary.total_stake:,.2f}")
    columns[2].metric("Current value", f"${summary.current_value:,.2f}")
    columns[3].metric("Open PnL", f"${summary.open_pnl:,.2f}")
    columns[4].metric("Checkpoint", checkpoint_label or "—")
    columns[5].metric("Last refreshed", last_refreshed or "—")


def render_table(rows: list[Row]) -> None:
    if not rows:
        st.info("No open positions for this wallet.")
        return
    st.dataframe(style_frame(rows_to_frame(rows)), width="stretch", hide_index=True)
    if any(r.status is Status.CLOSED for r in rows):
        st.caption(
            "Closed rows show — because the app cannot see cashout proceeds. "
            "They are excluded from the totals above."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ui.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add ui.py tests/test_ui.py
git commit -m "feat: add table rendering with pandas 3.0 Styler.map coloring"
```

---

## Task 8: Streamlit app wiring

**Files:**
- Create: `app.py`
- Test: manual (Streamlit entrypoints are wiring; the logic is tested in Tasks 6 and 7)

**Interfaces:**
- Consumes: everything above.
- Produces: the runnable app.

- [ ] **Step 1: Write the implementation**

Create `app.py`:

```python
"""Local Polymarket prop tracker. Read-only. Never trades, never signs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

import db
from calculations import compare, summarize
from fixtures import SCENARIOS, FixtureSource
from models import CheckpointRow, Position, PositionSource
from polymarket_client import InvalidWalletError, PolymarketError, PolymarketSource
from ui import render_summary, render_table

DB_PATH = Path(__file__).parent / "data" / "polymarket_tracker.db"


@st.cache_resource
def _connection():
    return db.init_db(DB_PATH)


def _source(use_fake: bool, scenario: str) -> PositionSource:
    return FixtureSource(scenario) if use_fake else PolymarketSource()


def _load_positions(source: PositionSource, wallet: str) -> list[Position] | None:
    try:
        return source.fetch(wallet)
    except InvalidWalletError as exc:
        st.error(str(exc))
    except PolymarketError as exc:
        st.error(str(exc))
    return None


def main() -> None:
    st.set_page_config(page_title="Polymarket Prop Tracker", layout="wide")
    st.title("Polymarket Prop Tracker")

    conn = _connection()
    settings = db.load_settings(conn) or {}

    with st.sidebar:
        st.header("Data source")
        use_fake = st.toggle("Use fake data", value=False)
        scenario = st.selectbox("Scenario", SCENARIOS) if use_fake else ""

    wallet = st.text_input("Wallet address", value=settings.get("wallet_address", ""))

    controls = st.columns(4)
    if controls[0].button("Save settings") and wallet:
        db.save_settings(conn, wallet)
        st.success("Saved.")

    refresh = controls[1].button("Refresh", type="primary")
    label = controls[2].text_input("Checkpoint label", placeholder="Before match")
    save_checkpoint = controls[3].button("Save checkpoint")

    if refresh or "positions" not in st.session_state:
        if wallet or use_fake:
            positions = _load_positions(_source(use_fake, scenario), wallet)
            if positions is not None:
                st.session_state["positions"] = positions
                st.session_state["refreshed_at"] = datetime.now().strftime("%H:%M:%S")

    positions: list[Position] = st.session_state.get("positions", [])

    if save_checkpoint:
        if not label:
            st.warning("Give the checkpoint a label first.")
        elif not wallet:
            st.warning("Enter a wallet first.")
        else:
            checkpoint_id = db.create_checkpoint(conn, wallet, label)
            db.save_checkpoint_positions(conn, checkpoint_id, positions)
            st.success(f"Saved checkpoint: {label}")

    checkpoints = db.list_checkpoints(conn, wallet) if wallet else []
    options = {f"{c['label']}  ({c['created_at']})": c["id"] for c in checkpoints}
    selected = st.selectbox("Compare against", ["(none)"] + list(options))

    checkpoint_rows: list[CheckpointRow] = []
    if selected != "(none)":
        checkpoint_rows = db.load_checkpoint_positions(conn, options[selected])

    rows = compare(positions, checkpoint_rows)
    render_summary(
        summarize(rows),
        checkpoint_label="" if selected == "(none)" else selected,
        last_refreshed=st.session_state.get("refreshed_at", ""),
    )
    render_table(rows)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the app boots without error**

Run: `.venv/Scripts/python.exe -c "import app; print('imports clean')"`
Expected: `imports clean`

- [ ] **Step 3: Drive it with fake data**

Run: `.venv/Scripts/python.exe -m streamlit run app.py`

In the browser: enable **Use fake data**, pick `before_match`, click **Refresh**. Three props appear. Type `Before match`, click **Save checkpoint**. Switch the scenario to `after_goal`, click **Refresh**, select `Before match` in **Compare against**.

Expected: `Morocco wins +$5.00` green at the top, `0-0 first half −$2.00` red, `France 2-1 −$2.00` red.

Then switch to `after_cashout` and **Refresh**.
Expected: `Morocco wins` reads `Closed`, gray, with `—` under Now and Change — **not** a −$10.00 loss.

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: wire Streamlit app with fake/real source toggle"
```

---

# Phase 3 — Acceptance and documentation (serial)

## Task 9: End-to-end acceptance tests

**Files:**
- Create: `tests/test_acceptance.py`

**Interfaces:**
- Consumes: `fixtures.FixtureSource`, `calculations.compare`, `db`.
- Produces: nothing; this is the gate.

These encode the source plan's Step 14 exactly. They are the tests that decide whether the app is correct.

- [ ] **Step 1: Write the failing test**

Create `tests/test_acceptance.py`:

```python
"""The scenarios from initial_plan.md Step 14. These decide correctness."""

import dataclasses
import math

import pytest

import db
from calculations import compare, summarize
from fixtures import FixtureSource
from models import CheckpointRow, Status

WALLET = "0x" + "0" * 40


def _rows(current_scenario: str, checkpoint_scenario: str):
    current = FixtureSource(current_scenario).fetch(WALLET)
    checkpoint = [
        CheckpointRow.from_position(p)
        for p in FixtureSource(checkpoint_scenario).fetch(WALLET)
    ]
    return {r.market_title: r for r in compare(current, checkpoint)}


def test_after_a_goal_each_prop_shows_the_expected_change():
    rows = _rows("after_goal", "before_match")
    assert math.isclose(rows["Morocco wins"].change_since_checkpoint, 5.0)
    assert math.isclose(rows["0-0 first half"].change_since_checkpoint, -2.0)
    assert math.isclose(rows["France 2-1"].change_since_checkpoint, -2.0)


def test_after_a_goal_the_biggest_mover_is_first():
    current = FixtureSource("after_goal").fetch(WALLET)
    checkpoint = [
        CheckpointRow.from_position(p)
        for p in FixtureSource("before_match").fetch(WALLET)
    ]
    assert compare(current, checkpoint)[0].market_title == "Morocco wins"


def test_a_cashout_is_closed_and_never_a_market_loss():
    rows = _rows("after_cashout", "after_goal")
    morocco = rows["Morocco wins"]
    assert morocco.status is Status.CLOSED
    # It was worth $10 at the checkpoint. A -$10 "loss" would be a lie.
    assert morocco.change_since_checkpoint is None
    assert morocco.current_value is None
    assert morocco.checkpoint_value == 10.0


def test_a_cashout_is_excluded_from_the_totals():
    rows = list(_rows("after_cashout", "after_goal").values())
    summary = summarize(rows)
    assert summary.open_positions == 2  # Morocco is gone, not counted


def test_surviving_props_are_unaffected_by_the_cashout():
    rows = _rows("after_cashout", "after_goal")
    assert rows["0-0 first half"].status is Status.OPEN
    assert rows["France 2-1"].status is Status.OPEN
    assert math.isclose(rows["France 2-1"].change_since_checkpoint, 0.0, abs_tol=1e-9)


def test_a_partial_cashout_reads_reduced_not_closed():
    """The sizeThreshold=0 failure mode: a position shrunk below 1 share must
    still be Reduced, never Closed. With the API's default sizeThreshold of 1,
    this row would not come back at all and would be reported Closed."""
    before = FixtureSource("before_match").fetch(WALLET)
    checkpoint = [CheckpointRow.from_position(p) for p in before]

    morocco = next(p for p in before if p.market_title == "Morocco wins")
    shrunk = dataclasses.replace(
        morocco, size=0.4, current_value=0.4, stake=0.2, open_pnl=0.2
    )
    rows = {r.market_title: r for r in compare([shrunk], checkpoint)}
    assert rows["Morocco wins"].status is Status.REDUCED


@pytest.mark.parametrize("scenario", ["before_match", "after_goal", "after_cashout"])
def test_the_full_round_trip_through_sqlite_preserves_the_join_key(tmp_path, scenario):
    conn = db.init_db(tmp_path / "acc.db")
    positions = FixtureSource(scenario).fetch(WALLET)
    checkpoint_id = db.create_checkpoint(conn, WALLET, scenario)
    db.save_checkpoint_positions(conn, checkpoint_id, positions)

    reloaded = db.load_checkpoint_positions(conn, checkpoint_id)
    assert {r.asset for r in reloaded} == {p.asset for p in positions}

    # Comparing a scenario against itself must yield all-Open, zero change.
    rows = compare(positions, reloaded)
    assert all(r.status is Status.OPEN for r in rows)
    assert all(math.isclose(r.change_since_checkpoint, 0.0) for r in rows)
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_acceptance.py -v`
Expected: PASS, 9 passed (the parametrized round-trip contributes 3). If any fail, the bug is in `calculations.py` or `db.py`, not here — fix the module, not the test.

- [ ] **Step 3: Run the whole suite**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS, all tests green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_acceptance.py
git commit -m "test: add Step 14 acceptance tests for cashout vs market loss"
```

---

## Task 10: README and real-wallet smoke test

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: nothing.
- Produces: setup instructions for the user and their friends.

- [ ] **Step 1: Write the README**

Create `README.md`:

````markdown
# Polymarket Prop Tracker

A local dashboard showing how each of your individual Polymarket prop positions
has moved since a moment you marked — before a match, after a goal, after a cashout.

Read-only. It never trades, never signs, and never asks for a private key.

## Your wallet stays on your machine

This repo ships **code only**. Your wallet address and checkpoint history live in
`data/polymarket_tracker.db`, which is gitignored and never leaves your computer.

A wallet address is not a secret, but it *is* a permanent on-chain identifier:
publishing one links your GitHub identity to your entire betting history. A
pre-commit hook blocks committing one. Do not bypass it.

If anything ever asks you for a **private key** or **seed phrase**, it is not
this project. Do not paste it.

## Setup

```bash
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe scripts/check_no_secrets.py --install
```

## Run

```bash
.venv/Scripts/python.exe -m streamlit run app.py
```

Try it with no wallet first: tick **Use fake data** in the sidebar, pick
`before_match`, hit **Refresh**, save a checkpoint, then switch the scenario to
`after_goal` and refresh again.

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

## How to use it

1. Paste your wallet address, click **Save settings**.
2. Click **Refresh** to load your open positions.
3. Before the match, label a checkpoint `Before match` and click **Save checkpoint**.
4. After a goal, click **Refresh** and pick `Before match` under **Compare against**.
5. Each prop now shows its checkpoint value, its current value, and the change.
   Green is up, red is down, sorted by biggest mover.

If you cash out, the row reads `Closed` or `Reduced` rather than showing a fake
market loss. A closed row shows `—` rather than a number, because the app reads
only your open positions and never sees what you sold for.
````

- [ ] **Step 2: Run the real-wallet smoke test**

Only after the full suite passes. Start the app, untick **Use fake data**, paste
your real wallet, click **Refresh**.

Verify, in order:
- Positions appear and the count matches Polymarket's UI.
- Save a checkpoint labelled `Test checkpoint`.
- Click **Refresh** again; every row reads `Open` with a change of `$0.00`.
- Manually cash out one small or already-decided position on Polymarket.
- Click **Refresh**. That row reads `Reduced` or `Closed` — never a red market loss.
- Save a new checkpoint after the cashout and use it as the new baseline.

Do **not** paste your wallet into a commit, an issue, or a test fixture.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add README with setup, wallet privacy, and smoke test"
```

---

## Deferred to V2 (do not build now)

Per the source plan's scope rules, and deliberately excluded:

- **Auto-refresh** (15/30/60s, manual default). Adds state and debugging noise before the core is proven. When it lands: Streamlit rerun or a simple timer. Never a WebSocket, never a background thread.
- **CSV export**, then PNG export.
- Trading, signing, order placement, cash-out suggestions, predictions, ML, a second API endpoint, complex portfolio analytics.
