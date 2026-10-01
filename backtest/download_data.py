"""Download hourly Binance klines for every coin listed on Roostoo.

Roostoo streams its prices from Binance, so Binance USDT pairs are the
closest available history for backtesting. Output: one parquet-free CSV
per symbol in data/klines_1h/ (close, high, low, volume in quote USD).

Usage:
    python -m backtest.download_data --start 2023-01-01
"""
from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import requests

BINANCE = "https://data-api.binance.vision/api/v3/klines"
ROOSTOO_INFO = "https://mock-api.roostoo.com/v3/exchangeInfo"
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "klines_1h")
HOUR_MS = 3_600_000


def roostoo_coins() -> list[str]:
    pairs = requests.get(ROOSTOO_INFO, timeout=20).json()["TradePairs"]
    return sorted(p.split("/")[0] for p, v in pairs.items() if v.get("CanTrade"))


def fetch_symbol(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame | None:
    symbol = f"{coin}USDT"
    rows, cursor = [], start_ms
    while cursor < end_ms:
        for attempt in range(5):
            try:
                r = requests.get(BINANCE, params={"symbol": symbol, "interval": "1h",
                                                  "startTime": cursor, "limit": 1000}, timeout=20)
                break
            except requests.RequestException:
                time.sleep(2 ** attempt)
        else:
            return None
        if r.status_code == 400:  # symbol not listed on Binance spot
            return None
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + HOUR_MS
        time.sleep(0.05)
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume",
                                     "close_time", "quote_volume", "trades", "tb_base",
                                     "tb_quote", "ignore"])
    df = df[["open_time", "open", "high", "low", "close", "quote_volume"]].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.drop_duplicates("open_time").set_index("open_time")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    start_ms = int(datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(time.time() * 1000)
    coins = roostoo_coins()

    def job(coin: str) -> str:
        df = fetch_symbol(coin, start_ms, end_ms)
        if df is None:
            return f"{coin}: not on Binance spot, skipped"
        df.to_csv(os.path.join(OUT_DIR, f"{coin}.csv"))
        return f"{coin}: {len(df)} bars from {df.index[0]:%Y-%m-%d}"

    with ThreadPoolExecutor(args.workers) as ex:
        for msg in ex.map(job, coins):
            print(msg, flush=True)


if __name__ == "__main__":
    main()
