# Day Trading Setup

An **alert-only TradingView indicator** for intraday opening-range breakout
(ORB) trades, LE Trading Academy / Qullamaggie style: wait for a stock to
break a key level, let it pull back to the **8 EMA** (or the broken level),
and enter when a candle **closes right back above the 8 EMA**. It places no
orders and has no Python side. It's a separate project from the Momentum
Strategy Variant H system in the rest of this repo, and shares no code with
it.

- Script: [`pine/day_trading_setup.pine`](pine/day_trading_setup.pine) (Pine v6, `indicator`, overlay)
- TradingView layout name: **Day Trading Set up**

## What's on the chart

| Piece | What it shows |
|---|---|
| **EMAs 8 / 21 / 50** | 8/21 ribbon is **green** in a clean uptrend, **red** in a clean downtrend and **purple** in chop. The 50 EMA is blue. |
| **VWAP** | Session VWAP (orange). By default longs are only allowed above it and shorts only below it. |
| **Floor pivots** | P / R1 / S1 from the prior regular session, drawn faint. R2 / S2 are optional. They're context only and never trigger anything. Turn them off if they clutter the chart. |
| **PDH / PDL** | Prior-day regular-session high / low (solid green / red lines). |
| **PMH / PML** | Premarket high / low (dotted green / red). These are pulled from extended-hours data, so they work on a regular-hours-only chart. |
| **ORH / ORL** | Opening-range high / low (yellow). The range covers the first 5 minutes by default (1/2/5/10/15/30 are available). |
| **Breakout markers** | ▲ `ORH` / `PMH` / `PDH` below the bar, ▼ `ORL` / `PML` / `PDL` above it. The marker is **bright when the trend is clean and gray in chop**, so you can skip the low-probability breaks. |
| **Bar colors** | Green/red in a clean trend and gray in chop (choose "Dim chop only" or "Off" in settings). |
| **◆ Retest** | A small diamond marks the bar where an armed breakout first pulls back into the 8 EMA or the level. Treat it as "get ready". |
| **LONG / SHORT label** | The exact entry, with the entry price, stop and target printed on the label and drawn as lines. |
| **Exit labels** | `2R ✓ stop → BE`, `STOP`, `BE`, `TRAIL`, `EOD` |
| **Checklist panel** (bottom right) | Trend state, VWAP side, opening range, and where the long and short setups stand (`wait for break` → `armed ORH 245.10 → wait retest` → `retest ✓ → wait close > 8 EMA` → `IN LONG`), plus the open trade. |
| **Screener table** (top right) | The strongest-stocks table described below. |

### Trend vs chop

The trend counts as **clean up** when 8 > 21 > 50, the 8 and 21 are at least
0.10 ATR apart, and the 21 EMA has risen at least 0.05 ATR over the last 3
bars. **Clean down** is the mirror image. Everything else is **chop**.
Raising the spread/slope thresholds makes it stricter.

## Entry rules (long; shorts are the mirror)

1. **Breakout (arm).** During the regular session a candle closes above
   **ORH** (after the opening range is complete), **PMH** or **PDH**, with
   close > 8 EMA > 21 EMA. If several levels break on the same bar, the
   highest one is used.
2. **Disarm.** The setup is cancelled if a candle closes below the 21 EMA,
   or closes more than 0.25 ATR back below the broken level (a failed
   breakout).
3. **Retest.** On a later bar the low tags the 8 EMA **or** the broken level
   (within 0.15 ATR). The ◆ marker appears here.
4. **Trigger = ENTRY.** A bar **closes** with all of these true:
   - close > 8 EMA and close > open (a green candle back over the 8)
   - close is no more than **0.75 ATR** above the 8 EMA ("right above",
     no chasing)
   - close is still above the broken level
   - trend filter (clean uptrend by default) and price above VWAP
   - inside the entry window (09:35–12:00 New York), with no open trade and
     fewer than 3 entries today

   The retest and the trigger can be the same candle, for example a hammer
   that wicks into the 8 EMA and closes back above it.
5. **Stop** = lowest low since the retest − 0.05 ATR. Signals are skipped if
   the stop would be wider than 2.5 ATR. **Target** = 2R. At the target the
   stop moves to break-even, and the rest exits on a close below the 8 EMA
   (or the 21 EMA, set in the options). Everything is flat by 15:55.

Signals fire only on **bar close**, so a label never appears and then
disappears mid-bar. Every number above can be changed in the indicator
settings.

## Strongest-stocks screener

Up to 20 tickers are shown. The defaults are this repo's momentum universe
(NVDA, AMD, AVGO, MU, DELL, TSLA, PLTR, MSFT, AAPL, GOOGL) plus META, AMZN,
NFLX, ORCL, CRM, QCOM, INTC, ADBE, SPY and QQQ. You can edit them in the
settings.

| Column | Meaning |
|---|---|
| Price / Volume / % Chg | Last price, today's volume including premarket, and % change vs the prior regular-session close |
| PDH Break / PMH Break | **●** = price is above the level. Otherwise a green bar shows how far price has travelled from the low toward the high. |
| PDL Break / PML Break | **●** = price is below the level. Otherwise a red bar shows how far price has fallen from the high toward the low. |
| Trend | **▲** above PDH **and** PMH · **▼** below PDL **and** PML · **⚠** mixed or inside the range |

The table is sorted **strongest first**: ▲ rows, then ⚠, then ▼, with %
change as the tie-break. The best long candidates sit at the top and the
best short candidates at the bottom. When the screener-alert option is on,
an alert fires whenever a ticker flips to ▲ or ▼.

## Setting up the "Day Trading Set up" layout

TradingView layouts can only be created in the TradingView app, not from
code. Setup takes about two minutes:

1. **New layout:** open the layout menu (the layout name at the top right of
   the chart) → **Create new layout…** → name it **`Day Trading Set up`**.
2. **Chart:** pick a liquid stock (for example NVDA) on a **5-minute** chart.
   2-minute also works. Turn **extended hours off** (Chart settings →
   Symbol → Session → Regular trading hours). The script still reads
   premarket levels from extended-hours data in the background, and the
   EMAs and VWAP stay clean of thin premarket prints.
3. **Add the indicator:** open the **Pine Editor** (bottom panel) → **New** →
   paste the whole contents of `pine/day_trading_setup.pine` → **Save** as
   `Day Trading Setup` → **Add to chart**.
4. **Leave out the built-in EMA/VWAP/pivot indicators.** The script already
   draws the 3 EMAs, VWAP and pivots, so the layout needs only one indicator
   slot. That matters on plans that limit indicators per chart.
5. **Optional:** pin your watchlist on the right so you can click into the
   tickers the screener ranks as ▲ (or ▼ for shorts).
6. **Save** the layout (cloud icon or Ctrl/Cmd+S).

### Alerts ("tell me when to enter")

Create the alert on the indicator, on the symbol you're watching (⏰ Alert →
Condition: **Day Trading Setup**):

- **`Any alert() function call`** (recommended). One alert covers entries,
  exits, the 2R target, and screener ▲/▼ flips. Messages include the price,
  for example
  `Day Trading Setup: LONG NVDA @ 177.16 | stop 176.40 | target 178.68 (2R) | ORH breakout + 8 EMA retest`.
- Or pick a single condition: **Long entry**, **Short entry**,
  **Retest - get ready**, or **Key level breakout**.

Set the frequency to **Once per bar close**. Turn on app push notifications
or email under the alert's Notifications tab.

Each TradingView alert runs on one symbol. To get entry alerts on several
tickers, create one alert per ticker; if your plan supports watchlist
alerts, you can use those instead. The screener-flip alert covers all 20
screener tickers from a single chart.

## Limitations

- The script uses 21 `request.security` calls (20 screener tickers + the
  chart symbol), which is under TradingView's limit of 40. To save load
  time, turn off the screener or set the screener timeframe to 5.
- It's designed for intraday charts (1–15 min) on US stocks. PMH/PML stay
  blank on symbols that have no premarket session.
- PDH/PDL are the prior *regular-session* high/low, not the extended-hours
  high/low.
- It's a visual and alert tool, not a backtested strategy. Paper-trade the
  signals before sizing up.
