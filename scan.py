"""GitHub Actions: saare F&O stocks ke candles export (1m se Yearly tak) -> site/c_<timeframe>.json + site/data.json
Zones website / alert.js khud nikalte hain (docs/zones.js). Upstox seconds wali candles nahi deta, isliye 1m sabse chhota hai."""
import csv, gzip, io, json, os, sys, time, threading, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
import requests, pandas as pd
from app import TOKEN

HDR = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
BASE = "https://api.upstox.com/v3/historical-candle"
OUT = "site"
KEEP = 180  # har timeframe ke aakhri 180 candles
INSTR_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
NIFTY500_URLS = [  # pehla jo chale; sab fail ho to repo ki nifty500.csv use hoti hai
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
]
ALL_TFS = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "4h", "Daily", "Weekly", "Monthly", "Quarterly", "Half Yearly", "Yearly"]
_lock, _last = threading.Lock(), [0.0]


def load_instruments():
    """Upstox instrument file se: F&O stocks aur saare NSE equity {symbol: instrument_key}."""
    items = json.loads(gzip.decompress(requests.get(INSTR_URL, timeout=180).content))
    fno, eq = {}, {}
    for i in items:
        seg = i.get("segment")
        if seg == "NSE_FO" and i.get("underlying_type") == "EQUITY" and i.get("underlying_key"):
            fno[i["underlying_symbol"]] = i["underlying_key"]
        elif seg == "NSE_EQ" and i.get("instrument_type") == "EQ" and i.get("trading_symbol"):
            eq[i["trading_symbol"]] = i["instrument_key"]
    return fno, eq


def parse_symbols(text):
    """NSE csv (Symbol column) ya simple list (ek line mein ek symbol)."""
    text = text.lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    if "symbol" in head:
        i = head.index("symbol")
        return [r[i].strip().upper() for r in rows[1:] if len(r) > i and r[i].strip()]
    return [r[0].strip().upper() for r in rows if r and r[0].strip()]


def nifty500_symbols():
    for url in NIFTY500_URLS:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            syms = parse_symbols(r.text) if r.ok else []
            if len(syms) > 300:
                print("Nifty 500 list mili:", len(syms), "|", url)
                return syms
        except Exception as e:
            print("Nifty 500 fetch error:", url, e)
    if os.path.exists("nifty500.csv"):
        syms = parse_symbols(open("nifty500.csv", encoding="utf-8").read())
        print("nifty500.csv se list li:", len(syms))
        return syms
    print("WARNING: Nifty 500 list nahi mili - repo mein nifty500.csv daalein. Abhi sirf F&O scan hoga.")
    return []


def build_universe(fno, eq, n500, universe):
    """-> (stocks {sym: key}, fno_set, n500_set)"""
    n500_map = {s: eq[s] for s in n500 if s in eq}
    miss = [s for s in n500 if s not in eq]
    if miss:
        print("Upstox mein nahi mile:", len(miss), miss[:15])
    stocks = {}
    if universe in ("fno", "both"):
        stocks.update(fno)
    if universe in ("nifty500", "both"):
        for sym, key in n500_map.items():
            stocks.setdefault(sym, key)
    return dict(sorted(stocks.items())), {s for s in fno if s in stocks}, {s for s in n500_map if s in stocks}


def get(url):
    """Rate limit (~6 req/sec) ke saath API call, 429 par retry."""
    for i in range(4):
        with _lock:
            d = _last[0] + 0.16 - time.time()
            if d > 0:
                time.sleep(d)
            _last[0] = time.time()
        try:
            r = requests.get(url, headers=HDR, timeout=30)
        except Exception:
            time.sleep(2)
            continue
        if r.status_code == 429:
            time.sleep(3 * (i + 1))
            continue
        return r.json().get("data", {}).get("candles", []) if r.ok else []
    return []


def frame(rows):
    if not rows:
        return pd.DataFrame(columns=list("ohlcv"))
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["t", "o", "h", "l", "c", "v"]
    df.index = pd.DatetimeIndex(pd.to_datetime(df["t"], utc=True)).tz_convert("Asia/Kolkata")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[["o", "h", "l", "c", "v"]].astype(float)


def hist(key, unit, interval, days):
    to = dt.date.today()
    return get(f"{BASE}/{quote(key, safe='')}/{unit}/{interval}/{to}/{to - dt.timedelta(days=days)}")


def intra(key, unit, interval):  # sirf aaj ka trading din
    return get(f"{BASE}/intraday/{quote(key, safe='')}/{unit}/{interval}")


def _agg(g):
    return g.agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"))


def rs_months(m, months):
    """Monthly -> Quarterly / Half Yearly / Yearly."""
    return _agg(m.groupby((m.index.year * 12 + m.index.month - 1) // months))


def rs_min(df, n):
    """1m/5m candles -> n-minute candles (NSE session 9:15 se aligned)."""
    if df.empty:
        return df
    m = df.index.hour * 60 + df.index.minute - 555
    return _agg(df.groupby([df.index.normalize(), m // n]))


def rs_hr(df, k):
    """1h candles -> k-hour candles (har din ke pehle candle se)."""
    if df.empty:
        return df
    b = df.groupby(df.index.normalize()).cumcount() // k
    return _agg(df.groupby([df.index.normalize(), b.values]))


def trend(df):
    """U = Uptrend (close > EMA20 > EMA50), D = Downtrend (close < EMA20 < EMA50), S = Sideways."""
    c = df["c"].astype(float)
    e20, e50 = c.ewm(span=20, adjust=False).mean().iloc[-1], c.ewm(span=50, adjust=False).mean().iloc[-1]
    return "U" if c.iloc[-1] > e20 > e50 else "D" if c.iloc[-1] < e20 < e50 else "S"


def process(item, intraday):
    sym, key = item
    out = {}
    try:
        d, w, m = frame(hist(key, "days", 1, 730)), frame(hist(key, "weeks", 1, 1825)), frame(hist(key, "months", 1, 5475))
        out.update({"Daily": d, "Weekly": w, "Monthly": m})
        if len(m) > 10:
            out.update({"Quarterly": rs_months(m, 3), "Half Yearly": rs_months(m, 6), "Yearly": rs_months(m, 12)})
        if intraday:
            m1 = frame(intra(key, "minutes", 1))
            out["1m"], out["3m"] = m1, rs_min(m1, 3)
            m5 = frame(hist(key, "minutes", 5, 20) + intra(key, "minutes", 5))
            out["5m"] = m5
            for n in (10, 15, 30):
                out[f"{n}m"] = rs_min(m5, n)
            h1 = frame(hist(key, "hours", 1, 85) + intra(key, "hours", 1))
            out["1h"], out["2h"], out["4h"] = h1, rs_hr(h1, 2), rs_hr(h1, 4)
    except Exception as e:
        print(sym, "error:", e)
    return sym, out


def main():
    if not TOKEN:
        sys.exit("UPSTOX_TOKEN secret set nahi hai")
    cfg = json.load(open("config.json"))
    universe = cfg.get("universe", "both")            # fno | nifty500 | both
    fno, eq = load_instruments()
    n500 = nifty500_symbols() if universe != "fno" else []
    stocks, fno_set, n500_set = build_universe(fno, eq, n500, universe)
    mode = cfg.get("intraday_for", "watchlist")       # watchlist | fno | none
    wl = {str(x).upper() for x in cfg.get("watchlist", [])}
    intra_set = {"watchlist": wl & set(stocks), "fno": fno_set}.get(mode, set())
    est = len(stocks) * 3 + len(intra_set) * 5
    print(f"Stocks: {len(stocks)} (F&O {len(fno_set)}, Nifty500 {len(n500_set)}) | intraday: {mode} ({len(intra_set)} stocks) | ~{est} API calls")
    if est > 1900:
        print("WARNING: Upstox limit 2000 calls / 30 min hai - universe ya intraday_for kam karein")
    cand, trends = {tf: {} for tf in ALL_TFS}, {}
    with ThreadPoolExecutor(6) as ex:
        for n, (sym, frames) in enumerate(ex.map(lambda it: process(it, it[0] in intra_set), stocks.items()), 1):
            if all(tf in frames and len(frames[tf]) > 50 for tf in ("Daily", "Weekly", "Monthly")):
                trends[sym] = {tf: trend(frames[tf]) for tf in ("Daily", "Weekly", "Monthly")}
            for tf, df in frames.items():
                if len(df) > 8:
                    cand[tf][sym] = {k: [round(float(x), 2) for x in df[k].values[-KEEP:]] for k in "ohlc"}
                    cand[tf][sym]["v"] = [int(x) if x == x else 0 for x in df["v"].values[-KEEP:]]
            if n % 20 == 0:
                print(n, "/", len(stocks))
    if sum(len(cand[tf]) for tf in ("Daily", "Weekly", "Monthly")) == 0:
        sys.exit("Koi data nahi mila - token ya API check karein")  # site purani hi rahegi
    os.makedirs(OUT, exist_ok=True)
    for tf in ALL_TFS:
        with open(f"{OUT}/c_{tf.replace(' ', '_')}.json", "w") as f:
            json.dump(cand[tf], f, separators=(",", ":"))
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    with open(f"{OUT}/data.json", "w") as f:
        json.dump({"updated": dt.datetime.now(ist).strftime("%d %b %Y, %I:%M %p IST"), "total": len(stocks),
                   "trend": trends, "symbols": sorted(stocks), "fno": sorted(fno_set), "n500": sorted(n500_set), "watchlist": [str(x).upper() for x in cfg.get("watchlist", [])], "watchlist_only": bool(cfg.get("watchlist_only", False)), "alert_tfs": cfg["alert_timeframes"], "alert_pct": cfg["alert_distance_pct"]}, f)
    print("Done")


if __name__ == "__main__":
    main()
