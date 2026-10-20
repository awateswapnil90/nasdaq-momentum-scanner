#!/usr/bin/env python3
"""
NASDAQ Momentum Scanner — Phase 1: data downloader
==================================================

What it does
  1. Builds the universe: NASDAQ-listed COMMON STOCKS only (no ETFs, warrants,
     rights, units, preferreds, notes, test issues), price >= MIN_PRICE,
     ranked by market cap, top N kept (default 200).
  2. Downloads daily OHLCV from Yahoo Finance (yfinance), split/dividend adjusted.
  3. Writes  scanner_data.js  next to this script. Open nasdaq_scanner.html in
     the same folder and it loads automatically (or use "Load data file").

Usage
  pip install yfinance pandas requests
  python download_data.py                  # top 500 by market cap
  python download_data.py --top 500        # change universe size
  python download_data.py --min-price 10   # change penny-stock cut-off
  python download_data.py --tickers my_list.txt   # use your own list (one ticker per line)

Re-run once a day after the US close (after ~2:00 AM IST) to refresh.
"""

import argparse
import datetime as dt
import json
import math
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance is not installed. Run:  pip install yfinance pandas requests")

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "scanner_data.js"
UNIVERSE_CACHE = HERE / "universe_cache.csv"

BARS_TO_KEEP = 800          # ~3.2 years of daily bars stored for the page (EMA200 warm-up + 252-day return)
HISTORY_PERIOD = "max"      # full history is downloaded so ATH / ATL are correct
BATCH_SIZE = 50             # tickers per yfinance request

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

# Security names containing any of these are NOT common stock
EXCLUDE_NAME_PATTERNS = re.compile(
    r"\b(?:warrants?|rights?|units?|preferred|pref\.?|notes?|debentures?|"
    r"trust preferred|depositary shares? representing .*preferred|subordinated|"
    r"perpetual|etf|etn|fund|index)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- universe
def fetch_nasdaq_screener():
    """All NASDAQ-listed stocks with market cap from Nasdaq's public screener API."""
    url = ("https://api.nasdaq.com/api/screener/stocks"
           "?tableonly=true&limit=10000&offset=0&exchange=NASDAQ&download=true")
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    rows = r.json()["data"]["rows"]
    df = pd.DataFrame(rows)
    df = df.rename(columns={"symbol": "symbol", "name": "name", "lastsale": "last",
                            "marketCap": "mcap", "sector": "sector", "industry": "industry"})
    df["last"] = pd.to_numeric(df["last"].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
    df["mcap"] = pd.to_numeric(df["mcap"].astype(str).str.replace(",", ""), errors="coerce")
    df["symbol"] = df["symbol"].astype(str).str.strip()
    return df[["symbol", "name", "last", "mcap", "sector", "industry"]]


def fetch_nasdaq_listed_flags():
    """nasdaqtrader symbol directory — used to drop ETFs and test issues."""
    url = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        lines = [l for l in r.text.splitlines() if l and not l.startswith("File Creation Time")]
        cols = lines[0].split("|")
        data = [l.split("|") for l in lines[1:]]
        df = pd.DataFrame(data, columns=cols)
        return df.set_index("Symbol")
    except Exception as e:
        print(f"  ! nasdaqlisted.txt not reachable ({e}); continuing with name-based filters only")
        return None


def build_universe(top_n, min_price):
    print("Building universe from Nasdaq screener ...")
    try:
        df = fetch_nasdaq_screener()
    except Exception as e:
        # Nasdaq's site sometimes refuses cloud servers (e.g. GitHub). Fall back to the last good list.
        if UNIVERSE_CACHE.exists():
            print(f"  ! Nasdaq screener not reachable ({e}); using last saved list {UNIVERSE_CACHE.name}")
            df = pd.read_csv(UNIVERSE_CACHE)
            df = df[df["last"] >= min_price].sort_values("mcap", ascending=False).head(top_n).reset_index(drop=True)
            print(f"  {len(df)} stocks from saved list")
            return df
        raise
    n0 = len(df)

    # symbol hygiene: drop share classes with ^ or / (preferreds, warrants on Nasdaq feed)
    df = df[~df["symbol"].str.contains(r"[\^/ ]", regex=True)]
    # name-based exclusion
    df = df[~df["name"].fillna("").str.contains(EXCLUDE_NAME_PATTERNS)]

    flags = fetch_nasdaq_listed_flags()
    if flags is not None:
        ok = flags[(flags.get("ETF", "N") == "N") & (flags.get("Test Issue", "N") == "N")].index
        df = df[df["symbol"].isin(ok)]

    df = df[(df["last"] >= min_price) & (df["mcap"] > 0)].sort_values("mcap", ascending=False)
    try:
        df.to_csv(UNIVERSE_CACHE, index=False)      # full filtered list, reused if Nasdaq is unreachable next time
    except Exception:
        pass
    df = df.head(top_n).reset_index(drop=True)
    print(f"  {n0} NASDAQ rows -> {len(df)} common stocks kept (price >= ${min_price}, top {top_n} by market cap)")
    return df


def universe_from_file(path):
    tickers = [t.strip().upper() for t in Path(path).read_text().splitlines() if t.strip() and not t.startswith("#")]
    print(f"Using {len(tickers)} tickers from {path}")
    return pd.DataFrame({"symbol": tickers, "name": tickers, "last": float("nan"),
                         "mcap": float("nan"), "sector": "", "industry": ""})


# --------------------------------------------------------------------------- prices
def yahoo_symbol(sym):
    return sym.replace(".", "-")          # BRK.B style -> BRK-B (rare on Nasdaq, harmless)


def download_prices(symbols):
    """Returns dict symbol -> DataFrame[Open, High, Low, Close, Volume] (adjusted)."""
    out = {}
    ysyms = [yahoo_symbol(s) for s in symbols]
    back = dict(zip(ysyms, symbols))
    for i in range(0, len(ysyms), BATCH_SIZE):
        batch = ysyms[i:i + BATCH_SIZE]
        print(f"  downloading {i + 1}-{i + len(batch)} of {len(ysyms)} ...")
        for attempt in range(3):
            try:
                raw = yf.download(batch, period=HISTORY_PERIOD, interval="1d", auto_adjust=True,
                                  group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:
                print(f"    retry {attempt + 1}: {e}")
                time.sleep(5)
        else:
            continue
        for ys in batch:
            try:
                d = raw[ys] if isinstance(raw.columns, pd.MultiIndex) else raw
                d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
                if len(d) > 0:
                    out[back[ys]] = d
            except KeyError:
                pass
        time.sleep(1)
    return out


# --------------------------------------------------------------------------- fundamentals (TTM)
FUND_CACHE = HERE / "fundamentals_cache.json"
FUND_MAX_AGE_DAYS = 7       # quarterly results change rarely; re-fetch weekly (or use --refresh-fundamentals)
FUND_WORKERS = 6
REV_ROWS = ["Total Revenue", "Operating Revenue", "Revenue"]
NI_ROWS = ["Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest"]


def _pick_row(df, names):
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def fetch_one_fundamental(sym):
    """Quarterly revenue & net income, newest first: {"qd":[dates], "rev":[...], "ni":[...]}"""
    for attempt in range(2):
        try:
            t = yf.Ticker(yahoo_symbol(sym))
            df = t.quarterly_income_stmt
            if df is None or df.empty:
                df = t.quarterly_financials
            if df is None or df.empty:
                return None
            df = df.reindex(sorted(df.columns, reverse=True), axis=1)   # newest quarter first
            rev, ni = _pick_row(df, REV_ROWS), _pick_row(df, NI_ROWS)
            return {
                "qd": [pd.Timestamp(c).strftime("%Y-%m-%d") for c in df.columns],
                "rev": [None if rev is None or pd.isna(rev[c]) else float(rev[c]) for c in df.columns],
                "ni": [None if ni is None or pd.isna(ni[c]) else float(ni[c]) for c in df.columns],
            }
        except Exception:
            time.sleep(2)
    return None


def load_fundamentals(symbols, refresh=False):
    cache = {}
    if FUND_CACHE.exists():
        try:
            cache = json.loads(FUND_CACHE.read_text())
        except Exception:
            cache = {}
    today = dt.date.today()
    todo = [s for s in symbols if refresh or s not in cache or
            (today - dt.date.fromisoformat(cache[s].get("fetched", "2000-01-01"))).days > FUND_MAX_AGE_DAYS]
    if todo:
        print(f"Downloading quarterly results for {len(todo)} stocks (cached: {len(symbols) - len(todo)}) ...")
        from concurrent.futures import ThreadPoolExecutor
        done = 0
        with ThreadPoolExecutor(FUND_WORKERS) as ex:
            for sym, f in zip(todo, ex.map(fetch_one_fundamental, todo)):
                done += 1
                if f:
                    f["fetched"] = today.isoformat()
                    cache[sym] = f
                if done % 25 == 0 or done == len(todo):
                    print(f"  {done}/{len(todo)}")
        FUND_CACHE.write_text(json.dumps(cache))
    else:
        print(f"Quarterly results: all {len(symbols)} from cache (refreshed weekly)")
    return cache


# --------------------------------------------------------------------------- output
def rp(x):
    """Price rounding that keeps the file small: 2 decimals above $10, 3 above $1, else 4."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    x = float(x)
    return round(x, 2 if abs(x) >= 10 else 3 if abs(x) >= 1 else 4)


def r4(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), 4)


def build_payload(universe, prices, top_n, min_price, funds=None):
    funds = funds or {}
    # common calendar = union of all dates in the last BARS_TO_KEEP sessions
    all_dates = sorted(set().union(*[set(d.index) for d in prices.values()])) if prices else []
    all_dates = all_dates[-BARS_TO_KEEP:]
    date_pos = {d: i for i, d in enumerate(all_dates)}

    stocks = []
    for _, row in universe.iterrows():
        sym = row["symbol"]
        d = prices.get(sym)
        if d is None or len(d) < 30:
            continue
        first_kept = all_dates[0] if all_dates else None
        pre = d[d.index < first_kept] if first_kept is not None else d.iloc[0:0]
        hp = pre["High"].max() if len(pre) else None   # highest high BEFORE stored window (for ATH as of any date)
        lp = pre["Low"].min() if len(pre) else None    # lowest low BEFORE stored window (for ATL)
        d = d[d.index.isin(date_pos)]
        if d.empty:
            continue
        # align to calendar from first available bar, forward-fill rare gaps (halts)
        start = date_pos[d.index[0]]
        cal = all_dates[start:]
        d = d.reindex(cal)
        d["Close"] = d["Close"].ffill()
        for c in ("Open", "High", "Low"):
            d[c] = d[c].fillna(d["Close"])
        d["Volume"] = d["Volume"].fillna(0)
        stocks.append({
            "s": sym,
            "n": str(row.get("name", sym))[:60],
            "sec": row.get("sector") or "",
            "ind": row.get("industry") or "",
            "mc": None if pd.isna(row.get("mcap")) else float(row["mcap"]),
            "st": start,
            "hp": rp(hp), "lp": rp(lp),
            "h": [rp(x) for x in d["High"]],
            "l": [rp(x) for x in d["Low"]],
            "c": [rp(x) for x in d["Close"]],
            "v": [int(x) for x in d["Volume"]],
            "f": ({k: funds[sym][k][:6] for k in ("qd", "rev", "ni")} if sym in funds else None),
        })
    return {
        "meta": {
            "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": "Yahoo Finance via yfinance (split/dividend adjusted)",
            "universe": f"NASDAQ common stocks, price >= ${min_price}, top {top_n} by market cap",
            "count": len(stocks),
            "last_date": all_dates[-1].strftime("%Y-%m-%d") if all_dates else None,
        },
        "dates": [d.strftime("%Y-%m-%d") for d in all_dates],
        "stocks": stocks,
    }


def main():
    ap = argparse.ArgumentParser(description="Download data for the NASDAQ momentum scanner")
    ap.add_argument("--top", type=int, default=500, help="universe size: top N by market cap (default 500)")
    ap.add_argument("--min-price", type=float, default=5.0, help="exclude stocks below this price (default $5)")
    ap.add_argument("--tickers", help="optional text file with one ticker per line (overrides universe build)")
    ap.add_argument("--refresh-fundamentals", action="store_true", help="re-download quarterly results now (default: weekly)")
    ap.add_argument("--no-fundamentals", action="store_true", help="skip quarterly results (TTM conditions will show no data)")
    ap.add_argument("--out", default=str(OUT_FILE), help="output file (default scanner_data.js)")
    a = ap.parse_args()

    uni = universe_from_file(a.tickers) if a.tickers else build_universe(a.top, a.min_price)
    print(f"Downloading daily prices for {len(uni)} stocks ...")
    prices = download_prices(list(uni["symbol"]))
    missing = sorted(set(uni["symbol"]) - set(prices))
    if missing:
        print(f"  ! no data for {len(missing)}: {', '.join(missing[:20])}{' ...' if len(missing) > 20 else ''}")

    funds = {} if a.no_fundamentals else load_fundamentals(sorted(prices), a.refresh_fundamentals)
    nf = sum(1 for s in prices if s in funds)
    print(f"  quarterly results available for {nf} of {len(prices)} stocks")

    payload = build_payload(uni, prices, a.top, a.min_price, funds)
    js = "window.SCANNER_DATA = " + json.dumps(payload, separators=(",", ":")) + ";\n"
    Path(a.out).write_text(js, encoding="utf-8")
    mb = len(js) / 1e6
    print(f"Saved {payload['meta']['count']} stocks, last bar {payload['meta']['last_date']} -> {a.out} ({mb:.1f} MB)")
    print("Now open nasdaq_scanner.html (same folder).")


if __name__ == "__main__":
    main()
