# Polymarket Prop Tracker — Design

**Date:** 2026-07-09
**Status:** Approved
**Source spec:** [initial_plan.md](../../../initial_plan.md)

## Purpose

A local-first Streamlit dashboard that answers one question: **how has each individual prop position moved since a moment I marked?**

The user saves a named checkpoint ("Before match"), an event happens (a goal, a cashout), they hit Refresh, and the table shows per-prop before/now/change. The table is the product. Total portfolio PnL is deliberately secondary.

Read-only. No trading, no signing, no private keys, no ML, no advice text.

## Corrections to the source plan

Three claims in `initial_plan.md` were verified against the live Polymarket Data API docs and found wrong. Each would produce a dashboard that reports confident, incorrect numbers.

### 1. `stake = totalBought` is unit-inconsistent

`totalBought` is a **share count**, not dollars. In Polymarket's own documented example:

```
size         =  90548.087076   shares
totalBought  = 109548.077076   shares  (more than size: some were sold)
avgPrice     =      0.020628   dollars/share
initialValue =   1867.825940   dollars
currentValue =   5840.351616   dollars
cashPnl      =   3972.525676   dollars
```

The plan's `open_pnl = currentValue - totalBought` yields **-$103,707** on a position that is actually **up $3,972**.

These identities were verified numerically and hold exactly:

```
initialValue = size × avgPrice
currentValue = size × curPrice
cashPnl      = currentValue − initialValue
percentPnl   = cashPnl / initialValue × 100
```

**Resolution:** `stake = initialValue`, `open_pnl = currentValue − initialValue` (≡ `cashPnl`). `totalBought` is not used in V1. `realizedPnl` gets its own column.

### 2. `sizeThreshold` defaults to `1`, not `0`

The plan calls passing it "optional." Positions below 1 share are silently omitted. A partial cashout dropping a position under 1 share makes it **vanish**, and the comparison labels it `Closed` instead of `Reduced` — defeating the app's core correctness property.

**Resolution:** the client always sends `sizeThreshold=0`.

### 3. `limit` defaults to `100`

Max 500, paginated via `offset`. The plan never mentions pagination. Position 101 disappears and is reported `Closed` on the first refresh.

**Resolution:** the client paginates with `limit=500`, incrementing `offset` until a short page returns.

Failures 2 and 3 corrupt the same property in the same direction: they manufacture phantom `Closed` rows.

## API reference (verified)

`GET https://data-api.polymarket.com/positions`

Returns a **bare JSON array**. Relevant query params:

| Param | Default | Used |
|---|---|---|
| `user` | required | wallet address |
| `sizeThreshold` | `1` | forced to `0` |
| `limit` | `100` (max 500) | `500` |
| `offset` | `0` | pagination cursor |

Fields consumed: `asset`, `conditionId`, `title`, `eventSlug`, `outcome`, `size`, `avgPrice`, `initialValue`, `currentValue`, `cashPnl`, `percentPnl`, `realizedPnl`, `curPrice`, `endDate`, `redeemable`.

Prices are probabilities in `[0, 1]`. `size` is a share count. `size × price ≈ value`.

## Architecture

Dependency DAG rooted at `models.py`. Nothing imports sideways.

```
models.py          Position, CheckpointRow, Row, Status, PositionSource
   |
   +-- polymarket_client.py   PolymarketSource   (HTTP)
   +-- fixtures.py            FixtureSource      (JSON scenarios)
   +-- db.py                  SQLite; all SQL lives here
   +-- calculations.py        pure: (list[Position], list[CheckpointRow]) -> list[Row]
   |
   +-- ui.py                  renders list[Row]
   +-- app.py                 wiring, session state
```

`calculations.py` imports only `models.py`. Its tests need neither network nor database. This is the property that makes the module testable and the build parallelizable; preserve it.

### PositionSource protocol

```python
class PositionSource(Protocol):
    def fetch(self, wallet: str) -> list[Position]: ...
```

`PolymarketSource` hits the API. `FixtureSource` loads JSON from `tests/fixtures/`. `app.py` selects one via a sidebar toggle. Neither is imported by `calculations.py`.

### Internal shape

`Position` (normalized at the client boundary — raw API names never reach `ui.py`):

```
asset, condition_id, market_title, event_slug, outcome,
size, entry_price, current_price, stake, current_value,
open_pnl, percent_pnl, realized_pnl, redeemable, end_date
```

Normalization: `stake = initialValue`, `open_pnl = currentValue - initialValue`, `entry_price = avgPrice`, `current_price = curPrice`. Missing fields fall back to a default rather than raising; the API adds fields over time.

## Data model

Three tables. `checkpoints` gains a `wallet_address` column not present in the source plan.

```
settings              id, wallet_address, starting_bankroll, created_at, updated_at
checkpoints           id, wallet_address, label, created_at
checkpoint_positions  id, checkpoint_id, asset, condition_id, title, event_slug,
                      outcome, size, avg_price, stake, current_value, cur_price,
                      cash_pnl, percent_pnl, realized_pnl, created_at
```

`checkpoints.wallet_address` is indexed. The compare dropdown filters on the active wallet, making cross-wallet comparison impossible by construction. Without it, pasting a second wallet renders every old row `Closed` and every new row `New` — all numbers technically correct, the dashboard useless.

`checkpoint_positions` stores one row per active prop at that instant. Checkpoints are immutable once written.

## Comparison logic

Join on `asset` — the only stable identity for a prop outcome across refreshes. Never join on `title`, `outcome`, or `condition_id`; one `condition_id` spans many outcomes.

Iterate the **union** of checkpoint and current assets. Iterating only current rows can never discover a `Closed` position.

Status derives from `size`, never from value:

| Condition | Status |
|---|---|
| in both, sizes equal within tolerance | `Open` |
| in both, size decreased | `Reduced` |
| in both, size increased | `Increased` |
| checkpoint only | `Closed` |
| current only | `New` |

`size` is a float. Equality uses `math.isclose(a, b, rel_tol=1e-9)`. Exact `==` flakes and randomly reports `Reduced`.

Derived values, computed only when both sides exist:

```
change_since_checkpoint       = current_value  − checkpoint_value
price_change_since_checkpoint = current_price  − checkpoint_price
size_change                   = current_size   − checkpoint_size
size_change_percent           = size_change / checkpoint_size     (guard size 0)
```

Sort by `abs(change_since_checkpoint)` descending, so the biggest movers surface after a goal. `Closed` and `New` rows sort last; they have no change to rank by.

### Closed positions

A fully cashed-out position **disappears** from `/positions`. The app therefore cannot know its sale proceeds.

`Closed` rows show `—` for Now and Change (never `$0.00`, which asserts a measurement the app never made), render gray, and are **excluded from summary totals**. Consequence, accepted: after a cashout the summary cards will not reconcile against the Polymarket balance. The app is honest about what it measured rather than complete.

A resolved-but-unredeemed market still appears in `/positions` with `redeemable: true` and a price of `1.0` or `0.0`. That is `Open`, not `Closed` — a real market outcome, not a cashout.

## UI

Top: wallet input, save settings, refresh, checkpoint label input, save checkpoint, compare-against dropdown, fake/real toggle.

Summary cards (secondary): open position count, total stake, current value, open PnL, selected checkpoint, last refreshed. Never visually dominant.

Main table columns: Market, Outcome, Stake, Checkpoint Value, Now, Change Since Checkpoint, Since Entry, Realized, Checkpoint Price, Current Price, Price Change, Size at Checkpoint, Current Size, Size Status.

Coloring: `Change`, `Since Entry`, `Price Change` green above zero, red below, neutral at zero. Status: `Open` normal, `Reduced` yellow/orange, `Closed` gray, `New` blue. `Closed` is never colored as a red loss.

No advice text. Numbers only. Never "cash out now", "hold", "good bet".

**pandas 3.0.3 removed `Styler.applymap`** (and `DataFrame.applymap`). Use `Styler.map`. Verified against the installed version; nearly every example and recalled snippet still uses the removed name.

## Error handling

| Case | Behavior |
|---|---|
| Wallet fails `^0x[a-fA-F0-9]{40}$` | Inline validation error, no request issued |
| Empty array | Empty state, not an error |
| HTTP 429 / 5xx | Readable banner; last good table stays on screen |
| Network timeout | Same as above; 10s timeout |
| Missing/new fields | Fall back to default; never raise |

## Testing

Pytest. `calculations.py` is pure, so its tests need no network and no database.

Acceptance tests from the source plan's Step 14, driven by `FixtureSource`:

- before_match → after_goal: `Morocco wins +$5` (green), `0-0 first half −$2` (red), `France 2-1 −$2` (red)
- after_goal → after_cashout: `Morocco wins` is `Closed`, **not** a `−$10` market loss

Plus one guard test asserting `cashPnl ≈ currentValue − initialValue` across every fixture. If Polymarket changes those semantics, that test fails loudly instead of the dashboard quietly lying.

Real-wallet smoke test runs only after fake data passes. Fixtures use `0x` + 40 zeros; a pre-commit hook blocks real wallet addresses.

## Build order

| Phase | Work | Parallel |
|---|---|---|
| 0 | `models.py` + db schema — the frozen contracts | serial, lands first |
| 1 | client · fixtures · db · calculations | 4 agents, no shared files |
| 2 | `ui.py`, then `app.py` | serial |
| 3 | acceptance tests, then real-wallet smoke | serial |

Phase 1 parallelizes only because every agent codes against `models.py` and none import each other. If an agent needs to edit a sibling's file, the boundary was wrong.

## Out of scope for V1

Trading, wallet signing, order placement, private keys, cash-out suggestions, predictions, ML, WebSockets, background workers, complex portfolio analytics, a second API endpoint, and auto-refresh.

Auto-refresh is deferred deliberately: it adds state and debugging noise before the core is proven. When it lands (15/30/60s, manual default), it uses Streamlit rerun behavior or a simple timer — never a WebSocket or a background thread.

CSV export after the dashboard works. PNG export later still.

## Definition of done

1. Enter wallet. 2. Refresh. 3. See every individual prop. 4. Save checkpoint "Before match". 5. Event happens. 6. Refresh. 7. Each prop shows before, now, change. 8. Green up. 9. Red down. 10. A cashed-out prop reads `Closed` or `Reduced`, never a fake market loss.
