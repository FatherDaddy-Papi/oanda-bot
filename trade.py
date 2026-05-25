"""
OANDA paper trading CLI.

Usage:
    python trade.py balance
    python trade.py positions
    python trade.py orders
    python trade.py quote EUR_USD
    python trade.py buy EUR_USD 1000          # 1000 units (~0.01 lot)
    python trade.py sell EUR_USD 1000
    python trade.py close EUR_USD             # close position in instrument
    python trade.py close-all
    python trade.py history [N]               # last N closed trades (default 10)

Instrument format: EUR_USD, GBP_USD, USD_JPY, XAU_USD, etc. (underscore, not slash)
Units: positive number. 1 standard lot = 100000 units. 0.01 lot = 1000 units.
"""
import os
import sys
import json
from dotenv import load_dotenv
import oandapyV20
from oandapyV20 import API
from oandapyV20.endpoints import accounts, pricing, orders, positions, trades
from oandapyV20.exceptions import V20Error

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
API_TOKEN = os.getenv("OANDA_API_TOKEN")
ENV = os.getenv("OANDA_ENV", "practice")

if not ACCOUNT_ID or not API_TOKEN:
    print("ERROR: OANDA_ACCOUNT_ID and OANDA_API_TOKEN must be set in .env")
    sys.exit(1)

client = API(access_token=API_TOKEN, environment=ENV)


def fmt_money(x):
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x)


def cmd_balance():
    r = accounts.AccountSummary(accountID=ACCOUNT_ID)
    client.request(r)
    a = r.response["account"]
    print(f"Account:        {a['alias']} ({a['id']})")
    print(f"Currency:       {a['currency']}")
    print(f"Balance:        {fmt_money(a['balance'])}")
    print(f"NAV:            {fmt_money(a['NAV'])}")
    print(f"Unrealized P/L: {fmt_money(a['unrealizedPL'])}")
    print(f"Margin used:    {fmt_money(a['marginUsed'])}")
    print(f"Margin avail:   {fmt_money(a['marginAvailable'])}")
    print(f"Open trades:    {a['openTradeCount']}")
    print(f"Open positions: {a['openPositionCount']}")


def cmd_positions():
    r = positions.OpenPositions(accountID=ACCOUNT_ID)
    client.request(r)
    poss = r.response.get("positions", [])
    if not poss:
        print("(no open positions)")
        return
    print(f"{'INSTRUMENT':<12} {'SIDE':<6} {'UNITS':>10} {'AVG PRICE':>12} {'UNREAL P/L':>14}")
    for p in poss:
        long_units = int(p["long"]["units"])
        short_units = int(p["short"]["units"])
        if long_units > 0:
            side, units, avg = "LONG", long_units, p["long"]["averagePrice"]
        elif short_units < 0:
            side, units, avg = "SHORT", short_units, p["short"]["averagePrice"]
        else:
            continue
        pl = p["unrealizedPL"]
        print(f"{p['instrument']:<12} {side:<6} {units:>10} {avg:>12} {fmt_money(pl):>14}")


def cmd_orders():
    r = orders.OrdersPending(accountID=ACCOUNT_ID)
    client.request(r)
    ords = r.response.get("orders", [])
    if not ords:
        print("(no pending orders)")
        return
    for o in ords:
        print(json.dumps(o, indent=2))


def cmd_quote(instrument):
    r = pricing.PricingInfo(accountID=ACCOUNT_ID, params={"instruments": instrument})
    client.request(r)
    prices = r.response.get("prices", [])
    if not prices:
        print(f"No price for {instrument}")
        return
    p = prices[0]
    bid = p["bids"][0]["price"]
    ask = p["asks"][0]["price"]
    spread = float(ask) - float(bid)
    print(f"{instrument}  bid={bid}  ask={ask}  spread={spread:.5f}  ({p['status']})")


def place_market(instrument, units):
    data = {
        "order": {
            "instrument": instrument,
            "units": str(units),
            "type": "MARKET",
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
        }
    }
    r = orders.OrderCreate(accountID=ACCOUNT_ID, data=data)
    client.request(r)
    fill = r.response.get("orderFillTransaction")
    if fill:
        print(f"FILLED  id={fill['id']}  {fill['instrument']}  units={fill['units']}  price={fill['price']}")
    else:
        print(json.dumps(r.response, indent=2))


def cmd_buy(instrument, units):
    place_market(instrument, abs(int(units)))


def cmd_sell(instrument, units):
    place_market(instrument, -abs(int(units)))


def cmd_close(instrument):
    # First check which side is open so we only close that side
    pr = positions.PositionDetails(accountID=ACCOUNT_ID, instrument=instrument)
    try:
        client.request(pr)
    except V20Error as e:
        print(f"Could not fetch position: {e}")
        return
    pos = pr.response.get("position", {})
    long_units = int(pos.get("long", {}).get("units", "0"))
    short_units = int(pos.get("short", {}).get("units", "0"))
    data = {}
    if long_units > 0:
        data["longUnits"] = "ALL"
    if short_units < 0:
        data["shortUnits"] = "ALL"
    if not data:
        print(f"No open position in {instrument}")
        return
    r = positions.PositionClose(accountID=ACCOUNT_ID, instrument=instrument, data=data)
    try:
        client.request(r)
        resp = r.response
        for key in ("longOrderFillTransaction", "shortOrderFillTransaction"):
            fill = resp.get(key)
            if fill:
                print(f"CLOSED  {fill['instrument']}  units={fill['units']}  price={fill['price']}  realizedPL={fill.get('pl','?')}")
    except V20Error as e:
        print(f"Close failed: {e}")


def cmd_close_all():
    r = positions.OpenPositions(accountID=ACCOUNT_ID)
    client.request(r)
    poss = r.response.get("positions", [])
    if not poss:
        print("(nothing to close)")
        return
    for p in poss:
        cmd_close(p["instrument"])


def cmd_history(n=10):
    r = trades.TradesList(accountID=ACCOUNT_ID, params={"state": "CLOSED", "count": int(n)})
    client.request(r)
    ts = r.response.get("trades", [])
    if not ts:
        print("(no closed trades)")
        return
    print(f"{'ID':<10} {'INSTRUMENT':<12} {'UNITS':>8} {'OPEN':>10} {'CLOSE':>10} {'P/L':>10}")
    for t in ts:
        print(f"{t['id']:<10} {t['instrument']:<12} {t['initialUnits']:>8} {t['price']:>10} {t.get('averageClosePrice','-'):>10} {fmt_money(t.get('realizedPL','0')):>10}")


COMMANDS = {
    "balance": (cmd_balance, 0),
    "positions": (cmd_positions, 0),
    "orders": (cmd_orders, 0),
    "quote": (cmd_quote, 1),
    "buy": (cmd_buy, 2),
    "sell": (cmd_sell, 2),
    "close": (cmd_close, 1),
    "close-all": (cmd_close_all, 0),
    "history": (cmd_history, -1),  # 0 or 1 arg
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
    fn, expected = COMMANDS[cmd]
    if expected >= 0 and len(args) != expected:
        print(f"'{cmd}' takes {expected} arg(s), got {len(args)}")
        sys.exit(1)
    try:
        fn(*args)
    except V20Error as e:
        print(f"OANDA API error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
