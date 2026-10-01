"""Portfolio-level risk overlays shared by the live bot and the backtester.

These are path-dependent (they need realised equity), so they are applied on
top of the strategy's target weights at each decision step.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskParams:
    dd_soft: float = 0.03      # start de-risking once drawdown from peak exceeds this
    dd_hard: float = 0.08      # exposure reaches `dd_floor` at this drawdown
    dd_floor: float = 0.25     # minimum exposure multiplier while in drawdown


def drawdown_multiplier(equity: float, peak: float, p: RiskParams) -> float:
    """Linear de-risking between dd_soft and dd_hard, floored at dd_floor."""
    if peak <= 0:
        return 1.0
    dd = 1.0 - equity / peak
    if dd <= p.dd_soft:
        return 1.0
    if dd >= p.dd_hard:
        return p.dd_floor
    frac = (dd - p.dd_soft) / (p.dd_hard - p.dd_soft)
    return 1.0 - frac * (1.0 - p.dd_floor)


def apply_risk(weights: dict[str, float], multiplier: float) -> dict[str, float]:
    return {k: v * multiplier for k, v in weights.items()}
