// Print TradingView's alert fire log (the last 200 fires) as JSON, for backfill.py.
//
// Reads it from the TradingView desktop app over its debugging port (9222), inside
// the logged-in chart page, the same way the screener list builder does. Each row:
//   {fire_id, fire_time (ISO UTC), name, message, webhook_ok, webhook_error}
// Exit code 2 = TradingView not reachable (not open, or opened without the port).
//
//   node day_trading_setup/executor/tv_fires.mjs
import { createRequire } from 'module';
import { homedir } from 'os';

const require = createRequire(`${homedir()}/tradingview-mcp/package.json`);
const CDP = require('chrome-remote-interface');

let pages;
try {
  const targets = await (await fetch('http://localhost:9222/json/list')).json();
  pages = targets.filter((t) => t.type === 'page' && t.url.includes('tradingview.com/chart/'));
} catch (e) {
  console.error(`TradingView not reachable on port 9222: ${e.message}`);
  process.exit(2);
}
if (!pages.length) {
  console.error('no TradingView chart tab open');
  process.exit(2);
}

const client = await CDP({ port: 9222, target: pages[0].id });
const expr = `(async function () {
  var r = await fetch('https://pricealerts.tradingview.com/list_fires?log_username=' + encodeURIComponent(window.user.username), {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'text/plain;charset=UTF-8'},
    body: JSON.stringify({payload: {limit: 200}})
  }).then(function (r) { return r.json(); });
  if (r.s !== 'ok') return {error: JSON.stringify(r).slice(0, 200)};
  return {fires: (r.r || []).map(function (f) {
    var w = f.webhook || {};
    return {fire_id: f.fire_id, fire_time: f.fire_time, name: f.name, message: f.message,
            webhook_ok: w.http_code === 200, webhook_error: w.error || (w.http_code ? 'HTTP ' + w.http_code : null)};
  })};
})()`;
try {
  const res = await client.Runtime.evaluate({ expression: expr, awaitPromise: true, returnByValue: true });
  const v = res.result && res.result.value;
  if (!v || v.error) {
    console.error(`fire log unavailable: ${v ? v.error : JSON.stringify(res.exceptionDetails || {}).slice(0, 200)}`);
    process.exit(3);
  }
  console.log(JSON.stringify(v.fires));
} finally {
  await client.close();
}
