// Finds the TradingView tab whose layout name is EXACTLY the one given (argv[2]), whether or not
// it's the tab in front. Read-only. Prints JSON:
//   {"target_id": "<CDP id or null>", "layout": "<name or null>", "tabs": [{"id","layout","visible"}...]}
// Exit 0 whenever tabs could be enumerated (target_id null = no tab has that layout);
// exit 1 + {"error": ...} if TradingView/CDP can't be reached. If several tabs share the layout,
// the visible one wins, else the first listed.
import { createRequire } from 'module';
const require = createRequire('/Users/James Woo/tradingview-mcp/package.json');
const CDP = require('chrome-remote-interface');

const want = process.argv[2];
const done = (code, obj) => { process.stdout.write(JSON.stringify(obj) + '\n'); process.exit(code); };
const timer = setTimeout(() => done(1, { error: 'timed out reading layouts' }), 20000);

try {
  if (!want) done(1, { error: 'usage: tv_layout_check.mjs <layout name>' });
  const targets = await (await fetch('http://localhost:9222/json/list')).json();
  const pages = targets.filter(t => t.type === 'page' && /tradingview\.com\/chart/i.test(t.url));
  if (pages.length === 0) done(1, { error: 'no TradingView chart tab found (is TradingView open with CDP on :9222?)' });
  const tabs = [];
  for (const t of pages) {
    let c;
    try {
      c = await CDP({ host: 'localhost', port: 9222, target: t.id });
      await c.Runtime.enable();
      const r = await c.Runtime.evaluate({
        returnByValue: true,
        expression: `({layout: (function(){ var a = window.TradingViewApi; return a && typeof a.layoutName === 'function' ? a.layoutName() : null; })(), visible: document.visibilityState === 'visible'})`,
      });
      const v = r.result && r.result.value;
      tabs.push({ id: t.id, layout: v ? v.layout : null, visible: !!(v && v.visible) });
    } catch (e) {
      tabs.push({ id: t.id, layout: null, visible: false, error: String(e && e.message || e) });
    } finally {
      if (c) { try { await c.close(); } catch {} }
    }
  }
  clearTimeout(timer);
  const hits = tabs.filter(t => t.layout === want);
  const pick = hits.find(t => t.visible) || hits[0] || null;
  done(0, { target_id: pick ? pick.id : null, layout: pick ? pick.layout : null, tabs });
} catch (e) {
  done(1, { error: String(e && e.message || e) });
}
