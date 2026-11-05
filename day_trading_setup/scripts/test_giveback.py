"""Compare runner giveback levels (40/50/60%) on every DTS trade that reached 2R,
using real 5-minute stock bars from the TastyTrade feed. Results are in R (stock
move / initial risk), so they ignore option theta. First run 2026-10-19 (tracker note).

  venv/bin/python3 day_trading_setup/scripts/test_giveback.py
"""
import asyncio, json, sys, statistics as st
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0,'.')
from src.tastytrade_client import config
from tastytrade import DXLinkStreamer, Session
from tastytrade.dxfeed import Candle
NY=ZoneInfo("America/New_York")
s=json.load(open('state/dts_positions_dry.json'))
trades=[t for t in s['closed']+list(s['positions'].values()) if t.get('opened_at','')>='2026-10-14T09:30' and t.get('trimmed') and t.get('tv_target')]
tks=sorted({t['ticker'] for t in trades})
async def get():
    sess=Session(provider_secret=config.TT_CLIENT_SECRET, refresh_token=config.TT_REFRESH_TOKEN)
    out={k:{} for k in tks}
    async with DXLinkStreamer(sess) as stx:
        await stx.subscribe_candle(tks,"5m",datetime(2026,10,14,9,30,tzinfo=NY))
        loop=asyncio.get_event_loop(); dl=loop.time()+60
        while loop.time()<dl:
            try: c=await asyncio.wait_for(stx.get_event(Candle),6)
            except asyncio.TimeoutError: break
            b=c.event_symbol.split('{')[0]
            if b in out and c.time and c.close is not None: out[b][int(c.time)]=(float(c.high),float(c.low),float(c.close))
    return out
bars=asyncio.run(get())
def sim(t, g):
    e,stp,tgt=t['tv_entry'],t['tv_stop'],t['tv_target']; L=t['side']=='LONG'; R=abs(e-stp)
    t0=int(datetime.fromisoformat(t['opened_at']).timestamp()*1000)
    ks=[k for k in sorted(bars[t['ticker']]) if k>=t0-t0%300000]
    day=datetime.fromisoformat(t['opened_at']).astimezone(NY).date()
    best=e; hit=False
    for k in ks:
        dt=datetime.fromtimestamp(k/1000,NY)
        h,l,c=bars[t['ticker']][k]
        if (dt.date()-day).days>3: break
        best=max(best,h) if L else min(best,l)
        if not hit and ((h>=tgt) if L else (l<=tgt)): hit=True; continue
        if hit:
            if (l<=e) if L else (h>=e): return 0.0, dt.strftime('%m-%d %H:%M')          # BE stop
            lvl=best-(best-e)*g if L else best+(e-best)*g
            if (c<lvl) if L else (c>lvl): return ((c-e) if L else (e-c))/R, dt.strftime('%m-%d %H:%M')
            if dt.hour==15 and dt.minute>=50 and (datetime.fromisoformat(t['expiry']).date()==dt.date()):
                return ((c-e) if L else (e-c))/R, dt.strftime('%m-%d %H:%M')+' exp'
    return None, '-'
print(f"{'trade':24} {'40%':>12} {'50%':>12} {'60%':>12}")
res={g:[] for g in (0.4,0.5,0.6)}
for t in sorted(trades,key=lambda x:x['opened_at']):
    row=[]
    for g in (0.4,0.5,0.6):
        r,w=sim(t,g); row.append(f"{r:+.1f}R {w[6:11] if r is not None else ''}" if r is not None else '   -'); 
        if r is not None: res[g].append(r)
    print(f"{t['opened_at'][5:10]} {t['ticker']:5} {t['side']:5}  "+"  ".join(f"{x:>12}" for x in row))
for g,v in res.items(): print(f"giveback {g:.0%}: runner exits n={len(v)}  mean {st.mean(v):+.2f}R  median {st.median(v):+.2f}R  total {sum(v):+.1f}R")
