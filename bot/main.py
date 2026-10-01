"""Entry point: autonomous trading loop.

Schedule (UTC):
  HH:01                 full cycle  - refresh data, recompute signals, trade any
                                       coin whose weight is outside the band
                                       (targets move when the bars opening at
                                       00/08/16 UTC close, i.e. the 01:01,
                                       09:01 and 17:01 cycles; drift can trigger
                                       trades in between, exactly as in the
                                       backtest engine)
  HH:16 / HH:31 / HH:46 risk check  - mark to market; if the drawdown brake
                                       has tightened, scale positions down now
                                       instead of waiting for the next hour.
The loop never exits on errors: each cycle is wrapped, logged and retried
at the next slot.

Run:  python -m bot.main            # live, account chosen by BOT_ACCOUNT in .env
      python -m bot.main --paper    # simulated wallet on live prices, no keys needed
      python -m bot.main --paper --once   # one full cycle then exit (smoke test)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from bot.config import ROOT, load_config, load_credentials
from bot.execution import Executor, load_pairs
from bot.market_data import MarketData
from bot.paper import PaperClient
from bot.risk import drawdown_multiplier
from bot.roostoo_client import RoostooClient
from bot.strategy import compute
from bot.trade_log import TradeLogger, setup_logging

log = logging.getLogger("bot")


class State:
    """Small persistent state so a restart keeps the drawdown peak."""

    def __init__(self, path: str):
        self.path = path
        self.data = {"peak_nav": None, "last_targets": {}, "last_mult": 1.0}
        if os.path.exists(path):
            with open(path) as f:
                self.data.update(json.load(f))

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


class Bot:
    def __init__(self, paper: bool = False):
        self.cfg = load_config()
        os.chdir(ROOT)
        if paper:
            self.cfg.bot.log_dir = os.path.join(self.cfg.bot.log_dir, "paper")
            self.cfg.bot.state_file = os.path.join(self.cfg.bot.log_dir, "state.json")
            self.cfg.execution.limit_wait_seconds = 5
        setup_logging(self.cfg.bot.log_dir)
        if paper:
            if os.path.exists(self.cfg.bot.state_file):
                os.remove(self.cfg.bot.state_file)  # paper wallet restarts at $100k
            self.client = PaperClient(self.cfg.bot.base_url)
            log.info("starting bot | PAPER mode | commit=%s", git_commit())
        else:
            account, key, secret = load_credentials()
            log.info("starting bot | account=%s | commit=%s", account, git_commit())
            self.client = RoostooClient(key, secret, self.cfg.bot.base_url)
        self.client.sync_clock()
        self.pairs = load_pairs(self.client)
        sp = self.cfg.strategy
        missing = [c for c in sp.candidates if c not in self.pairs]
        if missing:
            log.warning("candidates not tradable on Roostoo, dropped: %s", missing)
        sp.candidates = [c for c in sp.candidates if c in self.pairs]
        self.data = MarketData(sp.candidates, self.cfg.bot.history_hours)
        self.trade_log = TradeLogger(self.cfg.bot.log_dir)
        self.executor = Executor(self.client, self.pairs, self.cfg.execution, self.trade_log,
                                 shorts_enabled=sp.allow_shorts)
        self.state = State(self.cfg.bot.state_file)

    # ---------- cycles ----------
    def _mark(self, event: str, breadth: float = float("nan")):
        snap = self.executor.snapshot()
        if snap is None:
            return None, 1.0
        nav = snap.nav
        peak = max(self.state.data["peak_nav"] or nav, nav)
        self.state.data["peak_nav"] = peak
        mult = drawdown_multiplier(nav, peak, self.cfg.risk)
        w = snap.weights()
        self.trade_log.equity_row(nav=round(nav, 2), usd=round(snap.usd_free + snap.usd_lock, 2), gross=round(sum(abs(v) for v in w.values()), 4),
                                  peak=round(peak, 2), drawdown=round(1 - nav / peak, 5), dd_mult=round(mult, 3),
                                  breadth=round(breadth, 3), n_positions=sum(abs(v) > 0.005 for v in w.values()),
                                  event=event)
        return snap, mult

    def full_cycle(self) -> None:
        self.client.cancel_order()  # hygiene: no stray pending order should survive a cycle
        tick = self.client.ticker()
        last = {p.split("/")[0]: float(d["LastPrice"]) for p, d in (tick.get("Data") or {}).items()}
        stale = self.data.refresh(last)
        sp = self.cfg.strategy
        sp.allow_shorts = sp.allow_shorts and self.executor.shorts_enabled
        weights, diag = compute(self.data.closes, self.data.quote_volume, sp)
        targets = {c: float(v) for c, v in weights.iloc[-1].items() if abs(v) > 1e-4}
        scores = diag["trend_score"].iloc[-1].dropna()
        breadth = float((scores > 0).mean()) if len(scores) else float("nan")
        mom = diag["mom_rel"].iloc[-1].dropna()
        self.trade_log.signal_row(breadth, targets, {**{f"trend:{c}": v for c, v in scores.items()},
                                                     **{f"mom:{c}": v for c, v in mom.items()}})

        snap, mult = self._mark("signal", breadth)
        if snap is None:
            return
        final = {c: w * mult for c, w in targets.items()}
        log.info("targets (dd_mult=%.2f, breadth=%.2f): %s", mult, breadth,
                 {c: round(w, 3) for c, w in final.items()} or "all cash")
        top = ", ".join(f"{c}:w={w:+.3f}/trend={scores.get(c, float('nan')):+.2f}/mom={mom.get(c, float('nan')):+.3f}"
                        for c, w in sorted(targets.items(), key=lambda kv: -abs(kv[1])))
        reason = f"rebalance trend_breadth={breadth:.2f} dd_mult={mult:.2f} [{top}]"
        self.executor.rebalance(final, reason, hold=stale)
        self.state.data.update(last_targets=targets, last_mult=mult)
        self.state.save()
        self._mark("post-trade", breadth)

    def risk_check(self) -> None:
        snap, mult = self._mark("risk-check")
        if snap is None:
            return
        if mult < self.state.data["last_mult"] - 0.1:
            final = {c: w * mult for c, w in self.state.data["last_targets"].items()}
            log.warning("drawdown overlay tightened %.2f -> %.2f, de-risking", self.state.data["last_mult"], mult)
            self.executor.rebalance(final, f"drawdown-derisk dd_mult={mult:.2f}")
            self.state.data["last_mult"] = mult
        self.state.save()

    # ---------- scheduler ----------
    def run(self) -> None:
        minute = self.cfg.bot.cycle_minute
        self._safe(self.full_cycle)
        while True:
            now = datetime.now(timezone.utc)
            slots = [now.replace(minute=(minute + 15 * k) % 60, second=5, microsecond=0) for k in range(4)]
            nxt = min((s if s > now else s + timedelta(hours=1)) for s in slots)
            time.sleep(max((nxt - datetime.now(timezone.utc)).total_seconds(), 1))
            if nxt.minute == minute:
                self._safe(self.full_cycle)
            else:
                self._safe(self.risk_check)

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception:
            log.exception("cycle %s failed; will retry next slot", fn.__name__)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="store_true", help="simulated wallet, live prices, no API keys")
    ap.add_argument("--once", action="store_true", help="run one full cycle and exit")
    args = ap.parse_args()
    if args.once:
        Bot(paper=args.paper).full_cycle()
        return
    while True:  # last-resort guard: rebuild the bot if initialisation itself fails
        try:
            Bot(paper=args.paper).run()
        except Exception:
            logging.getLogger("bot").exception("fatal error, restarting in 60s")
            time.sleep(60)


if __name__ == "__main__":
    main()
