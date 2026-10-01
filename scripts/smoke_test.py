"""End-to-end order test on the TESTING account only (refuses the competition
account, since manual API calls there would break the competition rules).

Buys ~$25 of BTC at market, sells it back, then rests a far-away limit order
and cancels it - exercising every order path the bot uses.

    python -m scripts.smoke_test
"""
from __future__ import annotations

import sys
import time

from bot.config import load_config, load_credentials
from bot.roostoo_client import RoostooClient, format_decimal


def show(label: str, resp: dict) -> dict:
    d = resp.get("OrderDetail", {})
    print(f"{label:28s} success={resp.get('Success')} status={d.get('Status')} "
          f"filled={d.get('FilledQuantity')} @ {d.get('FilledAverPrice')} fee={d.get('CommissionChargeValue')} "
          f"err={resp.get('ErrMsg') or '-'}")
    return resp


def main() -> None:
    account, key, secret = load_credentials()
    if account != "test":
        sys.exit("Refusing to run: BOT_ACCOUNT must be 'test'. Never place manual orders on the competition account.")
    cfg = load_config()
    c = RoostooClient(key, secret, cfg.bot.base_url)
    c.sync_clock()
    info = c.exchange_info()["TradePairs"]["BTC/USD"]
    t = c.ticker("BTC/USD")["Data"]["BTC/USD"]
    qty = format_decimal(25 / t["MinAsk"], int(info["AmountPrecision"]))

    show("market BUY", c.place_order("BTC/USD", "BUY", qty, "MARKET"))
    time.sleep(2)
    show("market SELL", c.place_order("BTC/USD", "SELL", qty, "MARKET"))
    far = format_decimal(t["MaxBid"] * 0.8, int(info["PricePrecision"]))
    r = show("limit BUY 20% below market", c.place_order("BTC/USD", "BUY", qty, "LIMIT", far))
    time.sleep(2)
    print("pending count             ", c.pending_count())
    oid = r.get("OrderDetail", {}).get("OrderID")
    print("cancel                    ", c.cancel_order(order_id=oid) if oid else "no order id")

    # Short round trip: is shorting allowed, and how is collateral reported in the wallet?
    def usd():
        b = c.balance()
        return (b.get("Wallet") or b.get("SpotWallet") or {}).get("USD")
    print("\nUSD wallet before short   ", usd())
    so = c.short_open("BTC/USD", "25")
    print("short open $25 collateral ", so)
    if not so.get("Success"):
        print("\nShorts rejected on this account -> the bot will automatically run long-only.")
        return
    time.sleep(2)
    print("USD wallet while short    ", usd())
    print("short positions           ", c.short_positions())
    print("short close               ", c.short_close("BTC/USD"))
    print("USD wallet after close    ", usd())
    print("\nAll calls should show Success=True. Check logs/api.log if not.")


if __name__ == "__main__":
    main()
