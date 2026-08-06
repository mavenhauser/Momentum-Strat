import asyncio
import threading
from datetime import datetime, timezone

from tastytrade import Session
from tastytrade.dxfeed import Greeks, Quote, Summary, Trade
from tastytrade.instruments import OptionType, get_option_chain
from tastytrade.market_data import get_market_data_by_type
from tastytrade.streamer import DXLinkStreamer

from src import config
from src.options_pricing import EASTERN

# Quotes only - order execution stays on IBKR (src/ibkr_client.py). Uses
# Tastytrade's REST market-data snapshot (get_market_data_by_type) rather
# than the DXLinkStreamer websocket: for a ~60s polling loop a point-in-time
# REST call per iteration is simpler and more robust than managing a
# persistent subscription, and it conveniently returns the day's low premium
# directly (needed by the Range Scalp strategy) instead of us having to track
# a running low ourselves.


class TastytradeClient:
    def __init__(self):
        self._loop = None
        self._thread = None
        self.session = None
        self._chain_cache = {}

    def connect(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self.session = self._run(self._create_session())

    async def _create_session(self):
        session = Session(
            provider_secret=config.TT_CLIENT_SECRET,
            refresh_token=config.TT_REFRESH_TOKEN,
        )
        await session.refresh(force=True)
        return session

    def _run(self, coro, timeout=15):
        if self._loop is None:
            raise RuntimeError("TastytradeClient.connect() must be called first")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    def get_option_chain(self, symbol="SPX"):
        """Today's 0DTE strikes/expirations for `symbol`, cached per session."""
        if symbol not in self._chain_cache:
            self._run(self.session.refresh())
            self._chain_cache[symbol] = self._run(get_option_chain(self.session, symbol))
        return self._chain_cache[symbol]

    def find_option(self, strike, right, expiry, symbol="SPX"):
        """expiry: a date object (use datetime.now(EASTERN).date() for 0DTE)."""
        chain = self.get_option_chain(symbol)
        want_type = OptionType(right)
        for opt in chain.get(expiry, []):
            if opt.strike_price == strike and opt.option_type == want_type:
                return opt
        return None

    async def _fetch_option_quote_snapshot(self, streamer_symbol, timeout):
        async with DXLinkStreamer(self.session) as streamer:
            await streamer.subscribe(Quote, [streamer_symbol])
            await streamer.subscribe(Summary, [streamer_symbol])
            await streamer.subscribe(Trade, [streamer_symbol])

            quotes = await self._collect_events(streamer, Quote, [streamer_symbol], timeout)
            summaries = await self._collect_events(streamer, Summary, [streamer_symbol], timeout)
            trades = await self._collect_events(streamer, Trade, [streamer_symbol], timeout)

        return quotes.get(streamer_symbol), summaries.get(streamer_symbol), trades.get(streamer_symbol)

    def get_quote(self, strike, right, expiry, symbol="SPX"):
        """Live snapshot for one contract: {bid, ask, mark, day_low, ts} or None
        if the contract can't be found or no live tick arrives within
        config.TT_QUOTE_STREAM_TIMEOUT_SECONDS.

        Uses the DXLink streaming API (same path as
        get_option_market_snapshot), not the REST market-data snapshot:
        get_market_data_by_type(options=[...]) was confirmed (2026-08-05)
        to return zero results for every SPX/SPXW 0DTE and SPY option
        contract tested, while indices=[...] and this DXLink stream both
        return real, fresh data - the REST options endpoint appears
        broken/unavailable for this account, independent of liquidity,
        staleness, or 0DTE-ness."""
        option = self.find_option(strike, right, expiry, symbol)
        if option is None:
            return None

        quote, summary, trade = self._run(
            self._fetch_option_quote_snapshot(
                option.streamer_symbol, config.TT_QUOTE_STREAM_TIMEOUT_SECONDS
            ),
            timeout=config.TT_QUOTE_STREAM_TIMEOUT_SECONDS + 10,
        )

        bid = float(quote.bid_price) if quote and quote.bid_price is not None else None
        ask = float(quote.ask_price) if quote and quote.ask_price is not None else None
        mark = (
            (bid + ask) / 2 if bid is not None and ask is not None else
            (float(trade.price) if trade and trade.price is not None else None)
        )
        day_low = float(summary.day_low_price) if summary and summary.day_low_price is not None else None

        if bid is None and ask is None and mark is None:
            return None

        return {
            "bid": bid,
            "ask": ask,
            "mark": mark,
            "day_low": day_low,
            "ts": datetime.now(timezone.utc),
        }

    async def _collect_events(self, streamer, event_class, streamer_symbols, timeout):
        """Pull events of one dxfeed type off the stream until every symbol
        in streamer_symbols has been seen once, or `timeout` seconds elapse -
        whichever comes first. Returns {streamer_symbol: event}, missing any
        symbol whose event never arrived in time."""
        remaining = set(streamer_symbols)
        results = {}
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while remaining:
            time_left = deadline - loop.time()
            if time_left <= 0:
                break
            try:
                event = await asyncio.wait_for(streamer.get_event(event_class), timeout=time_left)
            except asyncio.TimeoutError:
                break
            if event.event_symbol in remaining:
                results[event.event_symbol] = event
                remaining.discard(event.event_symbol)
        return results

    async def _fetch_option_market_snapshot(self, streamer_symbols, timeout):
        async with DXLinkStreamer(self.session) as streamer:
            await streamer.subscribe(Greeks, streamer_symbols)
            await streamer.subscribe(Summary, streamer_symbols)
            await streamer.subscribe(Quote, streamer_symbols)
            await streamer.subscribe(Trade, streamer_symbols)

            greeks = await self._collect_events(streamer, Greeks, streamer_symbols, timeout)
            summaries = await self._collect_events(streamer, Summary, streamer_symbols, timeout)
            quotes = await self._collect_events(streamer, Quote, streamer_symbols, timeout)
            trades = await self._collect_events(streamer, Trade, streamer_symbols, timeout)

        out = {}
        for sym in streamer_symbols:
            g, s, q, t = greeks.get(sym), summaries.get(sym), quotes.get(sym), trades.get(sym)
            out[sym] = {
                "delta": float(g.delta) if g and g.delta is not None else None,
                "theta": float(g.theta) if g and g.theta is not None else None,
                "iv": float(g.volatility) if g and g.volatility is not None else None,
                "open_interest": int(s.open_interest) if s and s.open_interest is not None else None,
                "volume": int(t.day_volume) if t and t.day_volume is not None else None,
                "bid": float(q.bid_price) if q and q.bid_price is not None else None,
                "ask": float(q.ask_price) if q and q.ask_price is not None else None,
            }
        return out

    def get_option_market_snapshot(self, streamer_symbols, timeout=10):
        """Real-time snapshot (delta/theta/iv/open_interest/volume/bid/ask)
        for a batch of equity-option contracts, keyed by streamer_symbol.

        Uses TastyTrade's DXLink streaming API rather than the REST
        market-data snapshot `get_quote` relies on: for real (non-index)
        equity-option strikes, the REST snapshot has been found to return
        empty rows, while DXLink streaming reliably returns a full chain's
        greeks/OI/volume. One-shot: opens a stream, waits up to `timeout`
        seconds per event type for each symbol, then closes it - there's no
        persistent subscription to manage between hourly runs.

        A missing symbol/field in the result (rather than a KeyError) means
        that event never arrived within `timeout` - callers should treat a
        None field as "data unavailable", not "value is zero".
        """
        return self._run(
            self._fetch_option_market_snapshot(streamer_symbols, timeout),
            timeout=timeout + 15,
        )

    def session_low(self, strike, right, expiry, symbol="SPX"):
        """Lowest premium printed today for this contract, or None if unavailable."""
        quote = self.get_quote(strike, right, expiry, symbol=symbol)
        return quote["day_low"] if quote else None

    def get_index_price(self, symbol="SPX"):
        """Live snapshot of the underlying index itself (not an option).
        IBKR's paper account only has a delayed (type 3) SPX/CBOE data
        subscription (see IBKRClient.connect), so the 5-min bar close
        signal detection runs on can be meaningfully stale by the time an
        order is actually about to go out - especially after the
        premium-velocity watch (up to
        config.PREMIUM_VELOCITY_WATCH_MAX_SECONDS). This exists so
        execution (strike selection, fill alerts) can use a genuinely
        live price instead. Returns the price, or None if unavailable or
        stale (same config.TT_QUOTE_MAX_STALENESS_SECONDS threshold as
        get_quote)."""
        self._run(self.session.refresh())
        results = self._run(get_market_data_by_type(self.session, indices=[symbol]))
        if not results:
            return None
        md = results[0]

        if md.updated_at is not None:
            updated_at = md.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            staleness = (datetime.now(timezone.utc) - updated_at).total_seconds()
            if staleness > config.TT_QUOTE_MAX_STALENESS_SECONDS:
                return None

        price = md.last if md.last is not None else md.mark
        return float(price) if price is not None else None

    def disconnect(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)


def today_expiry():
    """0DTE expiration date, ET."""
    return datetime.now(EASTERN).date()
