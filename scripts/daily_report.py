"""One-screen health and performance report from the bot's own logs.
Read-only: makes no API calls, so it is safe to run on the competition server.

    .venv/bin/python -m scripts.daily_report
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

BENIGN = ("no order canceled", "no pending order", "no order matched")


def read_csv(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "time" in df:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def tail_errors(path: str, since: datetime) -> list[str]:
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, errors="replace"):
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc)
        bad = " ERROR " in line or ("ok=False" in line and not any(b in line for b in BENIGN))
        if ts >= since and bad:
            out.append(line.strip()[:160])
    return out


def main() -> None:
    log_dir = sys.argv[1] if len(sys.argv) > 1 else "logs"
    eq = read_csv(os.path.join(log_dir, "equity.csv"))
    fills = read_csv(os.path.join(log_dir, "fills.csv"))
    trades = read_csv(os.path.join(log_dir, "trades.csv"))
    sig = read_csv(os.path.join(log_dir, "signals.csv"))
    now = datetime.now(timezone.utc)
    pd.set_option("display.width", 160)

    starts = [l for l in open(os.path.join(log_dir, "bot.log"), errors="replace") if "starting bot" in l] \
        if os.path.exists(os.path.join(log_dir, "bot.log")) else []
    print(f"=== REPORT {now:%Y-%m-%d %H:%M} UTC ===")
    print("last start :", starts[-1].strip()[:120] if starts else "n/a", f"| restarts logged: {len(starts)}")

    if eq.empty:
        print("no equity data yet (account not active or bot never reached the exchange)")
    else:
        last = eq.iloc[-1]
        start_nav = eq["nav"].iloc[0]
        nav = eq["nav"].values
        mdd = float((1 - nav / pd.Series(nav).cummax()).max())
        age_min = (now - last["time"]).total_seconds() / 60
        print(f"last check : {last['time']:%m-%d %H:%M} UTC ({age_min:.0f} min ago){'  <-- STALE?' if age_min > 30 else ''}")
        print(f"NAV        : {last['nav']:,.2f}  return {last['nav'] / start_nav - 1:+.2%}  "
              f"drawdown {last['drawdown']:.2%}  max DD {mdd:.2%}  brake x{last['dd_mult']}")
        print(f"exposure   : gross {last['gross']:.2f}  positions {int(last['n_positions'])}")

        daily = eq.set_index("time")["nav"].resample("1D").last().dropna()
        d = pd.DataFrame({"nav": daily.round(2), "ret_%": (daily.pct_change() * 100).round(2)})
        if not fills.empty:
            f = fills[fills["filled_qty"] > 0].set_index("time")
            d["fills"] = f["notional"].resample("1D").count().reindex(d.index).fillna(0).astype(int)
            d["fees_$"] = f["commission"].resample("1D").sum().reindex(d.index).fillna(0).round(2)
        print("\n--- daily (UTC) ---")
        print(d.to_string())

    if not fills.empty:
        f = fills[fills["filled_qty"] > 0]
        tot = f["notional"].sum()
        maker = f.loc[f["role"] == "MAKER", "notional"].sum()
        print(f"\n--- execution ---\nfilled notional ${tot:,.0f}  maker share {maker / tot:.0%}  "
              f"fees ${f['commission'].sum():,.2f} ({f['commission'].sum() / tot:.3%} of notional)  "
              f"active trading days {f['time'].dt.date.nunique()}")
    if not trades.empty:
        rej = trades[trades["success"].astype(str) != "True"]
        print(f"orders sent {len(trades)}  rejected {len(rej)}"
              + (f"  last reject: {rej.iloc[-1]['pair']} {rej.iloc[-1]['err']}" if len(rej) else ""))

    if not sig.empty:
        t = json.loads(sig.iloc[-1]["targets"])
        top = sorted(t.items(), key=lambda kv: -abs(kv[1]))[:12]
        print("\n--- current targets (NAV weights, - = short) ---")
        print("  ".join(f"{c}:{w:+.3f}" for c, w in top) or "all cash")

    errs = tail_errors(os.path.join(log_dir, "bot.log"), now - timedelta(hours=24)) + \
        tail_errors(os.path.join(log_dir, "api.log"), now - timedelta(hours=24))
    print(f"\n--- errors last 24h: {len(errs)} ---")
    for e in errs[-5:]:
        print(" ", e)


if __name__ == "__main__":
    main()
