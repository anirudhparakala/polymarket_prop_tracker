# Polymarket Prop Tracker

A local dashboard that tracks your individual Polymarket prop positions and
shows how each one has moved since a moment you marked — before a match,
after a goal, after a cashout.

**Read-only.** It only issues `GET` requests to Polymarket's public Data API.
It never trades, never signs a transaction, never places an order, and never
asks for a private key or seed phrase. If anything in this repo ever appears
to do otherwise, that is a bug — please report it, don't run it.

## Your wallet stays on your machine

This repo ships **code only** — never data. Your wallet address and
checkpoint history live in a local SQLite database
(`data/polymarket_tracker.db` by default), which is gitignored and never
leaves your computer.

A wallet address is not a cryptographic secret, but it *is* a permanent
on-chain identifier: publishing one links this public repo (and your GitHub
identity) to your entire Polymarket betting history. A pre-commit hook scans
every commit for anything address-shaped or key-shaped and blocks it. Never
bypass it with `--no-verify`.

If anything ever asks you for a **private key**, **seed phrase**, or
mnemonic, it is not this project — do not paste it anywhere.

## Which Polymarket are you on?

There are two, and they work completely differently. Pick yours:

| | **Polymarket US** | **Polymarket (crypto)** |
|---|---|---|
| Who | US users; sign up with phone/email | Global; on-chain |
| Funding | Bank card / transfer | USDC on Polygon |
| Wallet address | **None at all** | A proxy wallet (`0x…`) |
| This app needs | An API key in `.env` | Just your public address |

If you fund with a card and have never seen a `0x…` address, you're on
**Polymarket US** — your positions are invisible to the public crypto API, and
you need the API-key route below.

## Setup

Requires Python 3.13.

```bash
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe scripts/check_no_secrets.py --install
```

The last line installs the pre-commit hook described above. Re-run it any
time (e.g. after cloning fresh) — it's idempotent.

### If you're on Polymarket US

Generate an API key at [polymarket.us/developer](https://polymarket.us/developer),
then:

```bash
cp .env.example .env      # then fill in SECTION A
```

**Read this before you paste that key.** Polymarket US does **not** offer
read-only API keys — the same credential that reads your positions **can place
and cancel orders**. We can't scope it down; that's their design.

So this app contains the risk *structurally* rather than by promising to behave:
`polymarket_us_client.py` has **exactly one request in it** — `GET
/v1/portfolio/positions`. There is **no order-placing code anywhere in this
repo**, so a bug cannot trade; the capability doesn't exist in the source. A test
asserts this by parsing the module. (We deliberately don't use Polymarket's
official SDK, whose client exposes `orders.place()` next to
`portfolio.positions()`.)

Still: treat that key like a password. Keep it in `.env` (gitignored, and the
pre-commit hook blocks it). Never paste it into a chat, an issue, or a
screenshot. **Revoke it when you stop using this dashboard.**

Verify it works before trusting any number:

```bash
.venv/Scripts/python.exe scripts/check_us_account.py
```

That prints what you paid, what it's worth, and the derived price for each
position. **Check them against your Polymarket app.** Note that `you paid`
includes fees — a bet whose market hasn't moved can still show a small loss,
because the fee is real money you spent. (Verified: this matches what the
Polymarket app shows.)

### If you're on Polymarket (crypto)

No key needed — your position data is public. All it takes is your proxy wallet
address, from your profile URL (`polymarket.com/profile/0x…`). Check it with:

```bash
.venv/Scripts/python.exe scripts/check_wallet.py 0xYourAddress
```

If it lists your bets, paste that address into the app.

## Run

```bash
.venv/Scripts/python.exe -m streamlit run app.py
```

Opens at `localhost:8501`.

By default the app's database lives at `data/polymarket_tracker.db`
(gitignored, created automatically on first run). To point it somewhere
else — for example to keep multiple wallets in separate files — set the
`POLYMARKET_TRACKER_DB` environment variable to the path you want before
starting Streamlit.

## Try it with no wallet

You don't need a real wallet to see the app work. In the sidebar, tick
**Use fake data** and pick a scenario from the dropdown. There are three,
meant to be walked through in order:

1. **`before_match`** — three open props: `Morocco wins`, `0-0 first half`,
   `France 2-1`. Click **Refresh** to load them, then label a checkpoint
   `Before match` and click **Save checkpoint**.
2. **`after_goal`** — switch the scenario dropdown to `after_goal` and click
   **Refresh** again. Select `Before match` under **Compare against**.
   `Morocco wins` now shows a **Change Since Checkpoint** of **+$5.00** in
   green; `0-0 first half` and `France 2-1` each show **-$2.00** in red.
3. **`after_cashout`** — switch to `after_cashout` and refresh once more.
   `Morocco wins` has been cashed out and no longer appears among open
   positions from the API — the row reads **Closed**, shown in gray with
   `Now` displayed as `—`, *not* as a fabricated market loss. Closed rows
   are also excluded from the summary totals at the top, since the app never
   saw what the position actually sold for.

## Using it with a real wallet

1. Paste your wallet address (`0x` + 40 hex characters) and click
   **Save settings** so it persists between sessions.
2. Click **Refresh** to load your open positions from Polymarket.
3. Give a checkpoint a label (e.g. `Before match`) and click
   **Save checkpoint** to snapshot every currently open position.
4. After something happens — a goal, a line move, a cashout — click
   **Refresh**, then pick your saved checkpoint under **Compare against**.
5. Each row now shows its value at the checkpoint, its value now, and the
   change between them. The table sorts by the size of that change, biggest
   mover first, so the props that moved most after an event surface at the
   top.

## Understanding the table

There are **two different gain/loss measures** in the table, and they answer
different questions:

- **Change Since Checkpoint** — the move in value between your *saved
  checkpoint* and now. This column shows `—` until you have saved at least
  one checkpoint **and** selected it under **Compare against**. A checkpoint
  is a moment you explicitly mark; this column only ever measures movement
  since that exact moment, for whichever checkpoint you have selected.
- **Since Entry** (and the **Open PnL** summary card) — gain or loss versus
  your original stake, from whenever you first opened the position. This
  always has a value for any currently open position, with no checkpoint
  required.

Clicking **Refresh** always updates every position's price, value, and size
— it just doesn't populate **Change Since Checkpoint** unless a checkpoint
exists and is selected.

A cashed-out or partially-cashed-out position is never shown as a market
loss. Status is derived from **size**, not value:

| Checkpoint | Now | Status |
|---|---|---|
| exists | same size | `Open` |
| exists | smaller size | `Reduced` |
| exists | larger size | `Increased` |
| exists | absent | `Closed` |
| absent | exists | `New` |

`Closed` rows are shaded gray and show `Now = —` (the app only reads open
positions; it never saw what a closed position sold for, so it never
invents a dollar figure for it). `Reduced` rows are shaded yellow/orange.
Neither is treated as a loss.

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

## Real-wallet smoke-test checklist

Run this once, after the automated test suite passes, before trusting the
dashboard against real money. Do **not** paste your wallet address into a
commit, a GitHub issue, or a test fixture — use it only in the running app.

1. Start the app, untick **Use fake data**, paste your real wallet, click
   **Save settings**, then click **Refresh**.
2. Confirm positions appear and the count matches what Polymarket's own UI
   shows for that wallet.
3. Label a checkpoint `Test checkpoint` and click **Save checkpoint**.
4. Click **Refresh** again with `Test checkpoint` selected under **Compare
   against**. Every row should read `Open` with a **Change Since
   Checkpoint** of `$0.00` — nothing has changed yet.
5. On Polymarket itself, manually cash out one small or already-decided
   position.
6. Click **Refresh**. That position's row should now read `Reduced` (if
   partially sold) or `Closed` (if fully sold) — never a red market loss.
7. Save a new checkpoint after the cashout and use it as your new baseline
   going forward.

## Not built yet

Deliberately out of scope for this version: auto-refresh, CSV/PNG export,
and anything resembling trading, order placement, or cash-out advice. The
app reports numbers; it does not counsel.
