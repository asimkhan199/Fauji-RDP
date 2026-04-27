// Common helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function api(path, opts = {}) {
  const o = { credentials: "same-origin", headers: { "Accept": "application/json" }, ...opts };
  if (o.body && typeof o.body === "object" && !(o.body instanceof FormData)) {
    o.headers["Content-Type"] = "application/json";
    o.body = JSON.stringify(o.body);
  }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch { data = { ok: r.ok }; }
  if (!r.ok && data && data.error) throw new Error(data.error);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return data;
}

function fmtMoney(v, currency = "USD") {
  if (v == null || isNaN(v)) return "—";
  try { return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 2 }).format(v); }
  catch { return Number(v).toFixed(2); }
}

function fmtNum(v, digits = 2) {
  if (v == null || isNaN(v)) return "—";
  return Number(v).toFixed(digits);
}

function fmtUptime(s) {
  s = Math.floor(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${h}h ${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
}

// Dashboard live updater (uses WebSocket)
function startLiveStream(onPayload) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/live`;
  let ws = null, retry = 0;
  const connect = () => {
    ws = new WebSocket(url);
    ws.onmessage = (ev) => {
      try { onPayload(JSON.parse(ev.data)); } catch (e) { console.warn(e); }
      retry = 0;
    };
    ws.onclose = () => {
      retry = Math.min(retry + 1, 6);
      setTimeout(connect, 500 * Math.pow(2, retry));
    };
    ws.onerror = () => { try { ws.close(); } catch {} };
  };
  connect();
}
