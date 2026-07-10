# CLAUDE.md

Local-first dashboard that tracks individual Polymarket prop positions and shows how each one moved since a saved checkpoint (e.g. "Before match" → "After Morocco goal 1").

Full scope and build order live in [initial_plan.md](initial_plan.md). Read it before starting a build pass.

## Hard rules

These are not style preferences. Breaking them causes real-world harm.

1. **Read-only. Never trading.** This app only issues `GET` requests to Polymarket's public Data API. Never write code that signs transactions, places orders, or handles a private key, seed phrase, or API secret — not even behind a flag or in a comment. If a task seems to require one, stop and say so.
2. **Never commit a wallet address.** It is not a cryptographic secret, but it is a permanent on-chain identifier: committing it links this public repo to that person's entire betting history. Wallets live only in `data/*.db` (gitignored) or `.env` (gitignored). A pre-commit hook enforces this; never suggest `--no-verify` to get around it.
3. **This repo is public and shared with friends.** It ships code, never data. No wallet, no checkpoints, no `.db` file, no CSV export.
4. **No advice text in the UI.** Show numbers only. Never render "cash out now", "hold", "good bet". The user is looking at real money; the app reports, it does not counsel.

## Commands

Windows venv layout (`Scripts/`, not `bin/`):

```bash
.venv/Scripts/python.exe -m pytest          # tests
.venv/Scripts/python.exe -m streamlit run app.py   # dev server, localhost:8501
.venv/Scripts/python.exe scripts/check_no_secrets.py --install  # (re)install pre-commit hook
```

Recreate the environment from scratch:

```bash
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe scripts/check_no_secrets.py --install
```

## Architecture

One responsibility per module. Keep it modular but not over-engineered.

| File | Owns |
|---|---|
| `app.py` | Streamlit entrypoint, session state, wiring only |
| `polymarket_client.py` | HTTP against the Data API, wallet validation, error/empty handling |
| `calculations.py` | Normalization + checkpoint comparison. Pure functions, no I/O, no Streamlit |
| `db.py` | SQLite. All SQL lives here, nowhere else |
| `ui.py` | Rendering and styling |
| `data/*.db` | The user's local database. Gitignored, never fixture data |

`calculations.py` is pure by design so the comparison logic can be tested without a network or a database. Keep it that way.

### Data flow

`GET /positions?user=<wallet>` → normalize to the internal shape → compare against the selected checkpoint row-by-row → render.

Normalize immediately at the client boundary. Do not let raw API field names (`cashPnl`, `curPrice`, `initialValue`) reach `ui.py`; the API's names and the app's names are allowed to drift.

### Schema

Three tables: `settings` (wallet, optional bankroll), `checkpoints` (id, label, created_at), `checkpoint_positions` (one row per active prop at that moment).

## Domain invariants

**`asset` is the join key.** It is the only stable identity for a prop outcome across a refresh. Never match rows on `title`, `outcome`, or `condition_id` — a market has many outcomes sharing one `condition_id`.

**A cashout is not a market loss.** This is the single most important correctness property in the app, per the plan. When a position's size shrinks or the position vanishes between checkpoint and now, that is the user taking money off the table, not the market moving against them. Deriving status from value alone will make the dashboard lie.

Status comes from comparing **size**, not value:

| Checkpoint | Now | Status |
|---|---|---|
| exists | same size | `Open` |
| exists | smaller size | `Reduced` |
| exists | larger size | `Increased` |
| exists | absent | `Closed` |
| absent | exists | `New` |

A `Closed` row shows `Now = —` (an em dash, never `$0.00` — the app only reads open positions and never sees the cashout proceeds, so `$0.00` would assert a measurement it never made). It must **not** be colored red as a loss. Color `Closed` gray and `Reduced` yellow/orange.

**Stake is `initialValue`** (which equals `size × avgPrice`), NOT `totalBought`. `totalBought` is a *share count*, not dollars — using it as the stake yields a nonsense open PnL (e.g. −$103,707 on a position that is actually up). Then `open_pnl = current_value - stake`, which equals the API's `cashPnl` exactly.

**Sort by `abs(change_since_checkpoint)` descending**, so the biggest movers surface after a goal. This is the whole point of the table.

The table is the product. Summary cards stay secondary — the user cares about individual prop movement, not net PnL, so never make total PnL visually dominant.

## Gotchas

**`pandas.Styler.applymap` does not exist.** This venv runs pandas 3.0.3, which *removed* `applymap` (both `DataFrame.applymap` and `Styler.applymap`) after deprecating it in 2.1. Most examples and recalled snippets still use it. Use `Styler.map` for elementwise styling:

```python
df.style.map(lambda v: "color:red" if v < 0 else "color:green", subset=["Change"])
```

Verified against the installed version. `DataFrame.map` is the elementwise equivalent. pandas 3.0 also defaults string columns to the `str` dtype rather than `object`.

**Polymarket returns an empty list, not an error, for a wallet with no positions.** Render an empty state; do not treat it as a failure.

**Prices are probabilities in `[0, 1]`,** not dollars. A price of `0.50` is 50¢ / an implied 50%. `size` is share count; `size × price ≈ value`.

## Scope discipline

V1 is deliberately, brutally simple. Do not add, even if it seems easy or helpful: trading, wallet signing, order placement, cash-out suggestions, predictions, ML, WebSockets, background workers, complex portfolio analytics, or auto-refresh (auto-refresh is Build pass 8, manual refresh comes first).

Auto-refresh is deferred on purpose: it creates extra state and debugging noise. When it lands, use Streamlit rerun behavior or a simple timer — never a WebSocket or a background thread.

## Testing

Test `calculations.py` against the fake datasets in the plan (Build pass 6) before touching the live API. The comparison logic must produce: `Morocco wins +$5` (green), `0-0 first half -$2` (red), and a cashed-out `Morocco wins` marked `Closed` rather than a fake `-$10` market loss.

Use fake data for tests. Never commit a real wallet address in a fixture — the pre-commit hook will block it. Use `0x` followed by 40 zeros.
