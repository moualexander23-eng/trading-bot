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
    print("\nAll four calls should show success=True. Check logs/api.log if not.")


if __name__ == "__main__":
    main()
