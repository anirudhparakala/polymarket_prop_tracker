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


def _money(raw: dict, key: str) -> float:
    """Polymarket US nests money as {"value": "12.34", "currency": "USD"}."""
    nested = raw.get(key)
    if not isinstance(nested, dict):
        return 0.0
    return _f(nested, "value")


def _finite(value: float, default: float = 0.0) -> float:
    return value if math.isfinite(value) else default


def _safe_div(numerator: float, denominator: float) -> float:
    """Derived price. Zero (or non-finite) denominator yields 0.0, never a
    ZeroDivisionError and never an Inf that would poison the table."""
    if denominator == 0 or not math.isfinite(denominator):
        return 0.0
    return _finite(numerator / denominator)


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
        # open_pnl is DERIVED, so _f never screens it: subtracting two finite
        # but enormous values can still overflow to +/-Inf, which would then
        # propagate into the table and the portfolio total. Screen the result.
        open_pnl = current_value - stake
        if not math.isfinite(open_pnl):
            open_pnl = 0.0
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
            open_pnl=open_pnl,
            percent_pnl=_f(raw, "percentPnl"),
            realized_pnl=_f(raw, "realizedPnl"),
            redeemable=_b(raw, "redeemable"),
            end_date=_s(raw, "endDate"),
        )

    @classmethod
    def from_us_api(cls, slug: str, raw: dict) -> Position:
        """Normalize ONE Polymarket US portfolio position.

        Polymarket US is a different platform with a different shape, and none of
        the crypto API's field names apply. Notably:
          * decimals arrive as STRINGS ("12.34"), not numbers;
          * money is nested as {"value": "12.34", "currency": "USD"};
          * there is NO price field at all -- price must be derived.

        Mapping (each one verified against a REAL payload, not inferred from the
        docs -- which omitted avgPx, fees, baseCost, team and subject entirely):
          netPositionDecimal -> size          shares currently held
          cost.value         -> stake         dollars paid, INCLUDING fees
          cashValue.value    -> current_value what it is worth now
          avgPx.value        -> entry_price   the API's own average fill price
          realized.value     -> realized_pnl  already banked on this market
          open_pnl = current_value - stake    (same identity as the crypto API)

        `stake` is `cost`, NOT `qtyBoughtDecimal`. qtyBought is a SHARE COUNT --
        using a share count as a dollar stake is exactly the unit confusion that
        produced a -$103,707 "loss" on a winning position on the crypto side.

        FEES: cost = baseCost + fees. We use `cost`, i.e. what actually left the
        account, so a position that has not moved but cost a fee shows that fee
        as a small loss. That is true: you are down by the fee.

        OUTCOME: marketMetadata.outcome is a useless "Yes" on every row. The bet
        the user actually made is in `team.name` ("Argentina") or `subject.name`
        ("FRA wins 2-1"). Without it, two bets in the same event render as two
        identical rows -- hold Argentina AND Brazil to win and you could not tell
        them apart.

        current_price has no field in the payload, so it is derived (value /
        shares) and guarded: a zero or non-finite share count yields 0.0 rather
        than an Inf that would poison the table.
        """
        meta = raw.get("marketMetadata") or {}

        size = _f(raw, "netPositionDecimal")
        stake = _money(raw, "cost")  # includes fees; see FEES above
        current_value = _money(raw, "cashValue")
        open_pnl = _finite(current_value - stake)

        # The specific selection: a team ("Argentina") for a winner market, a
        # subject ("FRA wins 2-1") for a score market. Fall back to the bare
        # Yes/No only if neither is present.
        team = meta.get("team") if isinstance(meta.get("team"), dict) else {}
        subject = meta.get("subject") if isinstance(meta.get("subject"), dict) else {}
        selection = _s(team, "name") or _s(subject, "name") or _s(meta, "outcome")

        return cls(
            # The positions object is keyed by market slug, one entry per
            # tradeable outcome, so the slug is the stable join key -- the role
            # `asset` (the ERC-1155 token id) plays on the crypto side.
            asset=slug,
            condition_id=_s(meta, "slug", slug),
            market_title=_s(meta, "title"),
            event_slug=_s(meta, "eventSlug"),
            outcome=selection,
            size=size,
            # The API reports its own average fill price -- use it rather than
            # re-deriving cost/shares and disagreeing with the source of truth.
            entry_price=_money(raw, "avgPx") or _safe_div(stake, size),
            current_price=_safe_div(current_value, size),
            stake=stake,
            current_value=current_value,
            open_pnl=open_pnl,
            percent_pnl=_finite(_safe_div(open_pnl, stake) * 100.0),
            realized_pnl=_money(raw, "realized"),
            redeemable=_b(raw, "expired"),
            end_date="",  # the US positions payload carries no settlement date
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
