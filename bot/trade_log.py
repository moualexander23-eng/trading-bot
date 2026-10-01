"""Audit trail: every order (with the signal that caused it), every equity
snapshot and every signal vector is appended to CSV files in logs/."""
from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for handler in (logging.StreamHandler(),
                    RotatingFileHandler(os.path.join(log_dir, "bot.log"), maxBytes=20_000_000, backupCount=5)):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    api = logging.getLogger("api")
    api.propagate = False
    h = RotatingFileHandler(os.path.join(log_dir, "api.log"), maxBytes=20_000_000, backupCount=5)
    h.setFormatter(fmt)
    api.addHandler(h)


class CsvAppender:
    def __init__(self, path: str, fields: list[str]):
        self.path, self.fields = path, fields
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.DictWriter(f, fields).writeheader()

    def write(self, row: dict) -> None:
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, self.fields, extrasaction="ignore").writerow(row)


class TradeLogger:
    def __init__(self, log_dir: str):
        os.makedirs(log_dir, exist_ok=True)
        self.orders = CsvAppender(os.path.join(log_dir, "trades.csv"), [
            "time", "pair", "side", "type", "qty", "limit_price", "notional_est", "target_w", "current_w",
            "success", "order_id", "status", "filled_qty", "avg_price", "commission", "err", "reason"])
        self.equity = CsvAppender(os.path.join(log_dir, "equity.csv"), [
            "time", "nav", "usd", "gross", "peak", "drawdown", "dd_mult", "breadth", "n_positions", "event"])
        self.fills = CsvAppender(os.path.join(log_dir, "fills.csv"), [
            "time", "order_id", "pair", "side", "status", "role", "filled_qty", "avg_price", "notional",
            "commission", "fee_pct"])
        self.signals = CsvAppender(os.path.join(log_dir, "signals.csv"), [
            "time", "breadth", "targets", "scores"])
        self.log = logging.getLogger("trades")

    def order(self, o: dict, order_type: str, price: str | None, resp: dict, reason: str) -> None:
        d = resp.get("OrderDetail") or {}
        if not d and o["side"].startswith("SHORT"):  # /v6 short endpoints use a flat schema
            d = {"OrderID": resp.get("ID", ""), "Status": resp.get("Status") or ("CLOSED" if resp.get("Success") else ""),
                 "FilledQuantity": resp.get("ShortQty", resp.get("ClosedQty", "")),
                 "FilledAverPrice": resp.get("EntryPrice", resp.get("ClosePrice", "")),
                 "CommissionChargeValue": resp.get("OpenFee", resp.get("CloseFee", ""))}
        row = {
            "time": utcnow(), "pair": f"{o['coin']}/USD", "side": o["side"], "type": order_type,
            "qty": o["qty"], "limit_price": price or "", "notional_est": round(o["notional"], 2),
            "target_w": round(o["target"], 4), "current_w": round(o["current"], 4),
            "success": resp.get("Success"), "order_id": d.get("OrderID", ""), "status": d.get("Status", ""),
            "filled_qty": d.get("FilledQuantity", ""), "avg_price": d.get("FilledAverPrice", ""),
            "commission": d.get("CommissionChargeValue", ""), "err": resp.get("ErrMsg", ""), "reason": reason,
        }
        self.orders.write(row)
        self.log.info("%s %s %s %s @%s -> %s %s %s", order_type, o["side"], o["qty"], row["pair"],
                      price or "mkt", row["success"], row["status"], row["err"])

    def fill(self, coin: str, side: str, resp: dict) -> None:
        """Executed (or finally cancelled) order with its actual price, fee and maker/taker role."""
        d = resp.get("OrderDetail")
        if d is None:  # /v6 short endpoints
            d = {"OrderID": resp.get("ID", ""), "Status": resp.get("Status") or "CLOSED", "Role": "TAKER",
                 "FilledQuantity": resp.get("ShortQty", resp.get("ClosedQty", 0)),
                 "FilledAverPrice": resp.get("EntryPrice", resp.get("ClosePrice", 0)),
                 "CommissionChargeValue": resp.get("OpenFee", resp.get("CloseFee", 0))}
        qty, px = float(d.get("FilledQuantity") or 0), float(d.get("FilledAverPrice") or 0)
        fee = float(d.get("CommissionChargeValue") or 0)
        notional = qty * px
        self.fills.write({"time": utcnow(), "order_id": d.get("OrderID", ""), "pair": f"{coin}/USD", "side": side,
                          "status": d.get("Status", ""), "role": d.get("Role", ""), "filled_qty": qty,
                          "avg_price": px, "notional": round(notional, 2), "commission": round(fee, 6),
                          "fee_pct": round(fee / notional * 100, 4) if notional else ""})

    def equity_row(self, **kw) -> None:
        self.equity.write({"time": utcnow(), **kw})

    def signal_row(self, breadth: float, targets: dict, scores: dict) -> None:
        self.signals.write({"time": utcnow(), "breadth": round(breadth, 4),
                            "targets": json.dumps({k: round(v, 4) for k, v in targets.items()}),
                            "scores": json.dumps({k: round(v, 3) for k, v in scores.items()})})
