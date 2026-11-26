"""LIVE scanner (apne PC par chalayein)  ->  http://localhost:5000
Upstox se intraday candles + live price (LTP, har ~6 sec) leke wahi page live dikhata hai.
Chalane se pehle:  set UPSTOX_TOKEN=aapka_token   (phone alert ke liye:  set NTFY_TOPIC=naam)
Phir:              python live_server.py"""
import json, os, sys, time, shutil, subprocess, threading, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
import numpy as np, requests
from flask import Flask, Response, abort, send_from_directory

os.chdir(os.path.dirname(os.path.abspath(__file__)))
import scan
from scan import frame, hist, intra, rs_min, rs_hr, INTRA_TFS, KEEP

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
N_OF = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240}
CFG = json.load(open("config.json"))
LV = CFG.get("live", {})
LTP_S, GAP = float(LV.get("ltp_seconds", 6)), float(LV.get("candle_gap", 2.0))   # API limit: ~66 calls/min
PAGE_S, CHUNK, MAXN = int(LV.get("page_refresh", 8)), int(LV.get("other_chunk", 20)), int(LV.get("max_stocks", 250))
PORT = int(os.environ.get("PORT", 5000))

stocks, KEY2SYM, FNO, N500, WL = {}, {}, set(), set(), set()
HIST, TODAY, LTP, TS = {}, {}, {}, {}
BASE = {tf: {} for tf in N_OF}          # BASE[tf][sym] = (arrays, live_ok)
status = {"msg": "Starting...", "done": 0, "total": 0, "ready": False, "ltp_at": 0}


def now():
    return dt.datetime.now(IST)


def market_open():
    n = now()
    return n.weekday() < 5 and dt.time(9, 0) <= n.time() <= dt.time(15, 45)


def arrs(df):
    if df is None or len(df) == 0:
        return {k: np.array([]) for k in "ohlcv"}
    return {k: df[k].values.astype(float) for k in "ohlcv"}


def load_hist(sym, key):
    """Pichhle dinon ki candles (aaj ko chhodkar) - din mein ek baar."""
    try:
        today = now().date()
        keep = lambda df: df[df.index.date < today] if len(df) else df
        m5, h1 = keep(frame(hist(key, "minutes", 5, 20))), keep(frame(hist(key, "hours", 1, 85)))
        H = {"5m": m5, "10m": rs_min(m5, 10), "15m": rs_min(m5, 15), "30m": rs_min(m5, 30),
             "1h": h1, "2h": rs_hr(h1, 2), "4h": rs_hr(h1, 4)}
        HIST[sym] = {tf: {k: a[-KEEP:] for k, a in arrs(df).items()} for tf, df in H.items()}
    except Exception as e:
        print("hist error", sym, e)
    status["done"] += 1


def compose(sym):
    """History + aaj ki 1m candles se saare intraday timeframe banao."""
    m1 = TODAY.get(sym)
    has = m1 is not None and len(m1) > 0
    for tf, n in N_OF.items():
        t = arrs(m1 if n == 1 else rs_min(m1, n)) if has else arrs(None)
        h = HIST.get(sym, {}).get(tf)
        a = {k: (np.concatenate([h[k], t[k]]) if h else t[k])[-KEEP:] for k in "ohlcv"}
        if len(a["c"]):
            BASE[tf][sym] = ({k: ([int(x) for x in a[k]] if k == "v" else [round(float(x), 2) for x in a[k]]) for k in "ohlcv"}, has)


def refresh(sym):
    try:
        rows = intra(stocks[sym], "minutes", 1)
        if rows:
            TODAY[sym] = frame(rows)
            compose(sym)
            TS[sym] = time.time()
    except Exception as e:
        print("refresh error", sym, e)


def ltp_once():
    keys = list(stocks.values())
    for i in range(0, len(keys), 150):
        url = "https://api.upstox.com/v3/market-quote/ltp?instrument_key=" + ",".join(quote(k, safe="") for k in keys[i:i + 150])
        try:
            r = requests.get(url, headers=scan.HDR, timeout=20)
            if not r.ok:
                continue
            for k, v in (r.json().get("data") or {}).items():
                sym = KEY2SYM.get(v.get("instrument_token")) or KEY2SYM.get(k)
                if sym and v.get("last_price") is not None:
                    LTP[sym] = float(v["last_price"])
        except Exception as e:
            print("ltp error", e)
    status["ltp_at"] = time.time()


def tf_json(tf):
    """Candles + live price: aakhri candle ka close/high/low LTP se update."""
    out = {}
    for sym, (a, ok) in list(BASE.get(tf, {}).items()):
        p = LTP.get(sym)
        if ok and p and a["c"]:
            out[sym] = {"o": a["o"], "h": a["h"][:-1] + [max(a["h"][-1], round(p, 2))], "l": a["l"][:-1] + [min(a["l"][-1], round(p, 2))],
                        "c": a["c"][:-1] + [round(p, 2)], "v": a["v"]}
        else:
            out[sym] = a
    return out


def boot():
    try:
        status["msg"] = "Instruments load ho rahe hain..."
        fno, eq = scan.load_instruments()
        universe = CFG.get("universe", "fno")
        n500 = scan.nifty500_symbols() if universe != "fno" else []
        st, f, n = scan.build_universe(fno, eq, n500, universe)
        WL.update(str(x).upper() for x in CFG.get("watchlist", []))
        for s in WL:
            if s in eq and s not in st:
                st[s] = eq[s]
        order = [s for s in st if s in WL] + [s for s in st if s not in WL and s in f] + [s for s in st if s not in WL and s not in f]
        if len(order) > MAXN:
            print(f"WARNING: {len(order)} stocks mein se pehle {MAXN} (Upstox limit). config.json -> live.max_stocks")
            order = order[:MAXN]
        stocks.update({s: st[s] for s in order})
        FNO.update(set(f) & set(stocks)); N500.update(set(n) & set(stocks))
        KEY2SYM.update({k: s for s, k in stocks.items()})
        status["total"] = len(stocks)
        status["msg"] = "History load ho rahi hai..."
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(lambda it: load_hist(*it), list(stocks.items())))
        status["msg"] = "Aaj ka data load ho raha hai..."
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(refresh, list(stocks)))
        ltp_once()
        status.update(msg="LIVE", ready=True)
        for fn in (ltp_loop, candle_loop, alert_loop):
            threading.Thread(target=fn, daemon=True).start()
    except Exception as e:
        status["msg"] = f"Error: {e}"
        print("BOOT ERROR:", e)


def ltp_loop():
    while True:
        time.sleep(LTP_S if market_open() else 60)
        ltp_once()


def candle_loop():
    wl = [s for s in stocks if s in WL]
    others = [s for s in stocks if s not in WL]
    ci, day = 0, now().date()
    while True:
        if not market_open():
            time.sleep(30)
            continue
        if now().date() != day:     # naya din: history dobara, aaj ka data saaf
            day = now().date(); TODAY.clear(); status["done"] = 0
            with ThreadPoolExecutor(6) as ex:
                list(ex.map(lambda it: load_hist(*it), list(stocks.items())))
        batch = list(wl)
        for _ in range(min(CHUNK, len(others))):
            batch.append(others[ci % len(others)]); ci += 1
        for sym in batch:
            refresh(sym)
            time.sleep(GAP)
        if not batch:
            time.sleep(5)


def alert_loop():
    """Phone alert: Node.js ho to har minute alert.js chalta hai (browser band ho tab bhi)."""
    if not shutil.which("node"):
        print("Node.js nahi mila: phone alert band (browser popup/beep chalega)")
        return
    while True:
        time.sleep(60)
        if not market_open():
            continue
        try:
            os.makedirs("site", exist_ok=True)
            for tf in CFG.get("alert_timeframes", []):
                if tf in N_OF:
                    json.dump(tf_json(tf), open(f"site/c_{tf.replace(' ', '_')}.json", "w"), separators=(",", ":"))
            subprocess.run(["node", "alert.js"], env=dict(os.environ, SITE_URL=f"http://127.0.0.1:{PORT}"), timeout=120)
        except Exception as e:
            print("alert error", e)


app = Flask(__name__)


def js(o):
    return Response(json.dumps(o, separators=(",", ":")), mimetype="application/json", headers={"Cache-Control": "no-store"})


@app.route("/data.json")
def data_json():
    upd = now().strftime("%d %b %Y, %I:%M:%S %p IST") if status["ready"] else status["msg"] + f" ({status['done']}/{status['total']})"
    return js({"updated": upd, "total": len(stocks), "trend": {}, "alert_tfs": CFG.get("alert_timeframes", ["15m"]),
               "alert_pct": CFG.get("alert_distance_pct", 1), "watchlist": sorted(WL), "watchlist_only": bool(CFG.get("watchlist_only", False)),
               "mode": "intraday", "tfs": INTRA_TFS, "symbols": sorted(stocks), "fno": sorted(FNO), "n500": sorted(N500),
               "live": True, "refresh": PAGE_S, "ts": {s: int(t) for s, t in TS.items()}})


@app.route("/c_<name>.json")
def candles_json(name):
    tf = name.replace("_", " ")
    if tf not in N_OF:
        abort(404)
    return js(tf_json(tf))


@app.route("/alerts.json")
def alerts_json():
    try:
        return js(json.load(open("site/alerts.json")))
    except Exception:
        return js([])


@app.route("/")
def index():
    return send_from_directory("docs", "index.html", max_age=0)


@app.route("/<path:p>")
def static_docs(p):
    return send_from_directory("docs", p, max_age=0)


if __name__ == "__main__":
    if not scan.TOKEN:
        sys.exit("UPSTOX_TOKEN set nahi hai:  set UPSTOX_TOKEN=aapka_token")
    threading.Thread(target=boot, daemon=True).start()
    print(f"\nLIVE scanner: http://localhost:{PORT}   (phone: http://<PC-ka-IP>:{PORT})\n")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
