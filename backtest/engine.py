"""Event-style portfolio simulator that mirrors the live bot's decision loop.

Each hour: mark the book to market, apply the drawdown overlay to the
strategy's target weights, and trade only the coins whose weight is off by
more than the rebalance band. Fees are charged on traded notional, plus a
slippage allowance for crossing the spread.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from bot.risk import RiskParams, drawdown_multiplier

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "klines_1h")


@dataclass
class ExecParams:
    fee: float = 0.001          # taker 0.1%; 0.0005 if filled as maker
    slippage: float = 0.0002    # half-spread allowance
    band: float = 0.02          # min |target - current| weight change to trade
    min_trade_usd: float = 50.0


def load_panel(coins: list[str] | None = None, start: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    closes, qv = {}, {}
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.csv"))):
        coin = os.path.basename(path)[:-4]
        if coins and coin not in coins:
            continue
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        closes[coin], qv[coin] = df["close"], df["quote_volume"]
    closes, qv = pd.DataFrame(closes).sort_index(), pd.DataFrame(qv).sort_index()
    full = pd.date_range(closes.index[0], closes.index[-1], freq="1h")
    closes, qv = closes.reindex(full).ffill(limit=3), qv.reindex(full).fillna(0.0)
    if start:
        closes, qv = closes.loc[start:], qv.loc[start:]
    return closes, qv


def simulate(closes: pd.DataFrame, weights: pd.DataFrame, start: int, end: int,
             ex: ExecParams, risk: RiskParams | None, capital: float = 100_000.0) -> tuple[pd.Series, dict]:
    """Simulate hours [start, end) starting from all-cash. Returns hourly equity and trade stats."""
    px = closes.values
    tw = weights.values
    n = px.shape[1]
    units = np.zeros(n)
    cash = capital
    peak = capital
    equity = np.empty(end - start + 1)
    traded_notional, n_trades = 0.0, 0
    trades_per_day: dict = {}

    for k, t in enumerate(range(start, end)):
        p = np.nan_to_num(px[t], nan=0.0)
        nav = cash + float(units @ p)
        equity[k] = nav
        peak = max(peak, nav)
        mult = drawdown_multiplier(nav, peak, risk) if risk else 1.0
        target = np.nan_to_num(tw[t]) * mult
        cur = np.where(p > 0, units * p / nav, 0.0)
        diff = target - cur
        trade = (np.abs(diff) > ex.band) | ((target == 0) & (cur != 0))
        trade &= (np.abs(diff) * nav > ex.min_trade_usd) & (p > 0)
        if trade.any():
            dv = diff[trade] * nav
            cost = np.abs(dv).sum() * (ex.fee + ex.slippage)
            units[trade] += dv / p[trade]
            cash -= dv.sum() + cost
            traded_notional += np.abs(dv).sum()
            n_trades += int(trade.sum())
            day = closes.index[t].normalize()
            trades_per_day[day] = trades_per_day.get(day, 0) + int(trade.sum())

    p = np.nan_to_num(px[end], nan=0.0)
    equity[-1] = cash + float(units @ p)
    eq = pd.Series(equity, index=closes.index[start:end + 1])
    days = (end - start) / 24
    stats = {
        "turnover_per_day": traded_notional / capital / max(days, 1e-9),
        "trades": n_trades,
        "days_with_trades": len(trades_per_day),
        "avg_gross": float(np.mean(np.abs(tw[start:end]).sum(axis=1))),
    }
    return eq, stats
