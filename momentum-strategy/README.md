# Momentum Strategy

A swing-trading momentum strategy for TradingView (Pine Script v6), developed and backtested through
several iterations before settling on the current live version (Variant H) — now traded live via
options in an IBKR paper account, not shares.

## Contents

- `pine/momentum_strategy.pine` — the current live strategy script (Variant H: SMA200 + triple-confirmed
  breakout entry, checked hourly with a 6-candle trailing-hour lookback instead of every 10-minute bar,
  swing exit with -10% stop / 2R trim to breakeven / 20-trading-day time exit).
- `docs/momentum_strategy_backtest_record.html` — full backtest record covering every variant tested,
  the 10-ticker universe expansion, the corrected position-level win rate, and the live options trading
  plan (option selection criteria, sizing, concentration caps).
- `scripts/momentum_strategy_scanner.py` — the live Telegram alert scanner (hourly, 10-ticker universe).
- `scripts/momentum_option_picker.py` — picks a qualifying long call (delta/volume/OI/expiry filters,
  1%-of-NLV sizing) for each new signal. Suggestion only, never places an order.
- `scripts/momentum_tastytrade_data.py` — live option chain data (greeks, OI, bid/ask, volume) via
  TastyTrade's streaming API, used by the option picker.

## Status

Variant H is live on the TradingView chart and the Telegram scanner watches the full 10-ticker universe
(DELL, AMD, NVDA, MU, TSLA, AVGO, PLTR, MSFT, AAPL, GOOGL) hourly, 10:00-14:00 ET. Execution moved from
"buy shares" to "buy a call option, IBKR paper account" — every new signal's Telegram alert includes a
suggested contract (strike/expiry/delta/IV/volume/OI) and quantity sized to 1% of account NLV, alongside
the underlying stock signal. It's a suggestion for you to execute manually in TWS, never an automated
order — no order-placement code exists anywhere in this repo.

**Data source:** market data (chain, greeks, OI, bid/ask, volume) comes from TastyTrade, not IBKR — IBKR's
market data proved unreliable during testing (empty/NaN on most attempts). TastyTrade's REST snapshot had
the same problem for equity options specifically, so the picker pulls everything from TastyTrade's DXLink
streaming API instead, which has been reliable. IBKR is used only for the paper account's net liquidation
value (position sizing), nothing else. Confirmed working end-to-end: NVDA → 2026-08-21 210C, delta 0.29,
IV 38%, vol 13,603, OI 45,070, 28 contracts suggested.

Credentials for TastyTrade live in `~/.trading-scanners/tastytrade.env`, copied from the SPX Scalp
project's own `.env` (same underlying account) so the two projects stay independent at runtime.

Open items, tracked in the backtest record but not yet decided on the stock backtest: position sizing
(20% vs. 100% of equity) and the R:R trim threshold (swept 1.5R-3.5R). The day-trade gap strategy
thread (opening-range rejected, gap-short reverted, gap-down long untested) is parked, deprioritized in
favor of the live options plan above.

This is a separate project from SPX Scalp Algo — unrelated strategy, kept in its own repo intentionally.
