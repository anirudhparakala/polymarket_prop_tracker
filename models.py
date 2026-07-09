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
