"""Parse DTS Entry alert messages into events.

TradingView decides every entry, stop, target and exit; these messages are the
only input the executor acts on. Formats are produced by
day_trading_setup/pine/dts_3_entry_signals.pine:

  DTS: LONG NVDA @ 177.16 | stop 176.40 | target 178.68 (2R) | ORH breakout + 8 EMA retest | grade A+ OUTSIDE
  DTS: NVDA hit 2R target @ 178.68 - take partial, stop to break-even 177.16
  DTS: EXIT LONG NVDA (STOP) @ 176.40          (STOP / BE / TRAIL / EOD / TIME)
  DTS: A+ OUTSIDE LONG NVDA @ 177.20 - watch for the 8 EMA retest entry
  DTS: STRONG CLOSE LONG NVDA @ 179.10 | daily 8 EMA 175.20 | range 88% | 15:00 178.40   (v14.0)
"""
import re
from dataclasses import dataclass, field
from typing import Optional

_NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
_TICK = r"([A-Z0-9.!_-]+)"

_ENTRY = re.compile(rf"^DTS: (LONG|SHORT) {_TICK} @ {_NUM} \| stop {_NUM} \| target {_NUM} \({_NUM}R\) \| (\w+) breakout")
_TARGET = re.compile(rf"^DTS: {_TICK} hit {_NUM}R target @ {_NUM} - take partial, stop to break-even {_NUM}")
_EXIT = re.compile(rf"^DTS: EXIT (LONG|SHORT) {_TICK} \((STOP|BE|TRAIL|EOD|TIME)\) @ {_NUM}")
_GRADE = re.compile(r"\| grade (A\+|A|B|C) (OUTSIDE|inside)\s*$")   # since DTS Entry v6.0 (2026-10-16)
_CHOP = re.compile(r"\| choppy \(QQQ crosses (\d+)\)")   # since DTS Entry v13.0 (2026-10-22): half size
_APLUS = re.compile(rf"^DTS: A\+ OUTSIDE (LONG|SHORT) {_TICK} @ {_NUM}")
_STRONG = re.compile(rf"^DTS: STRONG CLOSE (LONG|SHORT) {_TICK} @ {_NUM}")   # since v14.0 (2026-10-26)


def _f(x):
    return float(x.replace(",", ""))


@dataclass
class Signal:
    kind: str                      # ENTRY / TARGET / EXIT / APLUS / STRONG / IGNORED
    ticker: Optional[str] = None
    side: Optional[str] = None     # LONG / SHORT
    price: Optional[float] = None  # entry, target or exit price on TradingView
    stop: Optional[float] = None
    target: Optional[float] = None
    r_multiple: Optional[float] = None
    level: Optional[str] = None    # ORH / PMH / PDH / PWH / ORL / ...
    reason: Optional[str] = None   # exit reason
    grade: Optional[str] = None    # entry's setup grade: "A+ OUTSIDE", "A inside", ... (None before v6.0)
    chop: Optional[int] = None     # QQQ VWAP crosses when the market was flagged choppy (None = not choppy)
    raw: str = field(default="", repr=False)


def parse(message: str) -> Signal:
    text = (message or "").strip()
    m = _ENTRY.match(text)
    if m:
        return Signal("ENTRY", ticker=m.group(2), side=m.group(1), price=_f(m.group(3)), stop=_f(m.group(4)),
                      target=_f(m.group(5)), r_multiple=_f(m.group(6)), level=m.group(7),
                      grade=(lambda g: f"{g.group(1)} {g.group(2)}" if g else None)(_GRADE.search(text)),
                      chop=(lambda c: int(c.group(1)) if c else None)(_CHOP.search(text)), raw=text)
    m = _TARGET.match(text)
    if m:
        return Signal("TARGET", ticker=m.group(1), r_multiple=_f(m.group(2)), price=_f(m.group(3)),
                      stop=_f(m.group(4)), raw=text)
    m = _EXIT.match(text)
    if m:
        return Signal("EXIT", ticker=m.group(2), side=m.group(1), reason=m.group(3), price=_f(m.group(4)), raw=text)
    m = _APLUS.match(text)
    if m:
        return Signal("APLUS", ticker=m.group(2), side=m.group(1), price=_f(m.group(3)), raw=text)
    m = _STRONG.match(text)
    if m:
        return Signal("STRONG", ticker=m.group(2), side=m.group(1), price=_f(m.group(3)), raw=text)
    return Signal("IGNORED", raw=text)
