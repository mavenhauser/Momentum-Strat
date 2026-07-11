# Momentum Strategy

A swing-trading momentum strategy for TradingView (Pine Script v6), developed and backtested through
several iterations before settling on the current live version (Variant G).

## Contents

- `pine/momentum_strategy.pine` — the current live strategy script (Variant G: SMA200 + triple-confirmed
  breakout entry, swing exit with -10% stop / 2R trim to breakeven / 20-trading-day time exit).
- `docs/momentum_strategy_backtest_record.html` — full backtest record covering every variant tested
  (entry/exit logic, per-ticker results, and why each did or didn't work), across a 5-ticker pilot
  (DELL, AMD, NVDA, MU, TSLA).

## Status

Variant G is live on the TradingView chart as of 2026-07-12. Next step: port this logic into the
Telegram alert scanner so it matches the backtested strategy exactly.

This is a separate project from SPX Scalp Algo — unrelated strategy, kept in its own repo intentionally.
