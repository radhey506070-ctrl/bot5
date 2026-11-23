// GitHub Actions: zone ke paas aaye stocks ka phone alert (ntfy). Settings = config.json
const fs = require("fs"), ZN = require("./docs/zones.js");
const cfg = JSON.parse(fs.readFileSync("config.json", "utf8"));
const S = { ...ZN.DEF, ...cfg, pats: { ...ZN.DEF.pats, ...(cfg.pats || {}) } };
const PCT = cfg.alert_distance_pct, WL = (cfg.watchlist || []).map(x => String(x).toUpperCase()), cur = {};
for (const tf of cfg.alert_timeframes) {
  let cand = {};
  try { cand = JSON.parse(fs.readFileSync(`site/c_${tf.replace(/ /g, "_")}.json`, "utf8")); } catch (e) { continue; }
  for (const [sym, cd] of Object.entries(cand)) {
    if (WL.length && !WL.includes(sym)) continue;   // watchlist di hai to sirf wahi stocks
    const v = ZN.zoneScan(cd, S); if (!v) continue;
    for (const zt of ["demand", "supply"]) {
      const b = ZN.pick(v, zt, "approach", { ...S, near: PCT });
      if (b) cur[`${sym}|${tf}|${zt}|${Math.round(b.top)}`] = `${sym} ${tf} ${zt} zone - ${b.dist.toFixed(2)}% away (LTP ${b.c.toFixed(2)})`;
    }
  }
}
(async () => {
  let old = []; // pichhli baar ke alerts website se padhte hain (repo mein save nahi karte)
  try { const r = await fetch(process.env.SITE_URL + "/alerts.json"); if (r.ok) old = await r.json(); } catch (e) {}
  const fresh = Object.entries(cur).filter(([k]) => !old.includes(k)).map(([, m]) => m);
  const topic = process.env.NTFY_TOPIC;
  if (fresh.length && topic) {
    try { await fetch("https://ntfy.sh/" + topic, { method: "POST", body: fresh.join("\n"), headers: { Title: "Zone Alert", Priority: "high" } }); console.log("Alert sent:", fresh.length); }
    catch (e) { console.log("ntfy error:", e.message); }
  }
  fs.mkdirSync("site", { recursive: true });
  fs.writeFileSync("site/alerts.json", JSON.stringify(Object.keys(cur).sort()));
  console.log("approaching now:", Object.keys(cur).length, "| new:", fresh.length);
})();
