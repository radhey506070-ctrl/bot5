// Cloudflare Worker (free): Upstox live price (LTP) proxy - token yahin chhupa rehta hai, page mein nahi.
// Variables/Secrets (Worker -> Settings -> Variables and Secrets):
//   UPSTOX_TOKEN  (Secret)  = aapka Upstox Analytics Token
//   ALLOW_ORIGIN  (Text)    = https://<aapka-username>.github.io
export default {
  async fetch(req, env) {
    const origin = req.headers.get("Origin") || "";
    if (env.ALLOW_ORIGIN && origin !== env.ALLOW_ORIGIN) return new Response("forbidden", { status: 403 });
    const cors = { "Access-Control-Allow-Origin": origin || "*", "Access-Control-Allow-Methods": "GET,OPTIONS", "Content-Type": "application/json" };
    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    const keys = (new URL(req.url).searchParams.get("k") || "").split(",").filter(k => k.startsWith("NSE_EQ|")).slice(0, 150);
    if (!keys.length) return new Response("{}", { headers: cors });
    const api = "https://api.upstox.com/v3/market-quote/ltp?instrument_key=" + keys.map(encodeURIComponent).join(",");
    const r = await fetch(api, { headers: { Authorization: "Bearer " + env.UPSTOX_TOKEN, Accept: "application/json" }, cf: { cacheTtl: 5, cacheEverything: true } });
    if (!r.ok) return new Response("{}", { status: 502, headers: cors });
    const j = await r.json(), out = {};
    for (const [k, v] of Object.entries(j.data || {})) if (v && v.last_price != null) out[v.instrument_token || k] = v.last_price;
    return new Response(JSON.stringify(out), { headers: { ...cors, "Cache-Control": "public, max-age=5" } });
  }
};
