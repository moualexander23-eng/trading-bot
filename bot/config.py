"""Configuration: strategy/risk/execution parameters from config/settings.yaml,
API credentials from environment variables (loaded from .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

from bot.risk import RiskParams
from bot.strategy import StrategyParams

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@dataclass
class ExecutionParams:
    rebalance_band: float = 0.02      # trade a coin only if its weight is off by more than this
    min_order_usd: float = 50.0
    use_limit_orders: bool = True     # try maker (0.05%) first, fall back to market (0.1%)
    limit_wait_seconds: int = 120
    max_orders_per_cycle: int = 12


@dataclass
class BotParams:
    cycle_minute: int = 1             # run each hour at HH:01 (after the hourly bar closes)
    history_hours: int = 1000         # hourly bars fetched for signal computation (Binance max per call)
    state_file: str = "logs/state.json"
    log_dir: str = "logs"
    base_url: str = "https://mock-api.roostoo.com"


@dataclass
class Config:
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskParams = field(default_factory=RiskParams)
    execution: ExecutionParams = field(default_factory=ExecutionParams)
    bot: BotParams = field(default_factory=BotParams)
    backtest_fee: float = 0.001
    backtest_slippage: float = 0.0002


def load_config(path: str = "config/settings.yaml") -> Config:
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    s = raw.get("strategy", {})
    if "trend_lookbacks" in s:
        s["trend_lookbacks"] = tuple(s["trend_lookbacks"])
    return Config(
        strategy=StrategyParams(**s),
        risk=RiskParams(**raw.get("risk", {})),
        execution=ExecutionParams(**raw.get("execution", {})),
        bot=BotParams(**raw.get("bot", {})),
        **raw.get("backtest", {}),
    )


def load_credentials() -> tuple[str, str, str]:
    """Return (account, api_key, secret). BOT_ACCOUNT selects 'test' or 'competition'."""
    load_dotenv(os.path.join(ROOT, ".env"))
    account = os.getenv("BOT_ACCOUNT", "test").strip().lower()
    prefix = {"test": "ROOSTOO_TEST", "competition": "ROOSTOO_COMP"}.get(account)
    if prefix is None:
        raise ValueError("BOT_ACCOUNT must be 'test' or 'competition'")
    key, secret = os.getenv(f"{prefix}_API_KEY", ""), os.getenv(f"{prefix}_SECRET_KEY", "")
    if not key or not secret:
        raise ValueError(f"Missing {prefix}_API_KEY / {prefix}_SECRET_KEY in .env")
    return account, key, secret
