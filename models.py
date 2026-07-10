"""Frozen data contracts shared by every module.

Phase 0 artifact. Once this lands, Phase 1 agents code against it in parallel
and none of them may change it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


def _f(raw: dict, key: str, default: float = 0.0) -> float:
    """Coerce a raw JSON value to a *finite* float.

    The remote API is not trusted. A missing key, a non-numeric string, or a
    non-finite number (NaN/Inf — which Python's json module parses by default
    from the literal `NaN`/`Infinity` tokens) all collapse to the default
    rather than propagating. A NaN that reaches the UI renders the whole
    portfolio total as "NaN", scrambles row ordering, and silently becomes
    NULL when written to SQLite; an Inf poisons every downstream sum. None of
    those are recoverable once past this boundary, so they are rejected here.
    """
    value = raw.get(key)
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: an outsized int (e.g. 10**400) has no float form.
        return default
    return result if math.isfinite(result) else default


def _s(raw: dict, key: str, default: str = "") -> str:
    value = raw.get(key)
    return default if value is None else str(value)


def _b(raw: dict, key: str, default: bool = False) -> bool:
    """Coerce a raw JSON value to a bool without the ``bool("false") is True`` trap.

    A real JSON boolean arrives as a Python bool and passes straight through.
    A string (some feeds send ``"false"``) is interpreted by content, not by
    truthiness — ``bool("false")`` is ``True``, which would silently invert the
    flag.
    """
    value = raw.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


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
            redeemable=_b(raw, "redeemable"),
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
