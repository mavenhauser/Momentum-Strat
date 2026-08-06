"""
Order-placement check for the momentum trader's equity-option path -
mirrors scripts/check_ibkr_paper_order.py exactly, but for a standard
equity option instead of an SPX index option, validating the generalized
IBKR contract-qualification/order path added to src/ibkr_client.py
(symbol=, trading_class=None) and the TastyTrade DXLink greeks snapshot
added to src/tastytrade_client.py, before
scripts/run_momentum_paper_trader.py relies on either.

Places one 1-contract marketable-limit buy on a cheap, far-OTM monthly
call on the PAPER account, confirms the fill, then immediately sells it
back to close.

Prereqs:
  - Trader Workstation running, logged into the PAPER account (this uses
    IBKR_PORT from .env, which defaults to 7497 = paper TWS - double check
    before running against a real account)
  - .env populated with TT_CLIENT_SECRET / TT_REFRESH_TOKEN (Tastytrade
    OAuth) - run scripts/check_tastytrade_connection.py first to verify

Run from the 'Algo Trading' directory:
  source venv/bin/activate
  python scripts/check_momentum_option_order.py [--ticker PLTR]
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_insync import Stock  # noqa: E402

from src import config  # noqa: E402
from src.ibkr_client import IBKRClient  # noqa: E402
from src.momentum_option_picker import is_monthly_expiry  # noqa: E402
from src.tastytrade_client import TastytradeClient  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default=config.MOMENTUM_TICKERS[0])
    args = parser.parse_args()
    ticker = args.ticker

    client = IBKRClient(client_id=config.MOMENTUM_CHECK_IBKR_CLIENT_ID)
    tt = TastytradeClient()
    print(f"Connecting to IBKR at {client.host}:{client.port} (clientId={client.client_id})...")
    client.connect()
    print("Connecting to Tastytrade...")
    tt.connect()
    print("Connected to both. This places a REAL order on whatever account IBKR_PORT points to - "
          f"confirm {client.port} is your PAPER account before continuing.")

    try:
        contract = Stock(ticker, "SMART", "USD")
        client.ib.qualifyContracts(contract)
        bars = client.ib.reqHistoricalData(
            contract, endDateTime="", durationStr="1 D", barSizeSetting="5 mins",
            whatToShow="TRADES", useRTH=True, formatDate=1,
        )
        if not bars:
            print(f"No historical bars for {ticker} - stopping here.")
            return
        ref_price = bars[-1].close
        print(f"{ticker} reference price (latest historical close): {ref_price}")

        chain = tt.get_option_chain(symbol=ticker)
        cutoff = date.today() + timedelta(days=config.MOMENTUM_TARGET_DTE_MIN)
        candidates = sorted(d for d in chain.keys() if is_monthly_expiry(d) and d >= cutoff)
        if not candidates:
            print(f"No monthly expiry >= {config.MOMENTUM_TARGET_DTE_MIN}d out found for {ticker} - stopping here.")
            return
        expiry = candidates[0]

        calls = sorted(
            (o for o in chain[expiry] if o.option_type.value == "C"),
            key=lambda o: o.strike_price,
        )
        # Far OTM (spot + 20%, nearest listed strike) to keep the test premium cheap.
        far_call = min(calls, key=lambda o: abs(float(o.strike_price) - ref_price * 1.2))
        far_strike = float(far_call.strike_price)
        print(f"Using far-OTM test contract: {ticker} {far_strike}C {expiry}")

        snapshot = tt.get_option_market_snapshot([far_call.streamer_symbol]).get(far_call.streamer_symbol)
        if not snapshot or snapshot.get("ask") is None:
            print("No usable TastyTrade snapshot (bid/ask) for this contract - stopping here.")
            return
        print(f"TastyTrade snapshot - bid: {snapshot['bid']}, ask: {snapshot['ask']}, "
              f"delta: {snapshot['delta']}, theta: {snapshot['theta']}")

        buy_limit = round(snapshot["ask"] * (1 + config.MOMENTUM_LIMIT_SLIPPAGE_PCT), 2)
        print(f"\nPlacing BUY 1 {far_strike}C @ limit {buy_limit} (TastyTrade ask {snapshot['ask']} + "
              f"{config.MOMENTUM_LIMIT_SLIPPAGE_PCT:.0%} slippage buffer)...")
        buy_trade = client.buy_to_open(
            expiry, far_strike, "C", quantity=1, limit_price=buy_limit,
            trading_class=None, symbol=ticker,
        )
        buy_trade = client.wait_for_fill(buy_trade)
        print(f"Buy order status: {buy_trade.orderStatus.status}, "
              f"avgFillPrice: {buy_trade.orderStatus.avgFillPrice}")

        if buy_trade.orderStatus.status != "Filled":
            print("Buy order did not fill within the timeout - not attempting to close. "
                  "Check TWS for the order's actual state.")
            return

        snapshot = tt.get_option_market_snapshot([far_call.streamer_symbol]).get(far_call.streamer_symbol) or {}
        bid = snapshot.get("bid") or buy_trade.orderStatus.avgFillPrice
        sell_limit = round(bid * (1 - config.MOMENTUM_LIMIT_SLIPPAGE_PCT), 2)
        print(f"\nPlacing SELL 1 {far_strike}C @ limit {sell_limit} (TastyTrade bid {bid} - "
              f"{config.MOMENTUM_LIMIT_SLIPPAGE_PCT:.0%} slippage buffer)...")
        sell_trade = client.sell_to_close(
            expiry, far_strike, "C", quantity=1, limit_price=sell_limit,
            trading_class=None, symbol=ticker,
        )
        sell_trade = client.wait_for_fill(sell_trade)
        print(f"Sell order status: {sell_trade.orderStatus.status}, "
              f"avgFillPrice: {sell_trade.orderStatus.avgFillPrice}")
    finally:
        client.disconnect()
        tt.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    main()
