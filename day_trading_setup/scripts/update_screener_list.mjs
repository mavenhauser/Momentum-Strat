// Builds the day's 20-ticker list for DTS Screener and (with --apply) writes it into the
// indicator's "Custom list" on the TradingView tab whose layout is "Day Trading Set Up".
//
//   fixed  Momentum Strategy universe (MOMENTUM_TICKERS in src/config.py - follows that
//          list) plus any Magnificent 7 name not already in it. 12 tickers today.
//   daily  the remaining slots up to 20 (8 today): mega caps that are up the most in
//          premarket. Changes every trading day.
//
// Daily pick: US primary common stocks with market cap >= MIN_CAP, not already in the
// list, with at least MIN_PM_VOL premarket shares, ranked by premarket % change, highest
// first. If too few qualify (run outside premarket), the rest are filled by the last
// session's % change, highest first.
//
// With --alerts it also keeps TradingView alerts in step with the list: one "DTS Entry"
// alert per ticker. What an alert sends is decided by the indicator's own settings on the
// chart (entries always; new A+ setups and 2R / exit messages each have a switch), so
// changing a switch there changes the alerts on the next run. Alerts for tickers that left
// the list are deleted, missing ones created, and any that are stopped or differ from the
// chart's script version, settings or the notification choices below are replaced. Only
// alerts whose name starts with "DTS Entry · " or equals "DTS Screener · flips" are ever
// touched. Notifications: app push and on-screen pop-up; no sound, webhook or email.
// The screener-flip alert is switched off (WITH_SCREENER_ALERT); a leftover one is removed.
//
// Usage:  node update_screener_list.mjs                    dry run: print the list
//         node update_screener_list.mjs --apply            also write it into TradingView
//         node update_screener_list.mjs --apply --alerts   and sync the alerts
//         node update_screener_list.mjs --alerts-plan      show what --alerts would change
// Prints JSON. Exit 1 + {"error": ...} if the data feed or TradingView can't be reached,
// or if any alert could not be created or deleted.
import { createRequire } from 'module';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';

const LAYOUT = 'Day Trading Set Up';
const STUDY = 'DTS Screener';
const MAG7 = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA'];
const TOTAL = 20;
const MIN_CAP = 200e9;
const MIN_PM_VOL = 20000;
// Second share classes of names already covered, and classes with no usable premarket.
const EXCLUDE = new Set(['GOOG', 'BRK.A', 'BRK.B']);
const MOMENTUM_FALLBACK = ['DELL', 'AMD', 'NVDA', 'MU', 'TSLA', 'AVGO', 'PLTR', 'MSFT', 'AAPL', 'GOOGL'];

const ENTRY_STUDY = 'DTS Entry';
const ENTRY_PREFIX = 'DTS Entry · ';
const SCREENER_ALERT = 'DTS Screener · flips';
// Alerts run on the same real-time feed the charts use (Cboe One). The screener alert needs
// a symbol only as its 5-minute clock, so it sits on SPY.
const FEED = 'BATS';
const SCREENER_ALERT_SYMBOL = 'BATS:SPY';
const WITH_SCREENER_ALERT = false;

const apply = process.argv.includes('--apply');
const alertsPlan = process.argv.includes('--alerts-plan');
const alerts = process.argv.includes('--alerts') || alertsPlan;
const done = (code, obj) => { process.stdout.write(JSON.stringify(obj, null, 2) + '\n'); process.exit(code); };
const timer = setTimeout(() => done(1, { error: 'timed out' }), alerts ? 150000 : 40000);

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

async function megaCaps(taken, count) {
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
  const highest = key => (a, b) => b[key] - a[key];
  const picks = pool
    .filter(r => r.pm_change != null && (r.pm_volume || 0) >= MIN_PM_VOL)
    .sort(highest('pm_change')).slice(0, count).map(r => ({ ...r, ranked_by: 'premarket' }));
  if (picks.length < count) {
    const have = new Set(picks.map(r => r.name));
    picks.push(...pool.filter(r => !have.has(r.name) && r.change != null)
      .sort(highest('change')).slice(0, count - picks.length).map(r => ({ ...r, ranked_by: 'last session' })));
  }
  return { picks, pool_size: pool.length };
}

async function chartPages() {
  const require = createRequire('/Users/James Woo/tradingview-mcp/package.json');
  const CDP = require('chrome-remote-interface');
  const targets = await (await fetch('http://localhost:9222/json/list')).json();
  const pages = targets.filter(t => t.type === 'page' && /tradingview\.com\/chart/i.test(t.url));
  if (pages.length === 0) throw new Error('no TradingView chart tab found (is TradingView open with CDP on :9222?)');
  return { CDP, pages };
}

// Runs inside a "Day Trading Set Up" tab, using the page's own signed-in session. The
// alert's script id, version and settings are read from the indicators on the chart, so
// an alert always matches what the chart is running.
function alertSyncExpression(params) {
  return `(async function(){
    var P = ${JSON.stringify(params)};
    var api = window.TradingViewApi;
    if (!api || typeof api.layoutName !== 'function' || api.layoutName() !== P.layout) return { skip: true };
    var chart = api.activeChart();
    function series(name) {
      var s = chart.getAllStudies().filter(function(x){ return x.name === name; })[0];
      if (!s) throw new Error(name + ' is not on the ' + P.layout + ' chart');
      var st = chart.getStudyById(s.id); var src = st._study || st;
      var inputs = {}, pineId = null, pineVersion = null;
      st.getInputValues().forEach(function(i){
        if (i.id === 'pineId') pineId = i.value; else if (i.id === 'pineVersion') pineVersion = i.value;
        else if (i.id !== 'text') inputs[i.id] = i.value;
      });
      var offsets = {};
      (src.metaInfo().plots || []).forEach(function(p){ if (p.type !== 'alertcondition') offsets[p.id] = 0; });
      return { type: 'study', study: 'Script@tv-scripting-101', offsets_by_plot: offsets, inputs: inputs, pine_id: pineId, pine_version: pineVersion };
    }
    function same(want, got) {
      if (!got || got.pine_id !== want.pine_id || got.pine_version !== want.pine_version) return false;
      var gi = got.inputs || {};
      return Object.keys(want.inputs).every(function(k){ return JSON.stringify(want.inputs[k]) === JSON.stringify(gi[k]); });
    }
    function call(path, payload) {
      var url = 'https://pricealerts.tradingview.com/' + path + '?log_username=' + encodeURIComponent(window.user.username) + (path === 'create_alert' ? '&maintenance_unset_reason=initial_operated' : '');
      return fetch(url, { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'text/plain;charset=UTF-8' }, body: JSON.stringify({ payload: payload }) }).then(function(r){ return r.json(); });
    }
    function body(symbol, name, ser) {
      return { conditions: [{ type: 'pine_alert', series: [ser], resolution: '5' }],
        symbol: '=' + JSON.stringify({ adjustment: 'splits', 'currency-id': 'USD', session: 'regular', symbol: symbol }),
        resolution: '5', message: '', sound_file: null, sound_duration: 0, popup: true, auto_deactivate: false,
        email: false, sms_over_email: false, mobile_push: true, web_hook: null, name: name, expiration: null, active: true, ignore_warnings: true };
    }
    function notifiesAsWanted(a) { return !a.sound_file && a.popup === true && a.mobile_push === true && !a.web_hook && !a.email; }
    var entry = series(P.entryStudy), screener = P.withScreener ? series(P.screenerStudy) : null;
    var listed = await fetch('https://pricealerts.tradingview.com/list_alerts', { credentials: 'include' }).then(function(r){ return r.json(); });
    if (listed.s !== 'ok') throw new Error('could not list alerts: ' + JSON.stringify(listed).slice(0, 200));
    var want = {}; P.tickers.forEach(function(t){ want[t.ticker] = t; });
    var have = {}, drop = [], out = { kept: [], created: [], deleted: [], errors: [], entry_script_version: entry.pine_version };
    (listed.r || []).forEach(function(a){
      if (typeof a.name !== 'string') return;
      var isScreener = a.name === P.screenerName, isEntry = a.name.indexOf(P.entryPrefix) === 0;
      if (!isScreener && !isEntry) return;                      // not ours: never touched
      var key = isScreener ? '#screener' : a.name.slice(P.entryPrefix.length);
      var got = a.condition && a.condition.series && a.condition.series[0];
      var ok = a.active && !have[key] && notifiesAsWanted(a) && (isScreener ? (P.withScreener && same(screener, got)) : (!!want[key] && same(entry, got)));
      if (ok) { have[key] = true; out.kept.push(a.name); } else drop.push(a);
    });
    var make = [];
    P.tickers.forEach(function(t){ if (!have[t.ticker]) make.push({ name: P.entryPrefix + t.ticker, symbols: t.symbols, ser: entry }); });
    if (P.withScreener && !have['#screener']) make.push({ name: P.screenerName, symbols: [P.screenerSymbol], ser: screener });
    if (P.planOnly) { out.would_delete = drop.map(function(a){ return a.name; }); out.would_create = make.map(function(m){ return m.name; }); return out; }
    if (drop.length) {
      var d = await call('delete_alerts', { alert_ids: drop.map(function(a){ return a.alert_id; }) });
      if (d.s === 'ok') out.deleted = drop.map(function(a){ return a.name; }); else out.errors.push({ deleting: drop.map(function(a){ return a.name; }), response: JSON.stringify(d).slice(0, 200) });
    }
    for (var i = 0; i < make.length; i++) {
      var m = make[i], made = null, last = null;
      for (var j = 0; j < m.symbols.length && !made; j++) {
        last = await call('create_alert', body(m.symbols[j], m.name, m.ser));
        if (last.s === 'ok') made = m.symbols[j];
      }
      if (made) out.created.push(made === m.symbols[0] ? m.name : m.name + ' (on ' + made + ', may be delayed data)');
      else out.errors.push({ creating: m.name, response: JSON.stringify(last).slice(0, 200) });
      await new Promise(function(r){ setTimeout(r, 250); });
    }
    return out;
  })()`;
}

async function syncAlerts(tickers, planOnly) {
  const { CDP, pages } = await chartPages();
  const expression = alertSyncExpression({
    layout: LAYOUT, entryStudy: ENTRY_STUDY, screenerStudy: STUDY, entryPrefix: ENTRY_PREFIX, screenerName: SCREENER_ALERT,
    screenerSymbol: SCREENER_ALERT_SYMBOL, withScreener: WITH_SCREENER_ALERT, planOnly, tickers,
  });
  for (const t of pages) {
    let c;
    try {
      c = await CDP({ host: 'localhost', port: 9222, target: t.id });
      const r = await c.Runtime.evaluate({ expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error((r.exceptionDetails.exception && r.exceptionDetails.exception.description || 'alert sync failed').split('\n')[0]);
      const v = r.result && r.result.value;
      if (!v || v.skip) continue;
      return v;                                   // alerts are account-wide: one tab is enough
    } finally {
      if (c) { try { await c.close(); } catch {} }
    }
  }
  throw new Error(`no open TradingView tab has the "${LAYOUT}" layout`);
}

async function writeToTradingView(listText) {
  const { CDP, pages } = await chartPages();
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
  const mag7Added = MAG7.filter(t => !mom.list.includes(t));
  const fixed = [...mom.list, ...mag7Added];
  const nDaily = Math.max(0, TOTAL - fixed.length);
  const { picks, pool_size } = await megaCaps(new Set(fixed), nDaily);
  if (picks.length < nDaily) throw new Error(`only ${picks.length} of ${nDaily} mega caps available from the feed`);
  // The daily names carry their exchange so one-letter tickers (V, C) resolve to the right stock.
  const tickers = [...fixed, ...picks.map(p => p.symbol)];
  const listText = tickers.join(', ');
  const out = {
    as_of_new_york: new Date().toLocaleString('sv-SE', { timeZone: 'America/New_York' }),
    momentum: { tickers: mom.list, source: mom.source },
    mag7_added: mag7Added,
    mega_caps: picks.map(p => ({ ticker: p.name, premarket_pct: p.pm_change == null ? null : +p.pm_change.toFixed(2), premarket_volume: p.pm_volume, last_session_pct: p.change == null ? null : +p.change.toFixed(2), market_cap_bn: p.cap_bn, ranked_by: p.ranked_by })),
    mega_cap_pool: pool_size,
    list: listText,
    applied: false,
  };
  if (apply) { out.tradingview = await writeToTradingView(listText); out.applied = true; }
  if (alerts) {
    // Fixed names use the chart's feed; a daily name falls back to its listing exchange
    // if the feed doesn't carry it.
    const alertTickers = [
      ...fixed.map(t => ({ ticker: t, symbols: [`${FEED}:${t}`] })),
      ...picks.map(p => ({ ticker: p.name, symbols: [`${FEED}:${p.name}`, p.symbol] })),
    ];
    out.alerts = await syncAlerts(alertTickers, alertsPlan || !apply);
    if (out.alerts.errors.length) { clearTimeout(timer); done(1, { error: 'some alerts could not be synced', ...out }); }
  }
  clearTimeout(timer);
  done(0, out);
} catch (e) {
  done(1, { error: String(e && e.message || e) });
}
