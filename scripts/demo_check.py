"""
Check a real MT5 connector connected to a DEMO account.

Read-only by default. Pass --trade to place, modify and close one 0.01 lot
position on the demo account. Refuses to trade unless the account reports demo.

The connector's token is read from MT5_API_TOKEN, or pass --token.

    backend/.venv/bin/python scripts/demo_check.py
    backend/.venv/bin/python scripts/demo_check.py --trade
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.connector_guard import check_connector_url  # noqa: E402

results = []


def stage(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5001")
    parser.add_argument("--symbol", default="XAUUSD", help="broker symbol name, for example XAUUSD or GOLD")
    parser.add_argument("--trade", action="store_true", help="place, modify and close one 0.01 lot demo trade")
    parser.add_argument("--token", default=os.getenv("MT5_API_TOKEN", ""),
                        help="connector API token; defaults to MT5_API_TOKEN")
    args = parser.parse_args()

    check_connector_url(args.url)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    c = httpx.Client(base_url=args.url, timeout=30, headers=headers)

    try:
        health = c.get("/health")
    except httpx.HTTPError as exc:
        stage("connector reachable", False, f"{exc}. Is connector.py running on Windows, and is mirrored networking on?")
        return 1
    if health.status_code == 401:
        stage("connector reachable", False, "401, the token is missing or wrong. Set MT5_API_TOKEN or pass --token.")
        return 1
    if health.status_code == 503:
        stage("connector reachable", False, "503, the connector has no token configured. Set MT5_API_TOKEN on Windows too.")
        return 1
    stage("connector reachable", health.status_code == 200, health.text)

    init = c.post("/initialize")
    if not stage("terminal initialized", init.status_code == 200, init.text[:200]):
        return 1
    acc = init.json()["account"]
    print(f"      account {acc['login']} on {acc['server']}  balance {acc['balance']}  type {acc.get('trade_mode')}")
    is_demo = acc.get("trade_mode") == "demo"
    stage("account is demo", is_demo)

    sym = c.get(f"/symbol/{args.symbol}")
    if not stage(f"quote for {args.symbol}", sym.status_code == 200, sym.text[:200]):
        return 1
    q = sym.json()
    print(f"      bid {q['bid']}  ask {q['ask']}  digits {q['digits']}")

    bars = c.get(f"/data/latest/{args.symbol}", params={"timeframe": "15m", "count": 20})
    ok = bars.status_code == 200 and bars.json().get("count") == 20
    stage("20 candles of 15m data", ok, "" if ok else bars.text[:200])
    if ok:
        for row in bars.json()["data"][-3:]:
            t = datetime.fromtimestamp(row["time"], timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"      {t}  O {row['open']}  H {row['high']}  L {row['low']}  C {row['close']}")

    if not args.trade:
        print("\nRead-only check finished. Run again with --trade to test orders on the demo account.")
        return 0 if all(results) else 1

    if not is_demo:
        stage("trade test", False, "refused, account is not demo")
        return 1

    d = q["digits"]
    sl, tp = round(q["bid"] * 0.995, d), round(q["ask"] * 1.005, d)
    order = c.post("/order", json={"symbol": args.symbol, "action": "BUY", "volume": 0.01, "sl": sl, "tp": tp,
                                   "comment": "[DEMO_CHECK]"})
    if not stage("place 0.01 BUY with stop and target", order.status_code == 200, order.text[:300]):
        print("      If the error mentions AutoTrading, enable Algo Trading in the MT5 toolbar.")
        return 1
    ticket = order.json()["ticket"]
    body = order.json()
    print(f"      ticket {ticket}  filled {body['price']}  quoted {body.get('requested_price')}  "
          f"sl {body['sl']}  tp {body['tp']}")

    time.sleep(1)
    pos = c.get("/positions").json()
    stage("position visible", any(p["ticket"] == ticket for p in pos.get("positions", [])))

    new_sl = round(q["bid"] * 0.994, d)
    mod = c.post("/modify", json={"ticket": ticket, "sl": new_sl, "tp": tp})
    stage("modify stop loss", mod.status_code == 200, mod.text[:200])

    close = c.post("/close", json={"ticket": ticket})
    stage("close position", close.status_code == 200, close.text[:200])
    if close.status_code == 200:
        cb = close.json()
        print(f"      filled {cb['close_price']}  quoted {cb.get('requested_price')}")

    time.sleep(1)
    deals = [x for x in c.get("/history", params={"hours": 24}).json().get("deals", []) if x["position_id"] == ticket]
    entries = [x["entry"] for x in deals]
    stage("history shows OPEN and CLOSE", "OPEN" in entries and "CLOSE" in entries, str(entries))
    for x in deals:
        print(f"      {x['entry']:<5} {x['time']}  price {x['price']}  profit {x['profit']}")

    print("\nAll stages passed." if all(results) else "\nSome stages failed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
