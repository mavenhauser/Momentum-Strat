// Save day_trading_setup/pine/dts_3_entry_signals.pine as the "DTS Entry" script in
// TradingView Desktop (remote debugging on port 9222), safely:
//   1. pick the chart tab that shows DTS Entry, open its Pine editor
//   2. switch the editor to DTS Entry through its own script menu (pine_new / pine_open
//      in the MCP tool do NOT switch scripts and have overwritten other scripts before)
//   3. refuse unless the editor title is "DTS Entry" and it holds an indicator("DTS Entry")
//   4. paste, save, and confirm the saved server copy is identical to the repo file
// Usage: node day_trading_setup/scripts/save_dts_entry.mjs
import { createRequire } from 'module';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

const require = createRequire(process.env.HOME + '/tradingview-mcp/package.json');
const CDP = require('chrome-remote-interface');
const here = path.dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(path.join(here, '../pine/dts_3_entry_signals.pine'), 'utf8');
const WANT = 'DTS Entry', ID = 'USER;d5fe9095372540c8961b76b92460fe9b';
const mcp = readFileSync(process.env.HOME + '/tradingview-mcp/src/core/pine.js', 'utf8');
const FIND = mcp.match(/const FIND_MONACO = `([\s\S]*?)`;/)[1].trim();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function ev(target, expression) {
  const c = await CDP({ port: 9222, target });
  try {
    const r = await c.Runtime.evaluate({ expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  } finally { await c.close(); }
}

async function pickTab() {
  const tabs = (await CDP.List({ port: 9222 })).filter((t) => t.type === 'page' && t.url.includes('tradingview.com/chart/'));
  // Prefer a tab whose Pine editor is already open on DTS Entry (the editor may not open in a
  // tab TradingView isn't showing).
  for (const t of tabs) {
    const open = await ev(t.id, `[].slice.call(document.querySelectorAll('h2')).map(function(e){return (e.textContent||'').trim()}).join('|') === ${JSON.stringify(WANT)}`).catch(() => false);
    if (open) return t;
  }
  for (const t of tabs) {
    const has = await ev(t.id, `document.body.innerText.indexOf('DTS Entry') !== -1`).catch(() => false);
    if (has) return t;
  }
  throw new Error('no TradingView chart tab shows DTS Entry');
}

const titles = `[].slice.call(document.querySelectorAll('h2')).map(function(e){return (e.textContent||'').trim()})`;
const t = await pickTab();
console.log('tab', t.id.slice(0, 8), t.url);
let has = await ev(t.id, `(function(){ return ${FIND} !== null; })()`);
if (!has) {
  await ev(t.id, `document.querySelector('[data-name="pine-dialog-button"]').click()`);
  for (let i = 0; i < 20 && !has; i++) { await sleep(700); has = await ev(t.id, `(function(){ return ${FIND} !== null; })()`); }
}
if (!has) { console.log('ABORT: Pine editor did not open'); process.exit(1); }
let cur = await ev(t.id, titles);
if (cur.length !== 1 || cur[0] !== WANT) {
  const r = await ev(t.id, `new Promise(function(res){ var h=[].slice.call(document.querySelectorAll('h2'))[0]; if(!h) return res('no title'); h.click(); setTimeout(function(){ var it=[].slice.call(document.querySelectorAll('[aria-label="${WANT}"]'))[0]; if(!it) return res('menu item not found'); it.click(); setTimeout(function(){ var b=[].slice.call(document.querySelectorAll('button')).filter(function(e){return (e.innerText||'').trim()==="Don't save"})[0]; if(b){ b.click(); setTimeout(function(){res('switched (discarded unsaved buffer)')},2500); } else res('switched'); }, 2500); }, 900); })`);
  console.log('switch:', r);
  cur = await ev(t.id, titles);
}
const before = await ev(t.id, `(function(){ var v=${FIND}.editor.getValue(); return (v.split('\\n').filter(function(l){return /^(indicator|strategy)\\(/.test(l)})[0]||'').slice(0,60); })()`);
console.log('editor:', JSON.stringify(cur), '|', before);
if (cur.length !== 1 || cur[0] !== WANT || !/^indicator\("DTS Entry"/.test(before)) { console.log('ABORT: editor is not on DTS Entry'); process.exit(1); }
await ev(t.id, `(function(){ ${FIND}.editor.setValue(${JSON.stringify(SRC)}); return true; })()`);
const after = await ev(t.id, `(function(){ return {len:${FIND}.editor.getValue().length, title:${titles}}; })()`);
if (after.title.length !== 1 || after.title[0] !== WANT || after.len < SRC.length - 50) { console.log('ABORT before save', JSON.stringify(after)); process.exit(1); }
const saved = await ev(t.id, `new Promise(function(res){ var btns=[].slice.call(document.querySelectorAll('button')); var b=btns.filter(function(e){return e.className.indexOf('saveButton')!==-1 && e.offsetParent!==null})[0] || btns.filter(function(e){return /^(Update on chart|Save)$/i.test((e.innerText||'').trim()) && e.offsetParent!==null})[0]; if(!b) return res('no save button'); var label=(b.title||b.innerText||b.className).trim().slice(0,40); b.click(); setTimeout(function(){ res(label); }, 5000); })`);
console.log('save:', saved);
const v = await ev(t.id, `fetch('https://pine-facade.tradingview.com/pine-facade/get/${encodeURIComponent(ID)}/last',{credentials:'include'}).then(function(r){return r.json()}).then(function(j){ return {version:j.version, source:j.source}; })`);
const norm = (s) => s.replace(/\r\n/g, '\n').replace(/\s+$/, '');
const same = norm(v.source) === norm(SRC);
console.log('server: version', v.version, '| identical to repo:', same);
process.exit(same ? 0 : 1);
