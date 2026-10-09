// Builds the day's 20-ticker list for DTS Screener and (with --apply) writes it into the
// indicator's "Custom list" on the TradingView tab whose layout is "Day Trading Set Up".
//
//   10  Momentum Strategy universe (MOMENTUM_TICKERS in src/config.py - follows that list)
//    2  index ETFs (SPY, QQQ)
//    8  mega caps with the biggest premarket move today (changes every trading day)
//
// Mega-cap pick: US primary common stocks with market cap >= MIN_CAP, not already in the
// list, with at least MIN_PM_VOL premarket shares, ranked by absolute premarket % change.
// If fewer than 8 qualify (run outside premarket), the rest are filled by the last
// session's absolute % change.
//
// Usage:  node update_screener_list.mjs            dry run: print the list
//         node update_screener_list.mjs --apply    also write it into TradingView
// Prints JSON. Exit 1 + {"error": ...} if the data feed or TradingView can't be reached.
import { createRequire } from 'module';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';

const LAYOUT = 'Day Trading Set Up';
const STUDY = 'DTS Screener';
const ETFS = ['SPY', 'QQQ'];
const N_MEGA = 8;
const MIN_CAP = 200e9;
const MIN_PM_VOL = 20000;
// Second share classes of names already covered, and classes with no usable premarket.
const EXCLUDE = new Set(['GOOG', 'BRK.A', 'BRK.B']);
const MOMENTUM_FALLBACK = ['DELL', 'AMD', 'NVDA', 'MU', 'TSLA', 'AVGO', 'PLTR', 'MSFT', 'AAPL', 'GOOGL'];

const apply = process.argv.includes('--apply');
const done = (code, obj) => { process.stdout.write(JSON.stringify(obj, null, 2) + '\n'); process.exit(code); };
const timer = setTimeout(() => done(1, { error: 'timed out' }), 40000);

// The momentum list is read from wherever it is defined, first hit wins: the repo's
// src/config.py (when run from the repo), then the deployed live scanner in
// ~/.momentum-scanner (when run from the scheduled copy), then the built-in fallback.
function momentumTickers() {
  const sources = [
    [fileURLToPath(new URL('../../src/config.py', import.meta.url)), 'MOMENTUM_TICKERS', 'src/config.py'],
    [`${process.env.HOME}/.momentum-scanner/momentum_strategy_scanner.py`, 'TICKERS', '~/.momentum-scanner scanner'],
  ];
  for (const [path, name, label] of sources) {
    try {
      const m = readFileSync(path, 'utf8').match(new RegExp(`^${name}\\s*=\\s*\\[([^\\]]*)\\]`, 'm'));
      const list = m ? [...m[1].matchAll(/["']([A-Z.]+)["']/g)].map(x => x[1]) : [];
      if (list.length > 0) return { list, source: label };
    } catch {}
  }
  return { list: MOMENTUM_FALLBACK, source: 'built-in fallback' };
}

async function megaCaps(taken) {
  const res = await fetch('https://scanner.tradingview.com/america/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      filter: [
        { left: 'market_cap_basic', operation: 'egreater', right: MIN_CAP },
        { left: 'type', operation: 'equal', right: 'stock' },
        { left: 'is_primary', operation: 'equal', right: true },
      ],
      markets: ['america'],
      columns: ['name', 'market_cap_basic', 'change', 'premarket_change', 'premarket_volume'],
      sort: { sortBy: 'market_cap_basic', sortOrder: 'desc' },
      range: [0, 150],
    }),
  });
  if (!res.ok) throw new Error('screener feed returned HTTP ' + res.status);
  const pool = (await res.json()).data
    .map(r => ({ symbol: r.s, name: r.d[0], cap_bn: Math.round(r.d[1] / 1e9), change: r.d[2], pm_change: r.d[3], pm_volume: r.d[4] }))
    .filter(r => !taken.has(r.name) && !EXCLUDE.has(r.name));
  const byAbs = key => (a, b) => Math.abs(b[key]) - Math.abs(a[key]);
  const picks = pool
    .filter(r => r.pm_change != null && (r.pm_volume || 0) >= MIN_PM_VOL)
    .sort(byAbs('pm_change')).slice(0, N_MEGA).map(r => ({ ...r, ranked_by: 'premarket' }));
  if (picks.length < N_MEGA) {
    const have = new Set(picks.map(r => r.name));
    picks.push(...pool.filter(r => !have.has(r.name) && r.change != null)
      .sort(byAbs('change')).slice(0, N_MEGA - picks.length).map(r => ({ ...r, ranked_by: 'last session' })));
  }
  return { picks, pool_size: pool.length };
}

async function writeToTradingView(listText) {
  const require = createRequire('/Users/James Woo/tradingview-mcp/package.json');
  const CDP = require('chrome-remote-interface');
  const targets = await (await fetch('http://localhost:9222/json/list')).json();
  const pages = targets.filter(t => t.type === 'page' && /tradingview\.com\/chart/i.test(t.url));
  if (pages.length === 0) throw new Error('no TradingView chart tab found (is TradingView open with CDP on :9222?)');
  const expr = `(function(){
    var api = window.TradingViewApi;
    if (!api || typeof api.layoutName !== 'function' || api.layoutName() !== ${JSON.stringify(LAYOUT)}) return { skip: true };
    var chart = api.activeChart();
    var s = chart.getAllStudies().filter(function(x){ return x.name === ${JSON.stringify(STUDY)}; })[0];
    if (!s) return { error: ${JSON.stringify(STUDY + ' is not on the ' + LAYOUT + ' chart')} };
    var study = chart.getStudyById(s.id);
    var inputs = study.getInputValues();
    var before = null;
    inputs.forEach(function(i){
      if (i.id === 'in_0') i.value = 'Custom list';
      if (i.id === 'in_1') { before = i.value; i.value = ${JSON.stringify(listText)}; }
    });
    if (before === null) return { error: 'Custom list input not found on ' + s.name };
    study.setInputValues(inputs);
    var after = study.getInputValues().filter(function(i){ return i.id === 'in_1'; })[0].value;
    return { before: before, after: after, symbol: chart.symbol() };
  })()`;
  // The layout can be open in more than one tab; write to all of them so a stale copy
  // can't autosave the old list back.
  const written = [];
  for (const t of pages) {
    let c;
    try {
      c = await CDP({ host: 'localhost', port: 9222, target: t.id });
      const r = await c.Runtime.evaluate({ expression: expr, returnByValue: true });
      const v = r.result && r.result.value;
      if (!v || v.skip) continue;
      if (v.error) throw new Error(v.error);
      if (v.after !== listText) throw new Error('TradingView did not keep the new list');
      written.push({ tab: t.id, chart_symbol: v.symbol, previous_list: v.before });
    } finally {
      if (c) { try { await c.close(); } catch {} }
    }
  }
  if (written.length === 0) throw new Error(`no open TradingView tab has the "${LAYOUT}" layout`);
  return written;
}

try {
  const mom = momentumTickers();
  const fixed = [...mom.list, ...ETFS];
  const { picks, pool_size } = await megaCaps(new Set(fixed));
  if (picks.length < N_MEGA) throw new Error(`only ${picks.length} mega caps available from the feed`);
  // The daily names carry their exchange so one-letter tickers (V, C) resolve to the right stock.
  const tickers = [...fixed, ...picks.map(p => p.symbol)];
  const listText = tickers.join(', ');
  const out = {
    as_of_new_york: new Date().toLocaleString('sv-SE', { timeZone: 'America/New_York' }),
    momentum: { tickers: mom.list, source: mom.source },
    etfs: ETFS,
    mega_caps: picks.map(p => ({ ticker: p.name, premarket_pct: p.pm_change == null ? null : +p.pm_change.toFixed(2), premarket_volume: p.pm_volume, last_session_pct: p.change == null ? null : +p.change.toFixed(2), market_cap_bn: p.cap_bn, ranked_by: p.ranked_by })),
    mega_cap_pool: pool_size,
    list: listText,
    applied: false,
  };
  if (apply) { out.tradingview = await writeToTradingView(listText); out.applied = true; }
  clearTimeout(timer);
  done(0, out);
} catch (e) {
  done(1, { error: String(e && e.message || e) });
}
