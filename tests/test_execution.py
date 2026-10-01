import pytest

from bot.config import ExecutionParams
from bot.execution import Executor, PairInfo, ShortPos, Snapshot
from bot.paper import PaperClient


class NullLog:
    def order(self, *a, **k):
        pass


PAIRS = {c: PairInfo(c, f"{c}/USD", 2, 3, 1.0) for c in ("BTC", "ETH", "SOL")}


def snap(usd=100_000.0, holdings=None, shorts=None, usd_lock=0.0):
    px = {"BTC": 50_000.0, "ETH": 2_000.0, "SOL": 100.0}
    holdings = holdings or {}
    return Snapshot(usd_free=usd, usd_lock=usd_lock, holdings=holdings, free=dict(holdings), shorts=shorts or {},
                    bid=dict(px), ask=dict(px), last=dict(px))


def executor(**kw):
    return Executor(client=None, pairs=PAIRS, params=ExecutionParams(**kw), trade_log=NullLog())


def test_nav_counts_longs_and_shorts_once():
    s = snap(usd=50_000, holdings={"ETH": 10}, shorts={"SOL": ShortPos(qty=100, collateral=10_000, upnl=500)})
    assert s.nav == pytest.approx(50_000 + 20_000 + 10_500)
    # same account if the exchange reports short collateral inside USD 'Lock'
    s2 = snap(usd=50_000, holdings={"ETH": 10}, usd_lock=10_000,
              shorts={"SOL": ShortPos(qty=100, collateral=10_000, upnl=500)})
    assert s2.nav == pytest.approx(s.nav)
    assert s.weights()["SOL"] == pytest.approx(-10_000 / s.nav)


def test_plan_orders_release_cash_first_and_respect_band():
    s = snap(usd=60_000, holdings={"ETH": 20})          # ETH weight = 0.4
    orders = executor(rebalance_band=0.02).plan(s, {"BTC": 0.3, "ETH": 0.1, "SOL": 0.01}, hold=set())
    sides = [(o["coin"], o["side"]) for o in orders]
    assert sides == [("ETH", "SELL"), ("BTC", "BUY")]     # SOL change < band is skipped
    assert orders[0]["qty"] == "15.000"                    # 0.3 * 100k / 2000
    assert orders[1]["qty"] == "0.600"


def test_plan_full_exit_ignores_band():
    s = snap(usd=99_000, holdings={"SOL": 10})           # 1% weight, below band
    orders = executor(rebalance_band=0.02, min_order_usd=50).plan(s, {}, hold=set())
    assert [(o["side"], o["qty"]) for o in orders] == [("SELL", "10.000")]


def test_plan_flip_long_to_short():
    s = snap(usd=80_000, holdings={"ETH": 10})           # +20% ETH
    orders = executor().plan(s, {"ETH": -0.1}, hold=set())
    assert [o["side"] for o in orders] == ["SELL", "SHORT_OPEN"]
    assert orders[1]["notional"] == pytest.approx(10_000)


def test_plan_reduces_short():
    s = snap(usd=90_000, shorts={"SOL": ShortPos(qty=100, collateral=10_000, upnl=0)})
    orders = executor().plan(s, {"SOL": -0.04}, hold=set())
    assert [(o["side"], o["qty"]) for o in orders] == [("SHORT_CLOSE", "60.000")]


def test_held_coins_are_not_traded():
    s = snap(usd=100_000)
    assert executor().plan(s, {"BTC": 0.5}, hold={"BTC"}) == []


def test_paper_client_round_trip_costs_fees():
    c = PaperClient("https://example.invalid")
    c._tick = {"BTC/USD": {"MaxBid": 50_000.0, "MinAsk": 50_000.0, "LastPrice": 50_000.0}}
    c._tick_time = 1e18
    assert c.place_order("BTC/USD", "BUY", "1", "MARKET")["Success"]
    assert c.place_order("BTC/USD", "SELL", "1", "MARKET")["Success"]
    assert c.usd == pytest.approx(100_000 - 2 * 50.0)
    assert c.short_open("BTC/USD", "10000")["Success"]
    c._tick["BTC/USD"].update(MaxBid=45_000.0, MinAsk=45_000.0)
    r = c.short_close("BTC/USD")
    assert r["RealizedPNL"] == pytest.approx(1_000.0)
