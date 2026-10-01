"""Hourly OHLCV history for the live bot.

Roostoo only exposes ticker snapshots, but its prices are streamed from
Binance, so we pull closed hourly klines from Binance's public market-data
mirror (no key required). The cache is bootstrapped once, then each cycle
fetches only the last few bars. If Binance is unreachable for a coin, the
Roostoo ticker's last price is used to synthesise that hour's bar so the
strategy keeps running on the exchange's own prices.
"""
from __future__ import annotations

import logging
import time

import pandas as pd
import requests

log = logging.getLogger("market_data")

BINANCE_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")
HOUR = pd.Timedelta(hours=1)


class MarketData:
    def __init__(self, coins: list[str], history_hours: int = 600):
        self.coins = coins
        self.history_hours = history_hours
        self.closes = pd.DataFrame(dtype=float)
        self.quote_volume = pd.DataFrame(dtype=float)
        self.session = requests.Session()

    def _klines(self, coin: str, limit: int) -> pd.DataFrame | None:
        for host in BINANCE_HOSTS:
            try:
                r = self.session.get(f"{host}/api/v3/klines", timeout=10,
                                     params={"symbol": f"{coin}USDT", "interval": "1h", "limit": limit})
                if r.status_code != 200:
                    continue
                rows = r.json()
                df = pd.DataFrame(rows).iloc[:, [0, 4, 6, 7]]
                df.columns = ["open_time", "close", "close_time", "quote_volume"]
                df = df.astype(float)
                df = df[df["close_time"] < time.time() * 1000]  # drop the still-forming bar
                df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
                return df[["close", "quote_volume"]]
            except (requests.RequestException, ValueError, IndexError) as e:
                log.warning("klines %s via %s failed: %s", coin, host, e)
        return None

    def refresh(self, roostoo_last: dict[str, float] | None = None) -> set[str]:
        """Update the cache. Returns the set of coins whose data is stale."""
        bootstrap = self.closes.empty
        limit = min(self.history_hours, 1000) if bootstrap else 5
        closes, vols = {}, {}
        for coin in self.coins:
            df = self._klines(coin, limit)
            if df is not None and not df.empty:
                closes[coin], vols[coin] = df["close"], df["quote_volume"]
        new_c, new_v = pd.DataFrame(closes), pd.DataFrame(vols)
        self.closes = new_c.combine_first(self.closes) if not bootstrap else new_c
        self.quote_volume = new_v.combine_first(self.quote_volume) if not bootstrap else new_v

        expected = pd.Timestamp.now(tz="UTC").floor("h") - HOUR  # open time of last closed bar
        if expected not in self.closes.index:
            self.closes.loc[expected] = float("nan")
            self.quote_volume.loc[expected] = float("nan")
        self.closes = self.closes.sort_index()
        self.quote_volume = self.quote_volume.sort_index()

        stale = set()
        for coin in self.coins:
            col = self.closes.get(coin)
            if col is None or pd.isna(col.get(expected)):
                price = (roostoo_last or {}).get(coin)
                if price:
                    self.closes.loc[expected, coin] = price
                    prev = self.quote_volume[coin].ffill().iloc[-1] if coin in self.quote_volume else 0.0
                    self.quote_volume.loc[expected, coin] = prev
                    log.warning("%s: Binance bar missing, using Roostoo last price %s", coin, price)
                else:
                    stale.add(coin)

        full = pd.date_range(end=expected, periods=self.history_hours, freq="1h")
        self.closes = self.closes.reindex(full).ffill(limit=3)
        self.quote_volume = self.quote_volume.reindex(full).fillna(0.0)
        return stale
