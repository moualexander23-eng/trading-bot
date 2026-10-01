"""Signal generation and target-portfolio construction.

Everything here is a pure function of hourly price/volume history, so the
same code drives the backtest (vectorised over all hours) and the live bot
(which takes the last row).

Two sleeves with low correlation to each other (~0.1 daily):

  Trend sleeve (directional, long-only)
    For each coin in the 15 most liquid, a multi-horizon trend t-stat:
    log-return over L hours / (hourly vol * sqrt(L)), clipped to [-2, 2],
    averaged over L in {7d, 14d, 30d}. Coins with a positive score are held
    with weight proportional to score / vol, scaled to a target portfolio
    volatility. In a falling market every score turns negative and the
    sleeve goes to cash - this is where the drawdown protection comes from.

  Momentum sleeve (market-neutral, long/short)
    Among the 25 most liquid coins, rank the 14-day return relative to the
    universe average. Long the top k, short the bottom k, inverse-vol
    weighted, equal gross on each side - so it earns relative strength
    persistence without taking a view on the market direction.

The sum is capped per coin and in total gross (no leverage: on Roostoo both
longs and short collateral are funded from USD), and only re-sampled once a
day at `rebalance_hour` UTC to keep turnover - and fees - low.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

HOURS_PER_YEAR = 24 * 365


@dataclass
class StrategyParams:
    candidates: list[str] = field(default_factory=list)
    vol_halflife: int = 72
    min_history: int = 800            # hours of data before a coin is eligible
    # trend sleeve
    trend_universe: int = 15
    trend_lookbacks: tuple[int, ...] = (168, 336, 720)
    trend_target_vol: float = 0.30
    trend_assumed_corr: float = 0.7
    # momentum sleeve
    mom_universe: int = 25
    mom_lookback: int = 336
    mom_k: int = 5
    mom_gross: float = 0.6            # long gross + short gross
    allow_shorts: bool = True
    # portfolio
    max_weight: float = 0.25
    max_gross: float = 0.95
    rebalance_every: int = 24         # hours between re-samples of each tranche
    rebalance_hour: int = 0           # UTC hour of the first tranche
    tranches: int = 1                 # >1 staggers the rebalance (e.g. 3 = 1/3 every 8h)


def hourly_vol(closes: pd.DataFrame, halflife: int) -> pd.DataFrame:
    rets = np.log(closes).diff()
    return rets.ewm(halflife=halflife, min_periods=halflife).std()


def trend_score(closes: pd.DataFrame, lookbacks: tuple[int, ...], vol: pd.DataFrame) -> pd.DataFrame:
    logp = np.log(closes)
    parts = [((logp - logp.shift(L)) / (vol * np.sqrt(L))).clip(-2, 2) / 2 for L in lookbacks]
    return sum(parts) / len(parts)


def liquid_universe(quote_volume: pd.DataFrame, closes: pd.DataFrame, size: int,
                    min_history: int) -> pd.DataFrame:
    """Boolean mask: coin is among the `size` most-traded (7d avg USD volume) with enough history."""
    adv = quote_volume.rolling(168, min_periods=168).mean()
    has_history = closes.notna().rolling(min_history, min_periods=min_history).sum() >= min_history
    rank = adv.where(has_history).rank(axis=1, ascending=False, method="first")
    return rank <= size


def portfolio_vol(weights: pd.DataFrame, ann_vol: pd.DataFrame, rho: float) -> pd.Series:
    """Constant-correlation approximation of portfolio volatility."""
    wv = (weights * ann_vol).fillna(0.0)
    var = (1 - rho) * (wv ** 2).sum(axis=1) + rho * wv.sum(axis=1) ** 2
    return np.sqrt(var)


def trend_sleeve(closes, quote_volume, ann_vol, vol_h, p: StrategyParams):
    universe = liquid_universe(quote_volume, closes, p.trend_universe, p.min_history)
    score = trend_score(closes, p.trend_lookbacks, vol_h).where(universe & ann_vol.notna())
    raw = (score.clip(lower=0) / ann_vol).where(universe).fillna(0.0)
    pv = portfolio_vol(raw, ann_vol, p.trend_assumed_corr)
    w = raw.mul((p.trend_target_vol / pv.replace(0, np.nan)).fillna(0.0), axis=0)
    return w, score


def momentum_sleeve(closes, quote_volume, ann_vol, p: StrategyParams):
    universe = liquid_universe(quote_volume, closes, p.mom_universe, p.min_history) & ann_vol.notna()
    logp = np.log(closes)
    ret = (logp - logp.shift(p.mom_lookback)).where(universe)
    rel = ret.sub(ret.mean(axis=1), axis=0)
    rank_desc = rel.rank(axis=1, ascending=False, method="first")
    rank_asc = rel.rank(axis=1, ascending=True, method="first")
    enough = universe.sum(axis=1) >= 2 * p.mom_k
    longs = (rank_desc <= p.mom_k) & enough.values[:, None]
    shorts = (rank_asc <= p.mom_k) & enough.values[:, None]

    def side(mask):
        inv = (1.0 / ann_vol).where(mask, 0.0).fillna(0.0)
        return inv.div(inv.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)

    half = p.mom_gross / 2
    w = side(longs) * half
    if p.allow_shorts:
        w = w - side(shorts) * half
    return w, rel


def compute(closes: pd.DataFrame, quote_volume: pd.DataFrame,
            p: StrategyParams) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Target weights (fraction of NAV, negative = short) for every hour, plus diagnostics."""
    cols = [c for c in p.candidates if c in closes.columns] if p.candidates else list(closes.columns)
    closes, quote_volume = closes[cols], quote_volume[cols]

    vol_h = hourly_vol(closes, p.vol_halflife)
    ann_vol = vol_h * np.sqrt(HOURS_PER_YEAR)
    w_trend, trend = trend_sleeve(closes, quote_volume, ann_vol, vol_h, p)
    w_mom, rel_mom = momentum_sleeve(closes, quote_volume, ann_vol, p)

    w = (w_trend + w_mom).clip(lower=-p.max_weight, upper=p.max_weight)
    gross = w.abs().sum(axis=1)
    w = w.mul(np.minimum(1.0, p.max_gross / gross.replace(0, np.nan)).fillna(0.0), axis=0)

    if p.rebalance_every > 1:
        # Each tranche re-samples the target once per `rebalance_every` hours at a
        # staggered offset; averaging them spreads trades out and removes the
        # dependence on one arbitrary rebalance time.
        epoch_hours = (w.index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(hours=1)
        step = p.rebalance_every // p.tranches
        parts = []
        for t in range(p.tranches):
            keep = (epoch_hours - p.rebalance_hour - t * step) % p.rebalance_every == 0
            parts.append(w.where(pd.Series(keep, index=w.index), np.nan).ffill())
        w = sum(parts) / p.tranches
    diag = {"trend_score": trend, "mom_rel": rel_mom, "w_trend": w_trend, "w_mom": w_mom}
    return w.fillna(0.0), diag


def target_weights(closes: pd.DataFrame, quote_volume: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    return compute(closes, quote_volume, p)[0]
