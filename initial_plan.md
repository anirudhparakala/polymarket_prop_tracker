Below is the implementation plan I would give Claude Code. Build this as a **local Python Streamlit app** first. No trading, no wallet signing, no ML, no recommendations.

Polymarket’s relevant endpoint is `GET /positions` on the Data API. It requires a `user` wallet address and returns fields such as `asset`, `size`, `avgPrice`, `initialValue`, `currentValue`, `cashPnl`, `percentPnl`, `totalBought`, `realizedPnl`, `curPrice`, `title`, `eventSlug`, and `outcome`, which are enough for your prop-level dashboard. ([Polymarket Documentation][1])

# Step-by-step implementation plan

## Step 1: Create the project

Project name:

```text
polymarket-prop-tracker
```

Use:

```text
Python
Streamlit
SQLite
Pandas
Requests
```

Folder structure:

```text
polymarket-prop-tracker/
  app.py
  polymarket_client.py
  db.py
  calculations.py
  ui.py
  requirements.txt
  README.md
  data/
    polymarket_tracker.db
```

Keep it modular, but not over-engineered.

## Step 2: Define the exact MVP

The MVP must do only this:

```text
Enter wallet address
Fetch open positions
Display prop table
Save checkpoint
Refresh positions
Compare current values against selected checkpoint
Show positive changes green
Show negative changes red
Detect closed/reduced positions after cashout
```

Do **not** add:

```text
Trading
Private keys
Order placement
Cash-out suggestions
Predictions
ML
WebSockets
Complex portfolio analytics
```

WebSockets exist for near real-time orderbook and trade updates, but they are not needed for V1. Manual refresh and later 30-second polling are enough. ([Polymarket Documentation][2])

## Step 3: Build the Polymarket API client

Create `polymarket_client.py`.

Responsibilities:

```text
Validate wallet address format
Call Polymarket positions endpoint
Return clean position rows
Handle API errors
Handle empty position response
```

Fetch only open/current positions first.

Required request behavior:

```text
Input: wallet address
Call: positions endpoint with user=<wallet>
Optional: sizeThreshold=0
Output: list of position rows
```

Important fields to keep:

```text
asset
conditionId
title
eventSlug
outcome
size
avgPrice
initialValue
currentValue
cashPnl
percentPnl
totalBought
realizedPnl
curPrice
endDate
```

Use `asset` as the main identity key because each prop/outcome position needs a stable matching key between checkpoint and current refresh.

## Step 4: Create the SQLite database

Create `db.py`.

You need three tables.

### Table 1: settings

Purpose: store wallet and optional bankroll locally.

```text
settings
  id
  wallet_address
  starting_bankroll
  created_at
  updated_at
```

Starting bankroll is optional for V1. Your main screen is individual prop movement, not perfect cash accounting.

### Table 2: checkpoints

Purpose: store named moments.

```text
checkpoints
  id
  label
  created_at
```

Example labels:

```text
Before match
After Morocco goal 1
After Morocco cashout
Halftime
Full time
```

### Table 3: checkpoint_positions

Purpose: store the table at the moment of checkpoint.

```text
checkpoint_positions
  id
  checkpoint_id
  asset
  condition_id
  title
  event_slug
  outcome
  size
  avg_price
  stake
  current_value
  cur_price
  cash_pnl
  percent_pnl
  realized_pnl
  created_at
```

The key idea: every checkpoint stores **one row per active prop** at that moment.

## Step 5: Normalize position data

Create `calculations.py`.

Do not trust field names blindly inside the UI. First normalize every API row into your app’s internal shape.

Internal position object:

```text
asset
condition_id
market_title
event_slug
outcome
size
entry_price
current_price
stake
current_value
open_pnl
percent_pnl
realized_pnl
```

Stake logic:

```text
stake = totalBought if available
fallback = initialValue
```

Open PnL logic:

```text
open_pnl = currentValue - stake
```

This gives your “you put $5, now it is $10, so +$5” display.

## Step 6: Build the dashboard UI

Create `app.py` and `ui.py`.

Main layout:

```text
Top section:
  Wallet address input
  Save settings button
  Refresh button
  Save checkpoint button
  Checkpoint label input
  Compare against checkpoint dropdown

Main table:
  Individual prop rows
```

Do not make portfolio value the star of the UI. The table is the product.

Main table columns:

```text
Market
Outcome
Stake
Checkpoint Value
Now
Change Since Checkpoint
Since Entry
Checkpoint Price
Current Price
Price Change
Size at Checkpoint
Current Size
Size Status
```

Sort order:

```text
Largest absolute Change Since Checkpoint first
```

That means after a goal, the biggest movers jump to the top.

## Step 7: Implement checkpoint comparison

When user selects a checkpoint, compare current API rows against checkpoint rows by `asset`.

For each current row:

```text
current asset == checkpoint asset
```

Then calculate:

```text
change_since_checkpoint = current_value - checkpoint_value

price_change_since_checkpoint = current_price - checkpoint_price

size_change = current_size - checkpoint_size

size_change_percent = size_change / checkpoint_size
```

Display:

```text
Positive change = green
Negative change = red
Zero = neutral
```

Example:

| Market         | Outcome | Stake | Checkpoint |    Now | Change |
| -------------- | ------: | ----: | ---------: | -----: | -----: |
| Morocco wins   |     Yes | $5.00 |      $5.00 | $10.00 | +$5.00 |
| 0-0 first half |     Yes | $2.00 |      $2.00 |  $0.00 | -$2.00 |

This is the core feature.

## Step 8: Handle cashouts correctly

This is the most important edge case.

If you cash out, the position size changes. The app must not treat that as a normal market loss.

Use this logic:

```text
If asset existed in checkpoint and exists now with same size:
  Normal open position

If asset existed in checkpoint and exists now with smaller size:
  Reduced position

If asset existed in checkpoint and does not exist now:
  Closed position

If asset did not exist in checkpoint but exists now:
  New position
```

Status labels:

```text
Open
Reduced
Closed
New
Increased
```

For closed positions, show:

```text
Status: Closed
Checkpoint Value: last saved value
Now: $0.00
Change: Closed, not market loss
```

Do not color it as a normal red loss unless you also show that the position disappeared due to size change. Otherwise the dashboard will lie after you manually cash out.

## Step 9: Save another checkpoint after cashout

Workflow should be:

```text
Before game:
  Refresh
  Save checkpoint: Before match

After goal:
  Refresh
  Save checkpoint: After Morocco goal 1

After cashout:
  Refresh
  Save checkpoint: After Morocco cashout
```

After cashout, use `After Morocco cashout` as the new baseline.

This keeps the comparison clean because your position set changed.

## Step 10: Add manual refresh first

V1 should only have manual refresh.

User flow:

```text
Click Refresh
App fetches positions
App recalculates table
App compares against selected checkpoint
```

Do not auto-refresh first. It creates extra state and debugging noise.

Polymarket’s docs list general rate limiting and endpoint throttling through Cloudflare. The `/positions` endpoint exists under the Data API, and your manual refresh usage will be far below typical rate-limit pressure. ([Polymarket Documentation][3])

## Step 11: Add optional auto-refresh later

After V1 works, add:

```text
Auto-refresh toggle
Interval dropdown: 15 sec, 30 sec, 60 sec
Default: 30 sec
```

Keep it simple:

```text
If auto-refresh is on:
  refresh every selected interval
else:
  only refresh when user clicks button
```

No WebSocket. No background worker. Just Streamlit rerun behavior or a simple timer package.

Recommended defaults:

| Mode             |      Interval |
| ---------------- | ------------: |
| Manual           | Click refresh |
| Normal live game |        30 sec |
| Fast mode        |        15 sec |
| Pregame          |        60 sec |

## Step 12: Add visual styling

Profit/loss styling:

```text
Change Since Checkpoint > 0 = green
Change Since Checkpoint < 0 = red
Since Entry > 0 = green
Since Entry < 0 = red
Price Change > 0 = green
Price Change < 0 = red
```

Status styling:

```text
Open = normal
Reduced = yellow/orange label
Closed = gray label
New = blue label
```

Do not add advice text like:

```text
Cash out now
Hold
Good bet
Bad bet
```

Only show numbers.

## Step 13: Add basic summary cards, but keep them secondary

Top cards can show:

```text
Open positions count
Total stake
Current position value
Open PnL
Selected checkpoint
Last refreshed time
```

But the table should remain the main thing.

You care more about individual prop movement than net change, so do not make total PnL visually dominant.

## Step 14: Test with fake data before real Polymarket data

Before connecting everything to live API data, create fake positions:

Checkpoint:

```text
Morocco wins: stake $5, value $5, price 0.50, size 10
0-0 first half: stake $2, value $2, price 0.40, size 5
France 2-1: stake $5, value $5, price 0.20, size 25
```

After goal:

```text
Morocco wins: value $10
0-0 first half: value $0
France 2-1: value $3
```

Expected output:

```text
Morocco wins: +$5 green
0-0 first half: -$2 red
France 2-1: -$2 red
```

Then test cashout:

```text
Morocco wins existed before
Now it disappears or size becomes 0
```

Expected output:

```text
Status: Closed
Do not label as normal -$10 market loss
```

## Step 15: Test with your wallet

Use your real wallet only after fake data passes.

Test sequence:

```text
Enter wallet
Refresh
Confirm positions appear
Save checkpoint: Test checkpoint
Refresh again
Confirm changes are calculated
Cash out a tiny or already-decided position manually on Polymarket if needed
Refresh
Confirm status shows Reduced or Closed
Save checkpoint after cashout
```

## Step 16: Add export later

After the dashboard works, add:

```text
Export current table as CSV
Export current table as PNG
```

CSV is easy. PNG can come later.

Since you currently make manual screenshots, PNG export is useful, but not required for the first working version.

# Build order for Claude Code

Use this exact order.

## Build pass 1: Skeleton

Ask Claude Code:

```text
Create a Python Streamlit project for a local Polymarket prop tracker. Set up app.py, polymarket_client.py, db.py, calculations.py, ui.py, requirements.txt, and README.md. Do not implement trading or authentication. The app should be local only.
```

## Build pass 2: Database

```text
Implement SQLite storage for settings, checkpoints, and checkpoint_positions. Add helper functions to initialize the database, save settings, load settings, create a checkpoint, save checkpoint positions, list checkpoints, and load checkpoint positions.
```

## Build pass 3: API client

```text
Implement a Polymarket Data API client that fetches current positions for a wallet address from the public positions endpoint. Validate 0x wallet format. Normalize the returned rows into internal fields: asset, condition_id, market_title, event_slug, outcome, size, entry_price, current_price, stake, current_value, open_pnl, percent_pnl, realized_pnl.
```

## Build pass 4: Table calculations

```text
Implement comparison logic between current positions and a selected checkpoint. Match rows by asset. Calculate change_since_checkpoint, price_change_since_checkpoint, size_change, size_change_percent, and status. Status should be Open, Reduced, Increased, Closed, or New.
```

## Build pass 5: UI

```text
Build the Streamlit UI. Include wallet input, refresh button, checkpoint label input, save checkpoint button, checkpoint dropdown, summary cards, and the main table. Sort the table by absolute change_since_checkpoint descending. Color positive values green and negative values red. Show Closed and Reduced status clearly.
```

## Build pass 6: Fake data tests

```text
Add a fake data mode so I can test the dashboard without calling the API. Include a before-match dataset, after-goal dataset, and after-cashout dataset. Verify that Morocco wins shows +$5, 0-0 first half shows -$2, and cashed-out positions are marked Closed instead of treated as normal losses.
```

## Build pass 7: Real API test

```text
Connect fake mode and real API mode through a toggle. In real mode, fetch positions from Polymarket for the saved wallet. Ensure empty responses, API errors, invalid wallet addresses, and missing fields are handled cleanly.
```

## Build pass 8: Auto-refresh later

```text
Add optional auto-refresh with interval choices of 15, 30, and 60 seconds. Keep manual refresh as the default. Do not use WebSockets.
```

# Final MVP definition

The first working version is done when this works:

```text
1. You enter wallet.
2. You click Refresh.
3. You see all individual props.
4. You click Save Checkpoint: Before Match.
5. Game event happens.
6. You click Refresh.
7. Each prop shows before, now, and change.
8. Green means up.
9. Red means down.
10. If you cashed out, row says Closed or Reduced instead of showing fake market loss.
```

That is the build. Keep V1 brutally simple.