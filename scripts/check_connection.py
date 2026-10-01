"""Read-only connectivity check. Places NO orders, so it is safe on either account.

    python -m scripts.check_connection
"""
from __future__ import annotations

import json
import logging

from bot.config import load_config, load_credentials
from bot.roostoo_client import RoostooClient


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    cfg = load_config()
    account, key, secret = load_credentials()
    c = RoostooClient(key, secret, cfg.bot.base_url)
    print(f"Account selected in .env: {account}")

    print("1. server time      ->", c.server_time())
    c.sync_clock()
    print(f"   clock offset     -> {c.clock_offset_ms} ms (must be well under 60000)")
    info = c.exchange_info()
    pairs = info.get("TradePairs", {})
    print(f"2. exchange info    -> running={info.get('IsRunning')} pairs={len(pairs)} "
          f"initial_wallet={info.get('InitialWallet')}")
    missing = [x for x in cfg.strategy.candidates if f"{x}/USD" not in pairs]
    print(f"   strategy coins not listed: {missing or 'none'}")
    t = c.ticker("BTC/USD")
    print("3. BTC ticker       ->", t.get("Data", {}).get("BTC/USD"), "| success:", t.get("Success"))
    b = c.balance()
    print("4. balance (signed) -> success:", b.get("Success"), "| err:", b.get("ErrMsg") or "-")
    wallet = b.get("Wallet") or b.get("SpotWallet") or {}
    print("   non-zero wallet  ->", json.dumps({k: v for k, v in wallet.items()
                                              if float(v.get("Free", 0)) + float(v.get("Lock", 0)) > 0}))
    print("5. pending orders   ->", c.pending_count())
    print("6. short positions  ->", c.short_positions())
    print("\nIf step 4 shows success: True, your keys and signing work.")


if __name__ == "__main__":
    main()
