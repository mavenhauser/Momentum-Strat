// Prints the active TradingView layout name as JSON: {"layout": "<name>"}.
// Read-only. Uses the tradingview MCP's own connection module, so it reads the
// SAME tab the MCP will later drive (visible tab preferred, else first chart tab).
// Exit 0 + JSON on success; exit 1 + {"error": "..."} if it can't be read.
import { getClient, disconnect } from '/Users/James Woo/tradingview-mcp/src/connection.js';

const done = (code, obj) => { process.stdout.write(JSON.stringify(obj) + '\n'); process.exit(code); };
const timer = setTimeout(() => done(1, { error: 'timed out reading layout name' }), 20000);

try {
  const c = await getClient();
  const r = await c.Runtime.evaluate({
    expression: `(function(){ var f = window.TradingViewApi && window.TradingViewApi.layoutName; return typeof f === 'function' ? f.call(window.TradingViewApi) : null; })()`,
    returnByValue: true,
  });
  const layout = r.result && r.result.value;
  clearTimeout(timer);
  try { await disconnect(); } catch {}
  if (typeof layout !== 'string') done(1, { error: 'layoutName() unavailable', raw: r.result });
  done(0, { layout });
} catch (e) {
  done(1, { error: String(e && e.message || e) });
}
