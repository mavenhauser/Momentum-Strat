# Momentum Strat

Autonomous options trading system for **Momentum Strategy — Variant H**: a
long-only equity momentum strategy (SMA200 trend filter + triple-confirmed
breakout, checked hourly with a trailing 6-candle lookback, swing exit with
a stop/2R-trim/time-exit) executed as options against an **Interactive
Brokers paper account**. See
[`docs/momentum_strategy_backtest_record.html`](docs/momentum_strategy_backtest_record.html)
for the full backtest/variant history (A through I) this implements, and
[`docs/momentum_live_trading_dev_tracker.html`](docs/momentum_live_trading_dev_tracker.html)
for the live-deployment build log (bugs found and fixed, current status).

This repo was split out of a combined `Algo Trading` repo on 2026-08-06,
which originally held both this system and an unrelated SPX 0DTE scalping
strategy. A handful of broker/data-client files are intentionally
duplicated rather than shared between the two repos — see the tracker doc
above for why.

## How it works

- **Entry logic**: [`src/momentum_signals.py`](src/momentum_signals.py) —
  SMA200 trend filter, triple-confirmed breakout (prior-day high, premarket
  high, fresh high-of-day), evaluated only at the top of each hour over the
  trailing 6 x 10-min bars, plus the shared IBKR daily/intraday bar fetchers.
- **Contract selection**: [`src/momentum_option_picker.py`](src/momentum_option_picker.py) —
  nearest monthly expiry ≥45 days out, filtered to delta>0.25 / volume>500 /
  open interest>1000, cheapest survivor.
- **Live trader**: [`scripts/run_momentum_paper_trader.py`](scripts/run_momentum_paper_trader.py) —
  the autonomous entrypoint. Stateless per invocation (state persisted to
  `state/momentum_positions.json`); manages open positions first (stop /
  catalyst / trim / time-exit), then scans the 10-ticker universe for new
  entries, respecting concurrency and cluster-exposure caps. Supports
  `--dry-run`. Guards against running against anything but a known paper
  port, and against overlapping runs via a PID lock
  (`state/run_momentum_paper_trader.lock`).
- **Order execution**: entirely on IBKR (paper). TastyTrade supplies
  real-time option quotes/greeks via DXLink streaming only — it never
  places an order.
- **Pine Script**: [`pine/momentum_strategy_variant_h.pine`](pine/momentum_strategy_variant_h.pine) —
  the live TradingView strategy, for visual/Strategy-Tester confirmation
  alongside the Python system.

## Standing parameters

- Universe: DELL, AMD, NVDA, MU, TSLA, AVGO, PLTR, MSFT, AAPL, GOOGL
- Sizing: 1% of account NLV (premium budget) per position
- Caps: 4 concurrent positions max, 2 max within the semis/AI-hardware
  cluster (NVDA/AMD/MU/AVGO/DELL)
- Catalyst exit: `abs(theta) > delta`, OR still open during the contract's
  own expiry week — whichever fires first, ahead of the trim/time-exit rules

## Setup

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in IBKR / Tastytrade / Telegram credentials
```

Run a dry cycle (no orders placed):

```
python scripts/run_momentum_paper_trader.py --dry-run
```

Validate the equity-option order path before trusting a live run:

```
python scripts/check_momentum_option_order.py
```

Run the live trader once (meant to be invoked on a schedule — see the
crontab line noted in `scripts/run_momentum_paper_trader.sh`, or install
your own):

```
python scripts/run_momentum_paper_trader.py
```

## Disclaimer

This is a personal paper-trading system, not investment advice. Confirm
`IBKR_PORT` in `.env` points at a paper account (7497 = paper TWS, 4002 =
paper Gateway) before ever running it unattended.
