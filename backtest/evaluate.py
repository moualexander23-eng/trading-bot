"""Backtest report for the configured strategy.

Two views:
  1. Full history (Mar-2023 -> today): one continuous simulation, plus the
     same for each sleeve alone and for BTC buy & hold.
  2. Competition-shaped: start a fresh $100k all-cash portfolio every 2 days
     and run it for 14 days, scoring each window like the judges do
     (return, Sharpe, Sortino, Calmar, composite). Split into in-sample
     (2023-24, where design choices were made) and out-of-sample (2025-26).

Usage:
    python -m backtest.evaluate             # writes backtest/results/
"""
from __future__ import annotations

import argparse
import os
from dataclasses import replace

import numpy as np
import pandas as pd

from backtest.engine import ExecParams, load_panel, simulate
from backtest.metrics import max_drawdown, score
from bot.config import load_config
from bot.risk import RiskParams
from bot.strategy import target_weights

WINDOW_H = 14 * 24
OUT_DIR = os.path.join(os.path.dirname(__file__), "results")
PERIODS = {"in-sample 2023-24": ("2023-03-01", "2024-12-31"), "out-of-sample 2025-26": ("2025-01-01", None)}


def rolling_windows(closes: pd.DataFrame, weights: pd.DataFrame, ex: ExecParams,
                    risk: RiskParams | None, step_days: int = 2, warmup: int = 24 * 30) -> pd.DataFrame:
    rows = []
    for start in range(warmup, len(closes) - WINDOW_H - 1, step_days * 24):
        eq, stats = simulate(closes, weights, start, start + WINDOW_H, ex, risk)
        rows.append({"start": closes.index[start], **score(eq), **stats})
    return pd.DataFrame(rows).set_index("start")


def summarise(df: pd.DataFrame) -> dict:
    return {
        "windows": len(df),
        "mean_ret": df["return"].mean(),
        "median_ret": df["return"].median(),
        "p10_ret": df["return"].quantile(0.10),
        "p90_ret": df["return"].quantile(0.90),
        "win_rate": (df["return"] > 0).mean(),
        "median_mdd": df["max_dd"].median(),
        "p90_mdd": df["max_dd"].quantile(0.90),
        "median_sharpe": df["sharpe"].median(),
        "median_sortino": df["sortino"].median(),
        "median_calmar": df["calmar"].median(),
        "median_composite": df["composite"].median(),
        "turnover/day": df["turnover_per_day"].mean(),
        "trades/day": (df["trades"] / 14).mean(),
        "min_active_days": df["days_with_trades"].min(),
    }


def annual_stats(eq: pd.Series) -> dict:
    d = eq.resample("1D").last().pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    downside = np.sqrt((np.minimum(d, 0) ** 2).mean())
    mdd = max_drawdown(eq.values)
    return {"total_return": eq.iloc[-1] / eq.iloc[0] - 1, "CAGR": cagr, "ann_vol": d.std() * np.sqrt(365),
            "sharpe": d.mean() / d.std() * np.sqrt(365), "sortino": d.mean() / downside * np.sqrt(365),
            "max_dd": mdd, "calmar": cagr / mdd}


def slice_period(closes, w, start, end):
    m = closes.index >= pd.Timestamp(start, tz="UTC") - pd.Timedelta(days=30)
    if end:
        m &= closes.index <= pd.Timestamp(end, tz="UTC")
    return closes[m], w[m]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--step-days", type=int, default=2)
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    cfg = load_config(args.config)
    sp, rp = cfg.strategy, cfg.risk
    ex = ExecParams(fee=cfg.backtest_fee, slippage=cfg.backtest_slippage,
                    band=cfg.execution.rebalance_band, min_trade_usd=cfg.execution.min_order_usd)
    closes, qv = load_panel(sp.candidates)

    variants = {
        "Strategy": target_weights(closes, qv, sp),
        "Trend sleeve only": target_weights(closes, qv, replace(sp, mom_gross=0.0)),
        "Momentum sleeve only": target_weights(closes, qv, replace(sp, trend_target_vol=0.0)),
    }
    btc = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    btc["BTC"] = 1.0
    variants["BTC buy & hold"] = btc
    variants = {k: v.reindex(columns=closes.columns, fill_value=0.0) for k, v in variants.items()}

    # 1. full history
    start = closes.index.get_loc(pd.Timestamp("2023-03-01", tz="UTC"))
    curves, full = {}, {}
    for name, w in variants.items():
        eq, _ = simulate(closes, w, start, len(closes) - 1, ex, rp if name != "BTC buy & hold" else None)
        curves[name], full[name] = eq, annual_stats(eq)
    full_df = pd.DataFrame(full)

    # 2. competition-shaped rolling windows
    roll = {}
    for pname, (a, b) in PERIODS.items():
        for name in ("Strategy", "BTC buy & hold"):
            c, w = slice_period(closes, variants[name], a, b)
            res = rolling_windows(c, w, ex, rp if name == "Strategy" else None, step_days=args.step_days)
            roll[f"{name} ({pname})"] = summarise(res)
            if name == "Strategy":
                res.to_csv(os.path.join(OUT_DIR, f"windows_{pname.split()[0]}.csv"))
    roll_df = pd.DataFrame(roll)

    pd.set_option("display.float_format", lambda x: f"{x:,.3f}")
    pd.set_option("display.width", 200)
    print("\n=== Full history (continuous, from 2023-03-01) ===\n", full_df)
    print("\n=== 14-day windows, fresh $100k each ===\n", roll_df)
    with open(os.path.join(OUT_DIR, "summary.md"), "w") as f:
        f.write("## Full history\n\n" + full_df.to_markdown(floatfmt=".3f") + "\n\n")
        f.write("## Rolling 14-day windows\n\n" + roll_df.to_markdown(floatfmt=".3f") + "\n")
    pd.DataFrame(curves).resample("1D").last().to_csv(os.path.join(OUT_DIR, "equity_curves.csv"))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 5))
        for name, eq in curves.items():
            ax.plot(eq.index, eq / eq.iloc[0], label=name, lw=1.6 if name == "Strategy" else 1.0)
        ax.set_yscale("log")
        ax.axvline(pd.Timestamp("2025-01-01", tz="UTC"), color="grey", ls="--", lw=0.8)
        ax.text(pd.Timestamp("2025-01-10", tz="UTC"), ax.get_ylim()[1] * 0.9, "out-of-sample →", color="grey")
        ax.set_title("Growth of $1 (log scale), fees + slippage included")
        ax.legend()
        fig.tight_layout()
        fig.savefig(os.path.join(OUT_DIR, "equity_curve.png"), dpi=120)
    except ImportError:
        pass


if __name__ == "__main__":
    main()
