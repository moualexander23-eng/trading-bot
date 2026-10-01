"""Paper-trading stand-in for RoostooClient.

Public endpoints (exchange info, tickers) hit the real Roostoo API, so the
bot sees live prices; signed endpoints (balance, orders, shorts) are
simulated locally with Roostoo's fee rules. Lets the full bot run end to end
with no API keys and no risk.

    python -m bot.main --paper
"""
from __future__ import annotations

import itertools
import time

from bot.roostoo_client import RoostooClient

TAKER_FEE = 0.001
SHORT_FEE = 0.001


class PaperClient:
    def __init__(self, base_url: str, usd: float = 100_000.0):
        self.public = RoostooClient("paper", "paper", base_url)
        self.usd = usd
        self.coins: dict[str, float] = {}
        self.shorts: dict[str, dict] = {}       # coin -> {qty, entry, collateral}
        self.pending: dict[int, dict] = {}
        self.cancelled: dict[int, dict] = {}
        self.ids = itertools.count(1)
        self._tick: dict = {}
        self._tick_time = 0.0

    # ---------- public passthrough ----------
    def sync_clock(self) -> None:
        self.public.sync_clock()

    def server_time(self) -> dict:
        return self.public.server_time()

    def exchange_info(self) -> dict:
        return self.public.exchange_info()

    def ticker(self, pair: str | None = None) -> dict:
        if time.time() - self._tick_time > 20:
            t = self.public.ticker()
            if t.get("Success"):
                self._tick, self._tick_time = t["Data"], time.time()
        data = {pair: self._tick[pair]} if pair else self._tick
        return {"Success": bool(data), "ErrMsg": "", "Data": data}

    def _quote(self, pair: str) -> dict:
        self.ticker()
        return self._tick[pair]

    # ---------- simulated account ----------
    def balance(self) -> dict:
        wallet = {"USD": {"Free": self.usd, "Lock": 0.0}}
        wallet.update({c: {"Free": q, "Lock": 0.0} for c, q in self.coins.items() if q > 0})
        return {"Success": True, "ErrMsg": "", "Wallet": wallet}

    def pending_count(self) -> dict:
        return {"Success": True, "TotalPending": len(self.pending), "OrderPairs": {}}

    def place_order(self, pair, side, quantity, order_type="MARKET", price=None) -> dict:
        coin, qty = pair.split("/")[0], float(quantity)
        q = self._quote(pair)
        oid = next(self.ids)
        if order_type == "LIMIT":
            # resting at the touch: never fills instantly in this simulation,
            # so the bot's cancel + market-fallback path is exercised
            self.pending[oid] = {"pair": pair, "side": side, "qty": qty}
            return {"Success": True, "ErrMsg": "", "OrderDetail": {"OrderID": oid, "Status": "PENDING",
                                                                   "Role": "MAKER", "Pair": pair}}
        px = q["MinAsk"] if side == "BUY" else q["MaxBid"]
        value = qty * px
        fee = value * TAKER_FEE
        if side == "BUY":
            if value + fee > self.usd + 1e-9:
                return {"Success": False, "ErrMsg": "insufficient balance"}
            self.usd -= value + fee
            self.coins[coin] = self.coins.get(coin, 0.0) + qty
        else:
            if qty > self.coins.get(coin, 0.0) + 1e-12:
                return {"Success": False, "ErrMsg": "insufficient balance"}
            self.coins[coin] -= qty
            self.usd += value - fee
        return {"Success": True, "ErrMsg": "", "OrderDetail": {
            "OrderID": oid, "Pair": pair, "Status": "FILLED", "Role": "TAKER", "Side": side, "Type": "MARKET",
            "FilledQuantity": qty, "FilledAverPrice": px, "CommissionChargeValue": fee}}

    def query_order(self, order_id=None, **_) -> dict:
        o = self.cancelled.get(int(order_id)) if order_id else None
        if not o:
            return {"Success": False, "ErrMsg": "no order matched"}
        return {"Success": True, "OrderMatched": [{"OrderID": int(order_id), "Pair": o["pair"], "Side": o["side"],
                                                   "Status": "CANCELED", "Role": "MAKER", "FilledQuantity": 0,
                                                   "FilledAverPrice": 0, "CommissionChargeValue": 0}]}

    def cancel_order(self, order_id=None, pair=None) -> dict:
        ids = list(self.pending) if order_id is None else [int(order_id)]
        for i in ids:
            if i in self.pending:
                self.cancelled[i] = self.pending.pop(i)
        return {"Success": True, "ErrMsg": "", "CanceledList": ids}

    def short_open(self, pair: str, collateral: str) -> dict:
        coin, coll = pair.split("/")[0], float(collateral)
        bid = self._quote(pair)["MaxBid"]
        fee = coll * SHORT_FEE
        if coll + fee > self.usd + 1e-9:
            return {"Success": False, "ErrMsg": "insufficient balance"}
        self.usd -= coll + fee
        qty = coll / bid
        s = self.shorts.get(coin)
        if s:
            total = s["qty"] + qty
            s["entry"] = (s["entry"] * s["qty"] + bid * qty) / total
            s["qty"], s["collateral"] = total, s["collateral"] + coll
        else:
            self.shorts[coin] = {"qty": qty, "entry": bid, "collateral": coll}
        return {"Success": True, "Pair": pair, "OrderType": "MARKET", "EntryPrice": bid, "ShortQty": qty,
                "Collateral": coll, "OpenFee": fee, "Status": "OPEN"}

    def short_close(self, pair: str, close_qty: str | None = None) -> dict:
        coin = pair.split("/")[0]
        s = self.shorts.get(coin)
        if not s:
            return {"Success": False, "ErrMsg": "no open short position for this pair"}
        ask = self._quote(pair)["MinAsk"]
        qty = min(float(close_qty), s["qty"]) if close_qty else s["qty"]
        frac = qty / s["qty"]
        coll = s["collateral"] * frac
        pnl = max(qty * (s["entry"] - ask), -coll)
        fee = qty * ask * SHORT_FEE
        self.usd += coll + pnl - fee
        s["qty"] -= qty
        s["collateral"] -= coll
        full = s["qty"] <= 1e-12
        if full:
            del self.shorts[coin]
        return {"Success": True, "ClosePrice": ask, "RealizedPNL": pnl, "CloseFee": fee,
                "ReturnAmount": coll + pnl - fee, "ClosedQty": qty, "FullyClosed": full}

    def short_positions(self) -> dict:
        out = []
        for coin, s in self.shorts.items():
            ask = self._quote(f"{coin}/USD")["MinAsk"]
            upnl = s["qty"] * (s["entry"] - ask)
            out.append({"Pair": f"{coin}/USD", "EntryPrice": s["entry"], "ShortQty": s["qty"],
                        "Collateral": s["collateral"], "CurrentPrice": ask, "UnrealizedPNL": upnl,
                        "PositionValue": s["collateral"] + upnl, "PositionStatus": "OPEN"})
        return {"Success": True, "Positions": out}
