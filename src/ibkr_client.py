import math

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

    def get_option_bid_ask(self, expiry, strike, right, trading_class=None, symbol=None):
        """Live (bid, ask) for one option contract, from THIS broker's own
        market data - not any other data source. Momentum Strat
        (2026-09-03) prices every order's limit off this: contract
        selection stays on TastyTrade's greeks/volume/OI data, but
        execution happens on IBKR, so the execution price should reference
        IBKR's own live quote - a real incident showed TastyTrade's
        streaming quote for the same contract meaningfully stale vs.
        IBKR's (bid $27.50/ask $27.85 vs. IBKR's real $29.00/$30.45).

        Returns (None, None) if bid/ask aren't both available (no live
        quote this moment) - callers should fall back to something
        reasonable rather than trust a missing/NaN price.

        Unlike get_option_quote() (used by the long-lived SPX Scalp
        process, which keeps its subscriptions open across a whole
        session), this explicitly cancels the market data subscription
        after reading - Momentum Strat's trader reconnects fresh every
        cron invocation but can call this several times within one run,
        and leaving subscriptions open across those calls risks the same
        account-summary-limit class of issue as BUG 5 in the tracker."""
        contract = self._qualify_option(expiry, strike, right, trading_class, symbol)
        ticker = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(2)
        bid, ask = ticker.bid, ticker.ask
        self.ib.cancelMktData(contract)
        if bid is None or ask is None or math.isnan(bid) or math.isnan(ask) or bid <= 0 or ask <= 0:
            return None, None
        return bid, ask

    def get_option_mid_price(self, expiry, strike, right, trading_class=None, symbol=None):
        """Live mid-of-bid-ask for one option contract - see
        get_option_bid_ask's docstring for the full rationale. Returns
        None if no live quote is available."""
        bid, ask = self.get_option_bid_ask(expiry, strike, right, trading_class, symbol)
        if bid is None:
            return None
        return (bid + ask) / 2

    def get_option_position(self, expiry, strike, right, symbol):
        """Current REAL position size for one option contract, straight
        from IBKR's own account data - never from any internal state
        file. 0.0 if no position exists for this contract.

        2026-09-04 (BUG 10 in the tracker): a phantom entry once got
        recorded in state with no matching real fill on IBKR's books; a
        later close then blindly sold the believed quantity against a
        real position of 0 and opened a naked short. This is the
        hard safety check that closes that gap for good - callers must
        check this against what they believe they hold and never sell
        more than what this returns, no matter what state says or how
        state and reality diverged in the first place."""
        if hasattr(expiry, "strftime"):
            expiry = expiry.strftime("%Y%m%d")
        for p in self.ib.positions():
            c = p.contract
            if (
                c.secType == "OPT" and c.symbol == symbol
                and c.lastTradeDateOrContractMonth == expiry
                and float(c.strike) == float(strike) and c.right == right
            ):
                return p.position
        return 0.0

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
        codebase's synchronous polling style rather than async order events.

        2026-09-04: on timeout, actively cancels the order instead of just
        walking away from it. A prior version left an unfilled order
        resting live on IBKR's book indefinitely (until its own TIF
        expired - end of day for a DAY order) - a real incident (DELL)
        had exactly this happen: an abandoned BUY order sat open all day,
        then blocked every later close attempt on the same contract with
        Error 201 ("Cannot have open orders on both sides of the same US
        Option contract"), since IBKR won't allow a resting buy and a
        resting sell open on the same contract at once. If the order had
        already partially filled before the timeout, cancelling only
        cancels the unfilled remainder - the partial fill stands, and
        orderStatus.filled correctly reflects just that partial amount."""
        timeout_seconds = timeout_seconds or config.ORDER_FILL_TIMEOUT_SECONDS
        elapsed = 0
        terminal_states = ("Filled", "Cancelled", "ApiCancelled")
        while trade.orderStatus.status not in terminal_states and elapsed < timeout_seconds:
            self.ib.sleep(1)
            elapsed += 1
        if trade.orderStatus.status not in terminal_states:
            self.ib.cancelOrder(trade.order)
            self.ib.sleep(2)  # let the cancellation register before the caller reads orderStatus
        return trade
