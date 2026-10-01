"""Competition scoring metrics: return, Sharpe, Sortino, Calmar and the
composite 0.4*Sortino + 0.3*Sharpe + 0.3*Calmar."""
from __future__ import annotations

import numpy as np
import pandas as pd

RATIO_CAP = 50.0  # ratios explode when downside is ~0; cap for aggregation


def max_drawdown(equity: np.ndarray) -> float:
    peak = np.maximum.accumulate(equity)
    return float(np.max(1.0 - equity / peak))


def score(equity: pd.Series, periods_per_year: float = 365.0, resample: str | None = "1D") -> dict:
    """Metrics on an equity curve. Ratios are computed on `resample` returns
    (daily by default, matching how leaderboards usually snapshot NAV)."""
    eq = equity.resample(resample).last().dropna() if resample else equity
    eq = pd.concat([equity.iloc[:1], eq]) if resample else eq
    r = eq.pct_change().dropna().values
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    days = (equity.index[-1] - equity.index[0]).total_seconds() / 86400
    mdd = max_drawdown(equity.values)

    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    downside = np.sqrt(np.mean(np.minimum(r, 0.0) ** 2)) if len(r) else 0.0
    ann = np.sqrt(periods_per_year)
    sharpe = r.mean() / sd * ann if sd > 0 else 0.0
    sortino = r.mean() / downside * ann if downside > 0 else (RATIO_CAP if r.mean() > 0 else 0.0)
    ann_ret = (1 + total) ** (365 / max(days, 1e-9)) - 1
    calmar = ann_ret / mdd if mdd > 0 else (RATIO_CAP if total > 0 else 0.0)

    sharpe, sortino, calmar = (float(np.clip(x, -RATIO_CAP, RATIO_CAP)) for x in (sharpe, sortino, calmar))
    return {
        "return": total,
        "max_dd": mdd,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "composite": 0.4 * sortino + 0.3 * sharpe + 0.3 * calmar,
    }
