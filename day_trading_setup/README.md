# Day Trading Setup

Three **alert-only TradingView indicators** for intraday opening-range
breakout (ORB) trades, LE Trading Academy / Qullamaggie style: wait for a
stock to break a key level, let it pull back to the **8 EMA** (or the broken
level), and enter when a candle **closes right back above the 8 EMA**. They
place no orders and have no Python side. This is a separate project from the
Momentum Strategy Variant H system in the rest of this repo, and shares no
code with it.

TradingView layout name: **Day Trading Set up**

## The layout

| # | Indicator | Source |
|---|---|---|
| 1 | EMA 8 | Built-in *Moving Average Exponential*, length 8 |
| 2 | EMA 21 | Built-in *Moving Average Exponential*, length 21 |
| 3 | EMA 50 | Built-in *Moving Average Exponential*, length 50 |
| 4 | VWAP | Built-in *VWAP* |
| 5 | VRVP | Built-in *Visible Range Volume Profile* |
| 6 | Pivots | Built-in *Pivot Points Standard* (Traditional, daily) |
| – | Volume | Built-in *Volume* (its own pane, as in the reference chart) |
| 7 | **DTS Screener** | [`pine/dts_1_strongest_stocks.pine`](pine/dts_1_strongest_stocks.pine) |
| 8 | **DTS Trend** | [`pine/dts_2_trend_and_breakouts.pine`](pine/dts_2_trend_and_breakouts.pine) |
| 9 | **DTS Entry** | [`pine/dts_3_entry_signals.pine`](pine/dts_3_entry_signals.pine) |

That is 9 indicators on one chart. TradingView's free Basic plan allows only
a few indicators per chart, so this layout needs a paid plan that allows at
least 9.

The DTS scripts draw no EMA, VWAP or pivot lines; the built-ins handle
those. DTS 2 and DTS 3 calculate the 8/21/50 EMAs internally for their
rules, so keep their EMA length inputs equal to your built-in EMAs.

## DTS Screener (screener table)

The table tracks up to 20 tickers. To change the list, open the indicator
settings and go to **Tickers**:

- **Ticker list** has presets:
  - Momentum universe
  - Mega-cap tech (the list from the reference screenshot)
  - Semiconductors
  - High beta / momentum
  - Index ETFs + sectors
- **Custom list** is a text box. Paste or type any tickers, separated by
  commas, spaces or new lines, for example `NVDA, AMD, CRDO, PLTR`.
  - Exchange prefixes are optional; use one (`NYSE:DELL`) only when a ticker
    is ambiguous.
  - Duplicates are ignored, and anything past 20 is dropped.
  - A ticker TradingView can't find shows as `invalid` in its row.
- The default custom list is this repo's momentum universe plus META, AMZN,
  NFLX, ORCL, CRM, QCOM, INTC, ADBE, SPY and QQQ.
- The row for the ticker currently on the chart is highlighted in blue.

Pine can't read your TradingView watchlist, and it can't discover the
market's top gainers by itself. For discovery, use TradingView's Stock
Screener (for example, sorted by premarket % change) and paste the top names
into the custom list.

| Column | Meaning |
|---|---|
| Price / Volume / % Chg | Last price, today's volume (including premarket), and % change vs the prior regular-session close |
| PDH Break / PMH Break | **●** when price is above the level. Otherwise a green bar shows how far price has travelled from the low toward the high. |
| PDL Break / PML Break | **●** when price is below the level. Otherwise a red bar shows how far price has fallen from the high toward the low. |
| Trend | **▲** above PDH **and** PMH · **▼** below PDL **and** PML · **⚠** mixed or inside the range |

Rows are sorted strongest first: ▲ rows, then ⚠, then ▼, with % change as
the tie-break. The best long candidates sit at the top and the best shorts
at the bottom. An `alert()` fires when any ticker flips to ▲ or ▼, so a
single chart alert covers all 20 tickers.

## DTS Trend

- **Trend ribbon:** the band between the 8 and 21 EMAs is **green** in a
  clean uptrend, **red** in a clean downtrend and **purple** in chop. It
  sits underneath your built-in EMA lines.
- **Bar colors:** green or red in a clean trend, **gray in chop**. You can
  set "Dim chop only" or "Off" instead.
- **Clean trend** means 8 > 21 > 50, the 8 and 21 EMAs are at least 0.10
  ATR apart, and the 21 EMA has risen at least 0.05 ATR over 3 bars. A
  clean downtrend is the mirror image.
- **Levels:**
  - PDH / PDL: prior regular-session high and low, solid lines.
  - PMH / PML: premarket high and low, dotted lines. These work even with
    extended hours hidden.
  - ORH / ORL: opening-range high and low, yellow lines. The range is the
    first 5 minutes by default.
  - PWH / PWL: prior-week high and low, thick teal/red lines.
  - Each level gets a filled flag tag at the right edge (`PDH`, `PMH`,
    `PWH`…). Hover a tag to see its price, or switch the tag setting to
    "Name + price".
- **Market structure:** confirmed swings are labelled **HH / LH / HL / LL**.
  When a close breaks the last swing high (or low), a dotted line marks it
  with **BOS** (break of structure). Swing strength is 3 bars by default.
- **Breakout markers:** ▲ `ORH` / `PMH` / `PDH` / `PWH` and ▼ `ORL` / `PML` / `PDL` / `PWL`.
  They're **bright in a clean trend and gray in chop**, which tells you to
  skip the break. The option "Only mark breakouts in a clean trend" hides
  the gray ones.
- **Alerts:** *Clean-trend breakout up*, *Clean-trend breakdown*,
  *Trend turns clean*, *BOS up* and *BOS down*.

## DTS Entry (EMA + level retest)

Long rules (shorts are the mirror image):

1. **Breakout (arm).** In the regular session, a candle closes above
   **ORH** (after the opening range is done), **PMH**, **PDH** or **PWH**, with
   close > 8 EMA > 21 EMA. If several levels break on the same bar, the
   highest is used.
2. **Disarm.** The setup is cancelled by a close below the 21 EMA, or a
   close more than 0.25 ATR back below the broken level (a failed breakout).
3. **Retest.** On a later bar, the low tags the 8 EMA **or** the broken
   level (within 0.15 ATR). A ◆ diamond appears to say "get ready".
4. **ENTRY.** The setup triggers when a bar **closes** with all of these
   true:
   - close > 8 EMA, and it's a green candle
   - close is no more than **0.75 ATR** above the 8 EMA (no chasing)
   - close is still above the broken level
   - the trend is clean
   - price is above VWAP
   - it's between **09:35 and 12:00 New York time**
   - there's no open trade, and fewer than 3 entries today

   The retest and the trigger can happen on the same candle, for example a
   hammer that wicks into the 8 EMA and closes back above it.
5. **Stop and target.**
   - **Stop** = the lowest low since the retest − 0.05 ATR. The signal is
     skipped if the stop would be wider than 2.5 ATR.
   - **Target** = 2R. When it's hit, the stop moves to break-even and the
     rest exits on a close below the 8 EMA.
   - All trades are flat by 15:55.

**A+ setup banner** (top center), like the "A+ OUTSIDE LONG" bar:

- **Top line:**
  - **OUTSIDE LONG** when price is above both PDH and PMH.
  - **OUTSIDE SHORT** when price is below both PDL and PML.
  - Otherwise **INSIDE RANGE - NO EDGE**.
- **Grade:** one point each for a clean trend, the right side of VWAP,
  being beyond the opening range, and being beyond the prior-week high (or
  low).
  - 4 points = **A+**, 3 = **A**, 2 = **B**, 0–1 = **C**.
  - The banner is green for an A/A+ long, red for an A/A+ short, amber for
    a B/C grade, and gray when price is inside the range.
- **Second line:** a progress bar showing how much of the regular session
  has passed, plus the session phase:
  - OPENING RANGE (9:30–10:00)
  - PRIME TIME (10:00–11:30)
  - MIDDAY - CAUTION (11:30–14:00)
  - AFTERNOON (14:00–15:00)
  - POWER HOUR (15:00–16:00)

  For example: `▰▰▰▰▱▱▱▱▱▱  MIDDAY - CAUTION  36%`.
- **Alert:** fires when a new A+ OUTSIDE setup appears. That's the cue to
  watch for the 8 EMA retest entry.

What it draws:

- a **LONG / SHORT label** showing the exact entry, stop and target, plus
  lines for each;
- exit labels (`2R ✓`, `STOP`, `BE`, `TRAIL`, `EOD`);
- a **checklist panel** showing the trend, VWAP side, opening range, and
  where each side stands (`wait for break` → `armed ORH 245.10 → wait
  retest` → `retest ✓ → wait close > 8 EMA` → `IN LONG`).

Signals fire only on **bar close**, so labels never flicker mid-bar. Every
threshold can be changed in the settings.

## Setting up the "Day Trading Set up" layout (one time, about 5 minutes)

TradingView layouts and scripts can only be created inside TradingView, so
these steps have to be done by hand.

1. **New layout:** click the layout name (top right) → **Create new
   layout…** → name it **`Day Trading Set up`**.
2. **Chart:** load a liquid stock on a **5-minute** chart (2-minute also
   works). Turn **extended hours off** under Chart settings → Symbol →
   Session. The DTS scripts still read premarket levels in the background.
3. **Built-ins:** open Indicators and add **Moving Average Exponential**
   three times, setting the lengths to 8, 21 and 50. Then add **VWAP**,
   **Visible Range Volume Profile** and **Pivot Points Standard**.
4. **The three DTS scripts.** For each `.pine` file in `pine/`:
   1. Open the file on GitHub, click **Raw**, and copy all of it.
   2. In TradingView, open the **Pine Editor** (bottom panel) → **New** →
      select all → paste.
   3. Click **Save** and use the name `DTS Screener`, `DTS Trend` or `DTS Entry`
      to match the file.
   4. Click **Add to chart**.

   Once saved, the scripts appear under Indicators → **My scripts** for
   reuse on any chart.
5. **Save** the layout with Ctrl/Cmd+S.

### Alerts ("tell me when to enter")

Click ⏰ **Alert**, then set:

- **Condition:** `DTS Entry`
- **Trigger:** `Any alert() function call`
- **Frequency:** once per bar close
- **Notifications:** turn on app push

This one alert covers entries, the 2R target and exits on that symbol. A
message looks like this:

`DTS: LONG NVDA @ 177.16 | stop 176.40 | target 178.68 (2R) | ORH breakout + 8 EMA retest`

Add a second alert with **Condition** `DTS Screener` →
`Any alert() function call` to get ▲/▼ flips across all 20 tickers.

Each TradingView alert is tied to one symbol. To get entry alerts on several
tickers, create a DTS 3 alert on each one, or use watchlist alerts if your
plan supports them.

## Limitations

- **Request limit:** DTS 1 makes one request per ticker, so at most 20,
  which is under TradingView's limit of 40. `MAX_T` in the script can be
  raised to 40. If it loads slowly on a 1-minute chart, set its
  timeframe to 5.
- **Charts:** built for intraday charts (1–15 min) on US stocks. PMH/PML
  stay blank on symbols that have no premarket session.
- **Not a backtest:** this is a visual and alert tool. Paper-trade the
  signals before sizing up.
