"""One-off trade placer with stop loss attached."""
import os, sys, json
from dotenv import load_dotenv
from oandapyV20 import API
from oandapyV20.endpoints import orders, pricing

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
client = API(access_token=os.getenv("OANDA_API_TOKEN"), environment=os.getenv("OANDA_ENV", "practice"))
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")


def get_price(inst):
    r = pricing.PricingInfo(accountID=ACCOUNT_ID, params={"instruments": inst})
    client.request(r)
    p = r.response["prices"][0]
    return float(p["bids"][0]["price"]), float(p["asks"][0]["price"])


def place(inst, units, stop_pips, pip_size=0.0001, tag="claude-manual"):
    bid, ask = get_price(inst)
    if units > 0:
        # long; stop below entry
        ref = ask
        stop_price = round(ref - stop_pips * pip_size, 5 if pip_size < 0.001 else 3)
    else:
        ref = bid
        stop_price = round(ref + stop_pips * pip_size, 5 if pip_size < 0.001 else 3)
    data = {"order": {
        "instrument": inst,
        "units": str(units),
        "type": "MARKET",
        "timeInForce": "FOK",
        "positionFill": "DEFAULT",
        "stopLossOnFill": {"price": f"{stop_price}"},
        "clientExtensions": {"tag": tag, "id": f"{tag}-{inst}-{abs(units)}"},
        "tradeClientExtensions": {"tag": tag},
    }}
    r = orders.OrderCreate(accountID=ACCOUNT_ID, data=data)
    client.request(r)
    return r.response


if __name__ == "__main__":
    # Usage: python place_trade.py INST UNITS STOP_PIPS [pip_size]
    inst = sys.argv[1]
    units = int(sys.argv[2])
    stop_pips = float(sys.argv[3])
    pip = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0001
    resp = place(inst, units, stop_pips, pip)
    fill = resp.get("orderFillTransaction")
    if fill:
        print(f"FILLED  trade={fill.get('tradeOpened',{}).get('tradeID')}  {fill['instrument']}  "
              f"units={fill['units']}  price={fill['price']}")
        print(f"        stop loss set at {resp.get('relatedTransactionIDs')}")
    else:
        print(json.dumps(resp, indent=2)[:500])
