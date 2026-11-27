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
INTRA_TFS = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "4h"]
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
    for fn in ("nifty500.csv", "ind_nifty500list.csv"):   # NSE se download ki hui file ka koi bhi naam chalega
        if os.path.exists(fn):
            syms = parse_symbols(open(fn, encoding="utf-8-sig", errors="ignore").read())
            print(fn, "se list li:", len(syms))
            if syms:
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


def agg_key(df, key):
    return _agg(df.groupby(key))


def periods_from_daily(d):
    """Daily candles se Weekly / Monthly / Quarterly / Half Yearly / Yearly (aaj ki banti hui candle ke saath)."""
    if d.empty:
        return {}
    wk = (d.index.normalize().tz_localize(None) - pd.Timestamp("2000-01-03")).days // 7   # Monday se hafta
    mk = d.index.year * 12 + d.index.month - 1
    return {"Daily": d, "Weekly": agg_key(d, wk), "Monthly": agg_key(d, mk), "Quarterly": agg_key(d, mk // 3),
            "Half Yearly": agg_key(d, mk // 6), "Yearly": agg_key(d, mk // 12)}


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


def process(item, intraday, full=True):
    sym, key = item
    out = {}
    try:
        if full:   # 2 calls: lambi daily history + aaj ki candle; baaki sab timeframe daily se bante hain
            dh = frame(hist(key, "days", 1, 3600))
            if dh.empty:
                dh = frame(hist(key, "days", 1, 1800))
            today = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
            if len(dh):
                dh = dh[dh.index.date < today]
            d = pd.concat([dh, frame(intra(key, "days", 1))])
            d = d[~d.index.duplicated(keep="last")].sort_index()
            out.update(periods_from_daily(d))
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


def fetch_prev(name):
    """Pichhli baar deploy hui file site se wapas padho (rotate mode mein merge ke liye)."""
    site = os.environ.get("SITE_URL", "")
    if not site:
        return None
    try:
        r = requests.get(f"{site}/{name}?t={int(time.time())}", timeout=60)
        return r.json() if r.ok else None
    except Exception:
        return None


def main():
    if not TOKEN:
        sys.exit("UPSTOX_TOKEN secret set nahi hai")
    cfg = json.load(open("config.json"))
    universe = cfg.get("universe", "both")            # fno | nifty500 | both
    fno, eq = load_instruments()
    n500 = nifty500_symbols() if universe != "fno" else []
    stocks, fno_set, n500_set = build_universe(fno, eq, n500, universe)
    mode_cfg = cfg.get("mode", "full")                # full | intraday (sirf intraday timeframes)
    mode = cfg.get("intraday_for", "watchlist")       # watchlist | fno | rotate | none
    wl = {str(x).upper() for x in cfg.get("watchlist", [])}
    full = mode_cfg != "intraday"
    rotate = mode == "rotate" and full
    intra_set = {"watchlist": wl & set(stocks), "fno": fno_set}.get(mode, set())
    daily_set = set(stocks) if full else set()
    cand, trends, ts_i, ts_d = {tf: {} for tf in ALL_TFS}, {}, {}, {}
    if rotate:
        # Har run mein thode stocks (sabse purane pehle) refresh hote hain, baaki pichhle data se aate hain.
        with ThreadPoolExecutor(6) as ex:
            prevs = list(ex.map(lambda tf: fetch_prev(f"c_{tf.replace(' ', '_')}.json") or {}, ALL_TFS))
        for tf, pv in zip(ALL_TFS, prevs):
            cand[tf] = {x: v for x, v in pv.items() if x in stocks} if isinstance(pv, dict) else {}
        pdj = fetch_prev("data.json") or {}
        ts_i = {x: t for x, t in (pdj.get("ts_intra") or {}).items() if x in stocks}
        ts_d = {x: t for x, t in (pdj.get("ts_daily") or {}).items() if x in stocks}
        trends = {x: v for x, v in (pdj.get("trend") or {}).items() if x in stocks}
        syms = list(stocks)
        n_i, n_d = int(cfg.get("rot_intraday", 30)), int(cfg.get("rot_daily", 60))
        if len(ts_d) < 0.5 * len(syms):      # shuruaat: pehle badi batch (daily-weekly ka data jaldi bharne ke liye)
            n_d = int(cfg.get("rot_daily_bootstrap", 300))
        wl_in = [x for x in syms if x in wl]
        rest = sorted((x for x in syms if x not in wl), key=lambda x: ts_i.get(x, 0))
        intra_set = set(wl_in) | set(rest[:max(0, n_i - len(wl_in))])
        daily_set = set(sorted(syms, key=lambda x: ts_d.get(x, 0))[:n_d])
        print(f"Rotate: purane data wale stocks intraday {len(ts_i)}/{len(syms)}, daily {len(ts_d)}/{len(syms)}")
    elif not full:   # sirf intraday: pehle watchlist, phir F&O, phir baaki; limit ke andar rakhne ke liye trim
        order = [x for x in stocks if x in wl] + [x for x in stocks if x in fno_set and x not in wl] + [x for x in stocks if x not in wl and x not in fno_set]
        maxn = int(cfg.get("max_stocks", 380))
        if len(order) > maxn:
            print(f"WARNING: {len(order)} stocks mein se pehle {maxn} hi scan honge (Upstox limit). max_stocks / universe badlein")
            order = order[:maxn]
        stocks = {x: stocks[x] for x in order}
        fno_set, n500_set, intra_set = fno_set & set(stocks), n500_set & set(stocks), set(stocks)
    est = len(intra_set) * 5 + len(daily_set) * 2
    print(f"Stocks: {len(stocks)} (F&O {len(fno_set)}, Nifty500 {len(n500_set)}) | intraday: {mode} ({len(intra_set)} stocks) | daily+: {len(daily_set)} stocks | ~{est} API calls")
    if (rotate and est > 1000) or est > 1900:
        print("WARNING: Upstox limit 2000 calls / 30 min hai - rot_intraday / rot_daily ya universe kam karein")
    now_t = int(time.time())
    todo = [(x, k) for x, k in stocks.items() if x in intra_set or x in daily_set]
    with ThreadPoolExecutor(6) as ex:
        for n, (sym, frames) in enumerate(ex.map(lambda it: process(it, it[0] in intra_set, it[0] in daily_set), todo), 1):
            if all(tf in frames and len(frames[tf]) > 50 for tf in ("Daily", "Weekly", "Monthly")):
                trends[sym] = {tf: trend(frames[tf]) for tf in ("Daily", "Weekly", "Monthly")}
            got_i = got_d = False
            for tf, df in frames.items():
                if len(df) > 8:
                    cand[tf][sym] = {k: [round(float(x), 2) for x in df[k].values[-KEEP:]] for k in "ohlc"}
                    cand[tf][sym]["v"] = [int(x) if x == x else 0 for x in df["v"].values[-KEEP:]]
                    got_i = got_i or tf in INTRA_TFS
                    got_d = got_d or tf == "Daily"
            if got_i:
                ts_i[sym] = now_t
            if got_d:
                ts_d[sym] = now_t
            if n % 20 == 0:
                print(n, "/", len(todo))
    if sum(len(cand[tf]) for tf in (("Daily", "Weekly", "Monthly") if full else ("5m", "1h"))) == 0:
        sys.exit("Koi data nahi mila - token ya API check karein")  # site purani hi rahegi
    os.makedirs(OUT, exist_ok=True)
    tfs_out = ALL_TFS if full else INTRA_TFS
    for tf in tfs_out:
        with open(f"{OUT}/c_{tf.replace(' ', '_')}.json", "w") as f:
            json.dump(cand[tf], f, separators=(",", ":"))
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    with open(f"{OUT}/data.json", "w") as f:
        json.dump({"updated": dt.datetime.now(ist).strftime("%d %b %Y, %I:%M %p IST"), "total": len(stocks),
                   "trend": trends, "gen": now_t, "ts": ts_i, "ts_intra": ts_i, "ts_daily": ts_d,
                   "keys": stocks, "ltp_url": cfg.get("ltp_url", ""), "ltp_seconds": cfg.get("ltp_seconds", 8),
                   "mode": "full" if full else "intraday", "tfs": tfs_out, "symbols": sorted(stocks),
                   "fno": sorted(fno_set), "n500": sorted(n500_set), "watchlist": sorted(wl),
                   "watchlist_only": bool(cfg.get("watchlist_only", False)),
                   "alert_tfs": cfg["alert_timeframes"], "alert_pct": cfg["alert_distance_pct"]}, f)
    print("Done")


if __name__ == "__main__":
    main()
