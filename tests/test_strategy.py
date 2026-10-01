import numpy as np
import pandas as pd
import pytest

from bot.risk import RiskParams, drawdown_multiplier
from bot.strategy import StrategyParams, compute


@pytest.fixture(scope="module")
def market():
    rng = np.random.default_rng(0)
    idx = pd.date_range("2024-01-01", periods=1500, freq="1h", tz="UTC")
    coins = [f"C{i}" for i in range(30)]
    drift = rng.normal(0, 0.0004, len(coins))
    rets = rng.normal(drift, 0.01, (len(idx), len(coins)))
    closes = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=coins)
    qv = pd.DataFrame(rng.uniform(1e6, 1e8, closes.shape), index=idx, columns=coins)
    return closes, qv


def params(**kw):
    return StrategyParams(min_history=800, **kw)


def test_no_lookahead(market):
    """Weights at time t must not change when data after t is altered."""
    closes, qv = market
    w_full, _ = compute(closes, qv, params())
    cut = 1200
    shocked = closes.copy()
    shocked.iloc[cut + 1:] *= 3.0
    w_shocked, _ = compute(shocked, qv, params())
    pd.testing.assert_frame_equal(w_full.iloc[: cut + 1], w_shocked.iloc[: cut + 1])


def test_caps_respected(market):
    closes, qv = market
    p = params(max_weight=0.1, max_gross=0.8)
    w, _ = compute(closes, qv, p)
    assert w.abs().max().max() <= 0.1 + 1e-12
    assert w.abs().sum(axis=1).max() <= 0.8 + 1e-9


def test_momentum_sleeve_is_market_neutral(market):
    closes, qv = market
    _, diag = compute(closes, qv, params(mom_gross=0.6))
    net = diag["w_mom"].sum(axis=1).iloc[900:]
    gross = diag["w_mom"].abs().sum(axis=1).iloc[900:]
    assert net.abs().max() < 1e-9
    assert np.allclose(gross[gross > 0], 0.6)


def test_long_only_when_shorts_disabled(market):
    closes, qv = market
    w, _ = compute(closes, qv, params(allow_shorts=False))
    assert (w >= 0).all().all()


def test_targets_only_move_at_tranche_hours(market):
    closes, qv = market
    w, _ = compute(closes, qv, params(rebalance_every=24, tranches=3, rebalance_hour=0))
    changed = w.diff().abs().sum(axis=1) > 0
    hours = set(w.index[changed].hour)
    assert hours <= {0, 8, 16}


def test_drawdown_multiplier():
    p = RiskParams(dd_soft=0.05, dd_hard=0.15, dd_floor=0.2)
    assert drawdown_multiplier(100, 100, p) == 1.0
    assert drawdown_multiplier(96, 100, p) == 1.0
    assert drawdown_multiplier(90, 100, p) == pytest.approx(0.6)
    assert drawdown_multiplier(80, 100, p) == 0.2
