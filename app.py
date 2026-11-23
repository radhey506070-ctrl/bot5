"""
F&O Zone Scanner (Upstox API)  -  Demand / Supply zones, Daily..Yearly
Run:  pip install flask requests pandas
      set UPSTOX_TOKEN=your_access_token      (Linux/Mac: export UPSTOX_TOKEN=...)
      python app.py     ->  open http://localhost:5000  (phone se: http://<PC-IP>:5000)
"""
import os, json, gzip, time, threading, datetime as dt
import requests, pandas as pd
from flask import Flask, jsonify, request, Response

TOKEN = os.environ.get("UPSTOX_TOKEN", "")
HDR = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
P = dict(base_max=50, leg_min=50, lookback=100, bases=[1, 2, 3, 4, 5])  # Pine script wale hi defaults
TFS = ["Daily", "Weekly", "Monthly", "Quarterly", "Half Yearly", "Yearly"]
zones = {tf: {} for tf in TFS}
state = {"msg": "Starting...", "done": 0, "total": 0}


def fno_stocks():
    """Upstox instrument file se saare F&O stock (index chhodkar)."""
    url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
    items = json.loads(gzip.decompress(requests.get(url, timeout=120).content))
    out = {}
    for i in items:
        if i.get("segment") == "NSE_FO" and i.get("underlying_type") == "EQUITY" and i.get("underlying_key"):
            out[i["underlying_symbol"]] = i["underlying_key"]
    return dict(sorted(out.items()))


def candles(key, unit, years):
    to = dt.date.today()
    frm = to - dt.timedelta(days=int(365 * years))
    k = requests.utils.quote(key, safe="")
    url = f"https://api.upstox.com/v3/historical-candle/{k}/{unit}/1/{to}/{frm}"
    for _ in range(3):
        r = requests.get(url, headers=HDR, timeout=30)
        if r.status_code == 429:
            time.sleep(2)
            continue
        break
    rows = r.json().get("data", {}).get("candles", []) if r.ok else []
    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v", "oi"])
    if df.empty:
        return df
    df.index = pd.to_datetime(df.t, utc=True).dt.tz_convert("Asia/Kolkata")
    return df.sort_index()[["o", "h", "l", "c"]]


def resample(m, months):
    g = (m.index.year * 12 + m.index.month - 1) // months
    return m.groupby(g).agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"))


def scan(df):
    """Pine logic ka port: Demand = DBR/RBR, Supply = RBD/DBD."""
    O, H, L, C = [df[x].values[::-1] for x in "ohlc"]  # index 0 = latest candle
    N = len(C)
    bp = lambda i: abs(C[i] - O[i]) / (H[i] - L[i]) * 100 if H[i] > L[i] else 0
    res = {}
    for k in range(1, min(P["lookback"], N - 7) + 1):
        for n in P["bases"]:
            li = k + n + 1
            if li >= N or bp(li) < P["leg_min"] or bp(k) < P["leg_min"]:
                continue
            base = range(k + 1, k + n + 1)
            if any(bp(i) >= P["base_max"] for i in base):
                continue
            gh, gl = max(H[i] for i in base), min(L[i] for i in base)
            bt = max(max(O[i], C[i]) for i in base)
            bb = min(min(O[i], C[i]) for i in base)
            if C[k] > O[k] and "demand" not in res:
                top, bot = bt, gl
                if not any(C[j] < bot for j in range(k)):
                    res["demand"] = (top, bot, any(H[j] >= bot and L[j] <= top for j in range(k)))
            if C[k] < O[k] and "supply" not in res:
                top, bot = gh, bb
                if not any(C[j] > top for j in range(k)):
                    res["supply"] = (top, bot, any(H[j] >= bot and L[j] <= top for j in range(k)))
        if len(res) == 2:
            break
    return res, (C[0], H[0], L[0])


def load():
    try:
        if not TOKEN:
            state["msg"] = "UPSTOX_TOKEN set nahi hai"
            return
        stocks = fno_stocks()
        state.update(total=len(stocks), msg="Scanning...")
        for sym, key in stocks.items():
            try:
                d, w, m = candles(key, "days", 2), candles(key, "weeks", 5), candles(key, "months", 15)
                fr = {"Daily": d, "Weekly": w, "Monthly": m}
                if len(m) > 10:
                    fr.update({"Quarterly": resample(m, 3), "Half Yearly": resample(m, 6), "Yearly": resample(m, 12)})
                for tf, df in fr.items():
                    if len(df) > 8:
                        z, px = scan(df)
                        zones[tf][sym] = {"z": z, "px": px}
            except Exception as e:
                print(sym, e)
            state["done"] += 1
            time.sleep(0.3)  # rate limit se bachne ke liye
        state["msg"] = "Live"
    except Exception as e:
        state["msg"] = f"Error: {e}"


app = Flask(__name__)


@app.route("/api/scan")
def api_scan():
    tf = request.args.get("tf", "Weekly")
    crit = request.args.get("crit", "zone")  # zone | approach
    zt = request.args.get("zone", "supply")  # demand | supply
    near = float(request.args.get("near", 1))
    out = []
    for sym, v in zones.get(tf, {}).items():
        z = v["z"].get(zt)
        if not z:
            continue
        top, bot, tested = z
        c, h, l = v["px"]
        touch = h >= bot and l <= top
        dist = (c - top) / c * 100 if c > top else (bot - c) / c * 100 if c < bot else 0
        right_side = c > top if zt == "demand" else c < bot
        if (crit == "zone" and touch) or (crit == "approach" and not touch and right_side and dist <= near):
            out.append(dict(sym=sym, top=round(top, 2), bot=round(bot, 2), dist=round(dist, 2),
                            touch=touch, tested=tested, ltp=round(c, 2)))
    out.sort(key=lambda x: x["dist"])
    return jsonify(items=out, **state)


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Zone Scanner</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#0b1520;color:#e8e8e8;font-family:system-ui,sans-serif;text-align:center}
h1{margin:24px 0 4px;color:#cfcfcf}.sub{color:#9aa5b1;font-size:14px}
.row{display:flex;gap:12px;justify-content:center;padding:16px 12px 0;flex-wrap:wrap}
.seg{display:flex;border:1px solid #8a94a0;border-radius:12px;padding:4px;gap:4px}
.seg button{background:none;border:0;color:#cbd2d9;padding:10px 16px;border-radius:9px;font-size:14px;cursor:pointer}
.seg button.on{background:#c9a227;color:#111;font-weight:600}
.tabs{display:flex;gap:18px;overflow-x:auto;padding:22px 16px 10px;border-bottom:1px solid #2a3642;justify-content:center}
.tabs span{color:#aab3bd;cursor:pointer;white-space:nowrap;padding-bottom:8px}
.tabs span.on{color:#c9a227;border-bottom:2px solid #c9a227}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;padding:16px;max-width:900px;margin:auto}
.tile{display:block;text-decoration:none;color:#fff;background:linear-gradient(145deg,#142435,#0d1824);
border:1px solid #223244;border-radius:6px;padding:14px 8px;min-height:92px}
.tile b{display:block;font-size:15px;letter-spacing:.5px}.tile small{display:block;color:#9aa5b1;margin-top:5px;font-size:12px}
.in{color:#4ade80}.bad{color:#f59e0b}.demand b{color:#6ee7a0}.supply b{color:#ff8a8a}
</style></head><body>
<h1>Zone Scanner</h1><div class="sub" id="st">Loading...</div>
<div class="row">
 <div class="seg" id="crit"><button data-v="approach">Approaching</button><button data-v="zone" class="on">In Zone / Reacting</button></div>
 <div class="seg" id="zone"><button data-v="demand">Demand</button><button data-v="supply" class="on">Supply</button></div>
</div>
<div class="tabs" id="tfs"></div><div class="grid" id="g"></div>
<script>
const TFS=["Daily","Weekly","Monthly","Quarterly","Half Yearly","Yearly"];
let s={tf:"Weekly",crit:"zone",zone:"supply"};
const $=id=>document.getElementById(id);
$("tfs").innerHTML=TFS.map(t=>`<span data-v="${t}" class="${t==s.tf?'on':''}">${t}</span>`).join("");
function bind(id,key){$(id).onclick=e=>{const v=e.target.dataset.v;if(!v)return;s[key]=v;
 [...$(id).children].forEach(c=>c.classList.toggle("on",c.dataset.v==v));load()}}
bind("crit","crit");bind("zone","zone");bind("tfs","tf");
async function load(){
 const r=await (await fetch(`/api/scan?tf=${encodeURIComponent(s.tf)}&crit=${s.crit}&zone=${s.zone}`)).json();
 $("st").textContent=`${r.msg} - ${r.done}/${r.total} stocks scanned | ${r.items.length} match`;
 $("g").innerHTML=r.items.map(i=>`<a class="tile ${s.zone}" target="_blank" href="https://in.tradingview.com/chart/?symbol=NSE:${i.sym}">
 <b>${i.sym}</b><small>${i.bot} - ${i.top}</small>
 <small class="${i.touch?'in':''}">${i.touch?'IN ZONE':i.dist+'% away'}</small>
 <small class="${i.tested?'bad':''}">${i.tested?'Tested':'Fresh'} | LTP ${i.ltp}</small></a>`).join("")||'<div class="sub">Koi stock match nahi hua</div>';
}
load();setInterval(load,15000);
</script></body></html>"""

if __name__ == "__main__":
    threading.Thread(target=load, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
