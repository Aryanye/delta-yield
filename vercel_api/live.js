// Live quote relay for the published dashboard.
//
// The full sweep (24k contracts, 8.6k exact margins) is bounded by Kite's rate
// limits at ~155s, so it runs every 5 minutes. But the ~1,300 contracts the
// heatmap actually SHOWS can be re-quoted in a few calls. This endpoint does
// that; the browser recomputes IV -> delta -> yield against the cached per-lot
// margin. Same idea as a live-positions panel: cheap quotes on a short clock,
// expensive structure on a long one.
//
// Only the daily access token lives here, as a Vercel env var. It expires
// every morning and can only read quotes/place orders for one account, so
// exposure is one trading day; the api_secret never leaves the operator's Mac.

const KITE = "https://api.kite.trade/quote";
const MAX_INSTRUMENTS = 2000;       // 4 Kite calls; keeps well inside 10s
const BATCH = 500;                  // Kite's per-call ceiling
const TTL_MS = 10000;               // share one Kite fetch across viewers

// Module-level cache survives on a warm instance, so ten viewers polling the
// same view cost Kite one fetch per TTL rather than ten.
const cache = new Map();

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function fetchBatch(instruments, apiKey, token) {
  const url = KITE + "?" + instruments.map(i => "i=" + encodeURIComponent(i)).join("&");
  const res = await fetch(url, {
    headers: {
      "X-Kite-Version": "3",
      "Authorization": `token ${apiKey}:${token}`,
      "Accept": "application/json",
    },
  });
  if (res.status === 403) throw Object.assign(new Error("auth"), { code: "auth" });
  if (res.status === 429) throw Object.assign(new Error("ratelimit"), { code: "ratelimit" });
  if (!res.ok) throw Object.assign(new Error("http " + res.status), { code: "http" });
  const body = await res.json();
  if (body.status !== "success") {
    const code = /token/i.test(body.message || "") ? "auth" : "kite";
    throw Object.assign(new Error(body.message || "kite error"), { code });
  }
  return body.data || {};
}

function slim(q) {
  const d = q.depth || {};
  const bid = d.buy && d.buy[0] ? +d.buy[0].price : 0;
  const ask = d.sell && d.sell[0] ? +d.sell[0].price : 0;
  const o = q.ohlc || {};
  return { l: +q.last_price || 0, b: bid, a: ask, oi: +q.oi || 0,
           c: +o.close || 0, v: +q.volume || 0 };
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Content-Type", "application/json");
  if (req.method !== "POST") { res.status(405).end('{"error":"POST only"}'); return; }

  const apiKey = process.env.KITE_API_KEY;
  const token = process.env.KITE_ACCESS_TOKEN;
  if (!apiKey || !token) {
    res.status(200).end(JSON.stringify({ error: "auth", detail: "no token configured" }));
    return;
  }

  let body = req.body;
  if (typeof body === "string") { try { body = JSON.parse(body); } catch { body = {}; } }
  let list = Array.isArray(body && body.i) ? body.i : [];
  list = [...new Set(list.filter(s => typeof s === "string" && /^(NSE|NFO):[A-Z0-9&-]+$/.test(s)))];
  if (!list.length) { res.status(200).end('{"error":"empty"}'); return; }
  if (list.length > MAX_INSTRUMENTS) list = list.slice(0, MAX_INSTRUMENTS);

  const key = list.slice().sort().join(",");
  const hit = cache.get(key);
  const now = Date.now();
  if (hit && now - hit.at < TTL_MS) {
    res.status(200).end(JSON.stringify({ ...hit.payload, cached: true }));
    return;
  }

  const quotes = {};
  try {
    for (let i = 0; i < list.length; i += BATCH) {
      if (i) await sleep(1050);                 // Kite: 1 quote call per second
      const data = await fetchBatch(list.slice(i, i + BATCH), apiKey, token);
      for (const [k, v] of Object.entries(data)) quotes[k] = slim(v);
    }
  } catch (e) {
    // Serve the last good payload if we have one, marked stale, so a single
    // rate-limit collision with the 5-minute collector does not blank the page.
    if (hit) {
      res.status(200).end(JSON.stringify({ ...hit.payload, stale: true, error: e.code || "kite" }));
    } else {
      res.status(200).end(JSON.stringify({ error: e.code || "kite", detail: String(e.message).slice(0, 120) }));
    }
    return;
  }

  const payload = { ts: new Date().toISOString(), n: Object.keys(quotes).length, q: quotes };
  cache.set(key, { at: now, payload });
  if (cache.size > 32) cache.delete(cache.keys().next().value);
  res.status(200).end(JSON.stringify(payload));
};
