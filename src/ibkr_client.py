from ib_insync import IB, Index, LimitOrder, Option, Stock

from src import config


class IBKRClient:
    """Thin wrapper around ib_insync for SPX index + options data."""

    def __init__(self, host=None, port=None, client_id=None):
        self.ib = IB()
        self.host = host or config.IBKR_HOST
        self.port = port or config.IBKR_PORT
        self.client_id = client_id or config.IBKR_CLIENT_ID

    def connect(self):
        self.ib.connect(self.host, self.port, clientId=self.client_id)
        # Paper accounts often lack a live SPX/CBOE index data subscription;
        # fall back to delayed data (type 3) so quotes aren't just NaN.
        self.ib.reqMarketDataType(3)
        return self.ib

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()

    def get_spx_index_price(self):
        """Return the current SPX index ticker (last/close available on it)."""
        spx = Index("SPX", "CBOE")
        self.ib.qualifyContracts(spx)
        ticker = self.ib.reqMktData(spx, "", False, False)
        self.ib.sleep(2)
        return ticker

    def get_spx_historical_bars(self, duration="30 D", bar_size="5 mins", end_datetime=""):
        """Fetch historical SPX index OHLC bars."""
        spx = Index("SPX", "CBOE")
        self.ib.qualifyContracts(spx)
        return self.ib.reqHistoricalData(
            spx,
            endDateTime=end_datetime,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )

    def get_spy_historical_bars(self, duration="30 D", bar_size="5 mins", end_datetime=""):
        """Fetch historical SPY bars - used as SPX's volume proxy for VWAP
        (SPX is a cash index with no real trade volume of its own; both IBKR
        and Tastytrade confirmed this - see src.signals.attach_proxy_volume)."""
        spy = Stock("SPY", "SMART", "USD")
        self.ib.qualifyContracts(spy)
        return self.ib.reqHistoricalData(
            spy,
            endDateTime=end_datetime,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )

    def get_option_chain_params(self):
        """Return available expirations/strikes for SPX options (SPX + SPXW)."""
        spx = Index("SPX", "CBOE")
        self.ib.qualifyContracts(spx)
        return self.ib.reqSecDefOptParams(spx.symbol, "", spx.secType, spx.conId)

    def get_option_quote(self, expiry, strike, right, trading_class="SPXW", symbol="SPX"):
        """Fetch a live quote for a single option contract.

        expiry: 'YYYYMMDD'
        right: 'C' or 'P'
        """
        contract = self._qualify_option(expiry, strike, right, trading_class, symbol)
        ticker = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(2)
        return ticker

    def _qualify_option(self, expiry, strike, right, trading_class="SPXW", symbol="SPX"):
        """expiry: 'YYYYMMDD' string, or a date/datetime (e.g. from
        tastytrade_client.today_expiry()) - normalized to IBKR's required
        string format either way. ib_insync does NOT do this conversion
        itself; passing a raw date object silently fails contract
        qualification (Error 200: Invalid value in field, Unknown contract).

        trading_class: pass "SPXW" (the default) for SPX weeklies; pass
        None/"" for standard equity options, which don't use a separate
        weekly trading class - IBKR resolves the right contract from
        symbol/expiry/strike/right alone without it.
        """
        if hasattr(expiry, "strftime"):
            expiry = expiry.strftime("%Y%m%d")
        kwargs = dict(
            symbol=symbol,
            lastTradeDateOrContractMonth=expiry,
            strike=strike,
            right=right,
            exchange="SMART",
            currency="USD",
        )
        if trading_class:
            kwargs["tradingClass"] = trading_class
        contract = Option(**kwargs)
        self.ib.qualifyContracts(contract)
        return contract

    def buy_to_open(self, expiry, strike, right, quantity, limit_price, trading_class="SPXW", symbol="SPX"):
        """Place a limit BUY to open `quantity` contracts. Caller computes
        limit_price (e.g. live ask + slippage buffer, or a Range-Scalp-style
        day-low-minus-buffer) - this method just places whatever it's given.

        tif="DAY" is explicit (2026-08-03: a blank/default tif left TWS's own
        order-preset auto-correction to fill it in, which triggered an
        immediate cancellation - Error 10349, "Order TIF was set to DAY
        based on order preset" - on a live equity-option test order.
        Explicit DAY avoids that path entirely)."""
        contract = self._qualify_option(expiry, strike, right, trading_class, symbol)
        order = LimitOrder("BUY", quantity, round(limit_price, 2), tif="DAY")
        return self.ib.placeOrder(contract, order)

    def sell_to_close(self, expiry, strike, right, quantity, limit_price, trading_class="SPXW", symbol="SPX"):
        """Place a limit SELL to close `quantity` contracts - used for trims,
        stop/breakeven-stop, and EOD close alike; only quantity/price differ.
        tif="DAY" explicit - see buy_to_open's docstring."""
        contract = self._qualify_option(expiry, strike, right, trading_class, symbol)
        order = LimitOrder("SELL", quantity, round(limit_price, 2), tif="DAY")
        return self.ib.placeOrder(contract, order)

    def get_account_value(self, tag="NetLiquidation"):
        """Fetch a single account summary value (e.g. Net Liquidation Value)
        for the connected account. Returns None if the tag isn't present."""
        values = self.ib.accountSummary()
        if not values:
            self.ib.sleep(2)
            values = self.ib.accountSummary()
        for item in values:
            if item.tag == tag:
                return float(item.value)
        return None

    def wait_for_fill(self, trade, timeout_seconds=None):
        """Poll a Trade until it's filled or cancelled, or timeout_seconds
        elapses (default config.ORDER_FILL_TIMEOUT_SECONDS). Matches this
        codebase's synchronous polling style rather than async order events."""
        timeout_seconds = timeout_seconds or config.ORDER_FILL_TIMEOUT_SECONDS
        elapsed = 0
        terminal_states = ("Filled", "Cancelled", "ApiCancelled")
        while trade.orderStatus.status not in terminal_states and elapsed < timeout_seconds:
            self.ib.sleep(1)
            elapsed += 1
        return trade
