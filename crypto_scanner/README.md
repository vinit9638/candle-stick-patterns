# Binance USDT Crypto Scanner

A small Python script that scans every actively trading **USDT spot pair on Binance**
and scores each coin on three daily-chart signals. It uses only the public Binance
REST API, so **no API key is needed**.

> ⚠️ **These are signals, not guarantees.** A high score means a setup matches the rules
> below. It does not mean the price will go up. This is not financial advice.

## The signals (1 point each, max 3)

| # | Signal | Rule |
|---|--------|------|
| 1 | **Volume spike** | Latest day's volume ≥ 3× the average volume of the previous 20 days |
| 2 | **Bollinger squeeze** | 20-period Bollinger Bands (2 std). Bandwidth = (upper − lower) / middle. Squeeze if the current bandwidth, or the minimum bandwidth over the past 5 days, is in the lowest 10% of the ~120-day window |
| 3 | **Resistance breakout** | Latest close > highest high of the previous 30 days, **and** latest volume > the 20-day average |

How the data is handled:

- Pairs come from `/api/v3/exchangeInfo` (quote asset USDT, status `TRADING`).
- 24h price, % change and quote volume come from a single `/api/v3/ticker/24hr` call.
  Pairs with under **500,000 USDT** of 24h quote volume are skipped.
- Daily candles come from `/api/v3/klines` (`interval=1d`, `limit=120`).
- **"Latest day" means the most recent completed daily candle** (Binance days close at
  00:00 UTC). The still-forming candle is dropped, because its partial volume would
  make volume spikes look smaller than they are.
- Coins with too little history (very new listings) are skipped.
- The script sleeps between kline requests and backs off automatically if Binance
  returns HTTP 429/418 (rate limited).

## Setup

You need Python 3.8 or newer.

```bash
cd crypto_scanner
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running it

```bash
python scanner.py                 # coins scoring 2 or 3 (default)
python scanner.py --min-score 3   # only coins hitting all three signals
python scanner.py --min-score 0   # every liquid pair, for reference
```

Options:

| Option | Default | Description |
|--------|---------|-------------|
| `--min-score N` | `2` | Show only coins with score ≥ N (0–3) |
| `--sleep SECONDS` | `0.1` | Pause between kline requests. Raise this if you get rate limited |
| `--output-dir DIR` | `.` | Folder where the CSV is saved |
| `--base-url URL` | `https://api.binance.com` | Binance API host, e.g. `https://data-api.binance.vision` if your region is blocked |
| `--limit N` | none | Scan only the first N liquid pairs (useful for a quick test) |

A full scan of a few hundred pairs takes about a minute.

## Output

A table sorted by score (3 first), with ties broken by volume ratio:

```
   Symbol   Price  24h %  Vol Ratio  BW Pctl Vol Spike Squeeze Breakout  Score
  XYZUSDT  0.4821   18.3       4.12      6.9       yes     yes      yes      3
  ABCUSDT   12.07    6.1       3.35     41.2       yes      no      yes      2
```

- **Vol Ratio**: latest completed day's volume ÷ the 20-day average
- **BW Pctl**: where today's Bollinger bandwidth ranks in the window (0 = tightest)
- **Vol Spike / Squeeze / Breakout**: whether each signal fired

The same results are saved to `scan_results_YYYYMMDD_HHMMSS.csv` (UTC timestamp).

The example above is illustrative, not real market data.

## Note on location

Binance blocks `api.binance.com` in some regions (for example the US, where you get
HTTP 451). If that happens, use Binance's public market-data mirror:

```bash
python scanner.py --base-url https://data-api.binance.vision
```
