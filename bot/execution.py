"""Turns target weights (negative = short) into orders on Roostoo.

Rebalance algorithm (one call per decision cycle):
  1. Snapshot balances, short positions and tickers -> NAV, current weights.
  2. Diff against targets; skip coins whose weight is within the rebalance
     band (saves fees) unless a position must be fully closed.
  3. Orders that RELEASE cash go first (sell longs, close shorts), then
     orders that USE cash (buy longs, open shorts), each sized to the USD
     actually available.
  4. Maker pass for spot longs: post LIMIT orders at the touch (buy @ bid,
     sell @ ask) so fills pay the 0.05% maker fee instead of 0.1% taker.
     After `limit_wait_seconds`, cancel what's unfilled, re-snapshot, and
     finish the residual with MARKET orders. Shorts always go at market:
     Roostoo charges 0.1% on them regardless of order type.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from bot.config import ExecutionParams
from bot.roostoo_client import RoostooClient, format_decimal
from bot.trade_log import TradeLogger

log = logging.getLogger("execution")

RELEASES_CASH = ("SELL", "SHORT_CLOSE")
SHORTS_FORBIDDEN = "does not allow short"


@dataclass
class PairInfo:
    coin: str
    pair: str
    price_precision: int
    amount_precision: int
    min_notional: float


@dataclass
class ShortPos:
    qty: float
    collateral: float
    upnl: float


@dataclass
class Snapshot:
    usd_free: float
    usd_lock: float
    holdings: dict[str, float]      # coin -> long quantity (free + locked)
    free: dict[str, float]          # coin -> free long quantity
    shorts: dict[str, ShortPos]
    bid: dict[str, float]
    ask: dict[str, float]
    last: dict[str, float]
    _nav: float | None = field(default=None, repr=False)

    def mid(self, coin: str) -> float:
        b, a = self.bid.get(coin, 0.0), self.ask.get(coin, 0.0)
        return (b + a) / 2 if b > 0 and a > 0 else self.last.get(coin, 0.0)

    @property
    def nav(self) -> float:
        """USD + longs at mid + shorts at (collateral + unrealised PnL).

        Short collateral may or may not be reported inside the USD 'Lock'
        balance; we only snapshot when no orders are pending, so any USD lock
        beyond the short collateral is counted, never double-counted."""
        if self._nav is None:
            coll = sum(s.collateral for s in self.shorts.values())
            longs = sum(q * self.mid(c) for c, q in self.holdings.items())
            shorts = sum(s.collateral + s.upnl for s in self.shorts.values())
            self._nav = self.usd_free + max(0.0, self.usd_lock - coll) + longs + shorts
        return self._nav

    def weights(self) -> dict[str, float]:
        nav = self.nav
        if nav <= 0:
            return {}
        w = {c: q * self.mid(c) / nav for c, q in self.holdings.items()}
        for c, s in self.shorts.items():
            w[c] = w.get(c, 0.0) - s.qty * self.ask.get(c, self.mid(c)) / nav
        return w


def load_pairs(client: RoostooClient) -> dict[str, PairInfo]:
    info = client.exchange_info().get("TradePairs", {})
    out = {}
    for pair, v in info.items():
        if v.get("CanTrade"):
            coin = pair.split("/")[0]
            out[coin] = PairInfo(coin, pair, int(v["PricePrecision"]), int(v["AmountPrecision"]),
                                 float(v.get("MiniOrder", 1.0)))
    return out


class Executor:
    def __init__(self, client: RoostooClient, pairs: dict[str, PairInfo], params: ExecutionParams,
                 trade_log: TradeLogger, shorts_enabled: bool = True):
        self.client, self.pairs, self.p, self.trade_log = client, pairs, params, trade_log
        self.shorts_enabled = shorts_enabled

    # ---------- state ----------
    def snapshot(self) -> Snapshot | None:
        bal = self.client.balance()
        tick = self.client.ticker()
        if not bal.get("Success") or not tick.get("Success"):
            log.error("snapshot failed: balance=%s ticker=%s", bal.get("ErrMsg"), tick.get("ErrMsg"))
            return None
        shorts: dict[str, ShortPos] = {}
        if self.shorts_enabled:
            sp = self.client.short_positions()
            if not sp.get("Success"):
                log.error("snapshot failed: short_positions=%s", sp.get("ErrMsg"))
                return None
            for pos in sp.get("Positions") or []:
                coin = pos["Pair"].split("/")[0]
                shorts[coin] = ShortPos(float(pos.get("ShortQty", 0)), float(pos.get("Collateral", 0)),
                                        float(pos.get("UnrealizedPNL", 0)))
        wallet = bal.get("Wallet") or bal.get("SpotWallet") or {}
        usd = wallet.get("USD", {})
        holdings, free = {}, {}
        for coin, v in wallet.items():
            if coin == "USD":
                continue
            qty = float(v.get("Free", 0)) + float(v.get("Lock", 0))
            if qty > 0:
                holdings[coin], free[coin] = qty, float(v.get("Free", 0))
        bid, ask, last = {}, {}, {}
        for pair, d in tick.get("Data", {}).items():
            coin = pair.split("/")[0]
            bid[coin], ask[coin], last[coin] = float(d["MaxBid"]), float(d["MinAsk"]), float(d["LastPrice"])
        return Snapshot(float(usd.get("Free", 0)), float(usd.get("Lock", 0)), holdings, free, shorts,
                        bid, ask, last)

    # ---------- planning ----------
    def _order(self, coin, kind, qty, price, tgt, now, full=False) -> dict | None:
        info = self.pairs[coin]
        qty_str = format_decimal(qty, info.amount_precision)
        notional = float(qty_str) * price
        if not full and (float(qty_str) <= 0 or notional < max(self.p.min_order_usd, info.min_notional)):
            return None
        return {"coin": coin, "side": kind, "qty": qty_str, "notional": notional, "target": tgt,
                "current": now, "full": full}

    def plan(self, snap: Snapshot, targets: dict[str, float], hold: set[str]) -> list[dict]:
        nav, cur = snap.nav, snap.weights()
        orders = []
        for coin in sorted(set(targets) | set(cur)):
            if coin in hold or coin not in self.pairs:
                continue
            price = snap.mid(coin)
            if price <= 0:
                continue
            tgt, now = targets.get(coin, 0.0), cur.get(coin, 0.0)
            if not self.shorts_enabled:
                tgt = max(tgt, 0.0)
            must_close = (tgt == 0 and abs(now) * nav >= self.p.min_order_usd)
            if abs(tgt - now) < self.p.rebalance_band and not must_close:
                continue
            long_qty = snap.holdings.get(coin, 0.0)
            short_qty = snap.shorts[coin].qty if coin in snap.shorts else 0.0
            want_long = max(tgt, 0.0) * nav / price
            want_short = max(-tgt, 0.0) * nav / price

            if long_qty > want_long:
                qty = snap.free.get(coin, 0.0) if want_long == 0 else min(long_qty - want_long, snap.free.get(coin, 0.0))
                o = self._order(coin, "SELL", qty, price, tgt, now)
            elif want_long > long_qty:
                o = self._order(coin, "BUY", want_long - long_qty, price, tgt, now)
            else:
                o = None
            if o:
                orders.append(o)

            if short_qty > want_short:
                full = want_short == 0
                o = self._order(coin, "SHORT_CLOSE", short_qty - want_short, price, tgt, now, full=full)
            elif want_short > short_qty:
                o = self._order(coin, "SHORT_OPEN", want_short - short_qty, price, tgt, now)
            else:
                o = None
            if o:
                orders.append(o)
        orders.sort(key=lambda o: o["side"] not in RELEASES_CASH)
        return orders[: self.p.max_orders_per_cycle]

    # ---------- execution ----------
    def _send(self, o: dict, order_type: str, snap: Snapshot, reason: str) -> dict:
        info = self.pairs[o["coin"]]
        price = None
        if o["side"] == "SHORT_OPEN":
            resp = self.client.short_open(info.pair, format_decimal(o["notional"], 2))
            if SHORTS_FORBIDDEN in str(resp.get("ErrMsg", "")):
                log.error("exchange rejects shorts - switching to long-only")
                self.shorts_enabled = False
        elif o["side"] == "SHORT_CLOSE":
            resp = self.client.short_close(info.pair, None if o["full"] else o["qty"])
        else:
            if order_type == "LIMIT":
                touch = snap.bid[o["coin"]] if o["side"] == "BUY" else snap.ask[o["coin"]]
                price = format_decimal(touch, info.price_precision)
            resp = self.client.place_order(info.pair, o["side"], o["qty"], order_type, price)
        self.trade_log.order(o, order_type if o["side"] in ("BUY", "SELL") else "MARKET", price, resp, reason)
        return resp

    def _fit(self, o: dict, usd_avail: float, snap: Snapshot) -> dict | None:
        """Shrink a cash-using order so it fits in available USD (keeping a fee buffer)."""
        budget = usd_avail * 0.995
        if o["notional"] <= budget:
            return o
        price = snap.mid(o["coin"])
        return self._order(o["coin"], o["side"], budget / price, price, o["target"], o["current"])

    def _pass(self, orders: list[dict], order_type: str, snap: Snapshot, reason: str) -> int:
        usd_avail, sent = snap.usd_free, 0
        for o in orders:
            is_spot = o["side"] in ("BUY", "SELL")
            if order_type == "LIMIT" and not is_spot:
                continue  # shorts are handled in the market pass
            if o["side"] not in RELEASES_CASH:
                o = self._fit(o, usd_avail, snap)
                if o is None:
                    continue
                usd_avail -= o["notional"]
            resp = self._send(o, order_type, snap, reason)
            if resp.get("Success"):
                sent += 1
                if o["side"] in RELEASES_CASH and (order_type == "MARKET" or not is_spot):
                    usd_avail += o["notional"] * 0.997  # proceeds net of fee/slippage
        return sent

    def rebalance(self, targets: dict[str, float], reason: str, hold: set[str] | None = None) -> int:
        hold = hold or set()
        snap = self.snapshot()
        if snap is None:
            return 0
        orders = self.plan(snap, targets, hold)
        if not orders:
            log.info("rebalance: portfolio within band, no orders")
            return 0
        sent = 0
        if self.p.use_limit_orders and any(o["side"] in ("BUY", "SELL") for o in orders):
            sent = self._pass(orders, "LIMIT", snap, reason)
            if sent:
                time.sleep(self.p.limit_wait_seconds)
                self.client.cancel_order()  # cancel every still-pending order
                snap = self.snapshot()
                if snap is None:
                    return sent
                orders = self.plan(snap, targets, hold)
        return sent + self._pass(orders, "MARKET", snap, reason)
