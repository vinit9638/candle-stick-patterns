#!/usr/bin/env python3
"""
Binance USDT spot scanner.

Scans every actively trading USDT spot pair on Binance and scores each coin on
three daily-chart signals:

  1. Volume spike        - latest day's volume >= 3x the 20-day average volume
  2. Bollinger squeeze   - 20-period, 2-std Bollinger bandwidth is in the lowest
                           10% of the lookback window (now, or at any point in
                           the past 5 days)
  3. Resistance breakout - latest close above the highest high of the previous
                           30 days, on above-average volume

Each signal is worth 1 point (max score 3). Results at or above --min-score are
printed as a table and saved to a timestamped CSV.

Only the public Binance REST API is used, so no API key is needed.

DISCLAIMER: These are technical signals, not guarantees. Nothing here is
financial advice.
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_URL = "https://api.binance.com"

QUOTE_ASSET = "USDT"
MIN_QUOTE_VOLUME = 500_000      # skip pairs trading less than this in 24h (USDT)
KLINE_INTERVAL = "1d"
KLINE_LIMIT = 120               # number of daily candles to request

# Signal parameters
VOLUME_AVG_PERIOD = 20          # days used for the average-volume baseline
VOLUME_SPIKE_MULTIPLIER = 3.0   # latest volume must be >= this x average
BB_PERIOD = 20                  # Bollinger Band moving-average period
BB_STD = 2.0                    # Bollinger Band width in standard deviations
SQUEEZE_PERCENTILE = 10.0       # "lowest 10%" threshold
SQUEEZE_RECENT_DAYS = 5         # also count a squeeze seen in the last N days
BREAKOUT_LOOKBACK = 30          # days of prior highs that define resistance

# Need enough candles for the Bollinger window plus a meaningful history
MIN_CANDLES = max(BB_PERIOD + SQUEEZE_RECENT_DAYS + 10, BREAKOUT_LOOKBACK + 1)

DISCLAIMER = (
    "NOTE: These are technical signals, not guarantees. They can and do fail.\n"
    "      This is not financial advice - always do your own research."
)


# --------------------------------------------------------------------------- #
# Binance API helpers
# --------------------------------------------------------------------------- #
def api_get(session, path, params=None, max_retries=5):
    """GET a Binance endpoint, backing off politely if rate-limited.

    Binance returns HTTP 429 when you exceed the request-weight limit and 418
    if you keep hammering it after that. Both include a Retry-After header.
    """
    url = BASE_URL + path
    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=15)
        except requests.RequestException as exc:
            wait = 2 ** attempt
            print(f"  network error ({exc}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        if resp.status_code in (429, 418):
            wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
            print(f"  rate limited (HTTP {resp.status_code}); sleeping {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"Giving up on {path} after {max_retries} attempts")


def get_usdt_symbols(session):
    """Return all USDT-quoted spot symbols whose status is TRADING."""
    info = api_get(session, "/api/v3/exchangeInfo")
    return [
        s["symbol"]
        for s in info["symbols"]
        if s.get("quoteAsset") == QUOTE_ASSET
        and s.get("status") == "TRADING"
        and s.get("isSpotTradingAllowed", True)
    ]


def get_24h_tickers(session):
    """Fetch 24h stats for every symbol in ONE request (far cheaper than one
    call per symbol). Returns {symbol: ticker_dict}."""
    tickers = api_get(session, "/api/v3/ticker/24hr")
    return {t["symbol"]: t for t in tickers}


def get_daily_klines(session, symbol):
    """Fetch daily candles for a symbol as a DataFrame.

    The final candle Binance returns is the *current*, still-forming day. We
    drop it so that "latest day" means the most recent COMPLETED day - a
    half-finished day would make the volume look artificially low.
    """
    raw = api_get(session, "/api/v3/klines", {
        "symbol": symbol,
        "interval": KLINE_INTERVAL,
        "limit": KLINE_LIMIT,
    })
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_base",
            "taker_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[c] = df[c].astype(float)

    now_ms = int(time.time() * 1000)
    df = df[df["close_time"] < now_ms]          # keep completed candles only
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
def volume_spike(df):
    """Signal 1: latest volume vs. the average of the previous 20 days.

    The latest day is excluded from its own baseline so a big spike doesn't
    inflate the average it is compared against.
    Returns (is_spike, volume_ratio, avg_volume).
    """
    latest_vol = df["volume"].iloc[-1]
    avg_vol = df["volume"].iloc[-(VOLUME_AVG_PERIOD + 1):-1].mean()
    ratio = latest_vol / avg_vol if avg_vol > 0 else 0.0
    return ratio >= VOLUME_SPIKE_MULTIPLIER, ratio, avg_vol


def bollinger_squeeze(df):
    """Signal 2: Bollinger Band squeeze.

    Bandwidth = (upper - lower) / middle. We rank the current bandwidth
    against every bandwidth value in the lookback window (up to 120 days).
    A squeeze is flagged when the current bandwidth - or any bandwidth in the
    last 5 days (i.e. the recent minimum) - sits in the lowest 10%.
    Returns (is_squeeze, current_bandwidth_percentile).
    """
    close = df["close"]
    middle = close.rolling(BB_PERIOD).mean()
    std = close.rolling(BB_PERIOD).std(ddof=0)   # population std, per Bollinger
    upper = middle + BB_STD * std
    lower = middle - BB_STD * std
    bandwidth = ((upper - lower) / middle).dropna()

    if bandwidth.empty:
        return False, float("nan")

    def percentile_of(value):
        # Percent of bandwidth readings that are <= value (0 = tightest ever)
        return (bandwidth <= value).mean() * 100

    current_pct = percentile_of(bandwidth.iloc[-1])
    recent_min_pct = percentile_of(bandwidth.iloc[-SQUEEZE_RECENT_DAYS:].min())

    is_squeeze = (current_pct <= SQUEEZE_PERCENTILE
                  or recent_min_pct <= SQUEEZE_PERCENTILE)
    return is_squeeze, current_pct


def resistance_breakout(df, avg_vol):
    """Signal 3: close above the prior 30-day high, on above-average volume.

    "Previous 30 days" excludes the latest candle, so we compare today's
    close with resistance that existed BEFORE today.
    """
    latest_close = df["close"].iloc[-1]
    latest_vol = df["volume"].iloc[-1]
    resistance = df["high"].iloc[-(BREAKOUT_LOOKBACK + 1):-1].max()
    return bool(latest_close > resistance and latest_vol > avg_vol)


def analyze(df):
    """Run all three signals on one coin's candles and return a result dict."""
    spike, vol_ratio, avg_vol = volume_spike(df)
    squeeze, bw_pct = bollinger_squeeze(df)
    breakout = resistance_breakout(df, avg_vol)
    return {
        "volume_spike": spike,
        "volume_ratio": vol_ratio,
        "squeeze": squeeze,
        "bandwidth_percentile": bw_pct,
        "breakout": breakout,
        "score": int(spike) + int(squeeze) + int(breakout),
    }


# --------------------------------------------------------------------------- #
# Main scan
# --------------------------------------------------------------------------- #
def scan(min_score, sleep_seconds, limit=None):
    session = requests.Session()
    session.headers["User-Agent"] = "crypto-scanner/1.0"

    print("Fetching exchange info...")
    symbols = get_usdt_symbols(session)
    print(f"  {len(symbols)} USDT pairs with status TRADING")

    print("Fetching 24h tickers...")
    tickers = get_24h_tickers(session)

    # Liquidity filter: drop thinly traded pairs before pulling candles
    liquid = [s for s in symbols
              if s in tickers
              and float(tickers[s]["quoteVolume"]) >= MIN_QUOTE_VOLUME]
    print(f"  {len(liquid)} pairs with 24h volume >= {MIN_QUOTE_VOLUME:,} USDT")
    if limit:
        liquid = liquid[:limit]

    rows = []
    for i, symbol in enumerate(liquid, 1):
        print(f"\r  Scanning {i}/{len(liquid)}: {symbol:<14}", end="", flush=True)
        try:
            df = get_daily_klines(session, symbol)
        except Exception as exc:  # keep going if a single symbol fails
            print(f"\n  ! {symbol}: {exc}", file=sys.stderr)
            continue
        finally:
            time.sleep(sleep_seconds)   # stay well under Binance rate limits

        if len(df) < MIN_CANDLES:
            continue                    # too new to evaluate reliably

        result = analyze(df)
        t = tickers[symbol]
        rows.append({
            "symbol": symbol,
            "price": float(t["lastPrice"]),
            "change_24h_pct": float(t["priceChangePercent"]),
            "volume_ratio": round(result["volume_ratio"], 2),
            "bandwidth_percentile": round(result["bandwidth_percentile"], 1),
            "volume_spike": "yes" if result["volume_spike"] else "no",
            "squeeze": "yes" if result["squeeze"] else "no",
            "breakout": "yes" if result["breakout"] else "no",
            "score": result["score"],
        })
    print()  # finish the progress line

    results = pd.DataFrame(rows, columns=[
        "symbol", "price", "change_24h_pct", "volume_ratio",
        "bandwidth_percentile", "volume_spike", "squeeze", "breakout", "score",
    ])
    results = results[results["score"] >= min_score]
    # Highest score first; break ties by the strongest volume ratio
    results = results.sort_values(["score", "volume_ratio"],
                                  ascending=[False, False]).reset_index(drop=True)
    return results, len(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Scan Binance USDT spot pairs for volume spikes, "
                    "Bollinger squeezes and resistance breakouts.")
    parser.add_argument("--min-score", type=int, default=2, choices=[0, 1, 2, 3],
                        help="only show coins with at least this score (default: 2)")
    parser.add_argument("--sleep", type=float, default=0.1,
                        help="seconds to pause between kline requests (default: 0.1)")
    parser.add_argument("--output-dir", default=".",
                        help="folder for the CSV file (default: current folder)")
    parser.add_argument("--limit", type=int, default=None,
                        help="scan only the first N liquid pairs (handy for a quick test)")
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    results, scanned = scan(args.min_score, args.sleep, args.limit)

    print(f"\nScanned {scanned} pairs at {started:%Y-%m-%d %H:%M UTC}. "
          f"{len(results)} with score >= {args.min_score}.\n")

    if results.empty:
        print("No coins matched.")
    else:
        display = results.rename(columns={
            "symbol": "Symbol",
            "price": "Price",
            "change_24h_pct": "24h %",
            "volume_ratio": "Vol Ratio",
            "bandwidth_percentile": "BW Pctl",
            "volume_spike": "Vol Spike",
            "squeeze": "Squeeze",
            "breakout": "Breakout",
            "score": "Score",
        })
        with pd.option_context("display.max_rows", None,
                               "display.width", 200,
                               "display.float_format", "{:,.6g}".format):
            print(display.to_string(index=False))

    os.makedirs(args.output_dir, exist_ok=True)
    csv_path = os.path.join(args.output_dir,
                            f"scan_results_{started:%Y%m%d_%H%M%S}.csv")
    results.to_csv(csv_path, index=False)
    print(f"\nSaved results to {csv_path}")

    print("\n" + DISCLAIMER)


if __name__ == "__main__":
    main()
