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
