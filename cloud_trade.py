"""
Manual trade placer for GitHub Actions workflow_dispatch.

Reads from env vars (set by the workflow inputs) and places a market order
with broker-side stop. Tag: 'cloud-manual' (separate from bot tags).

Required env: OANDA_ACCOUNT_ID, OANDA_API_TOKEN
Trade inputs (env or CLI):
    TRADE_INSTRUMENT  e.g. EUR_USD
    TRADE_SIDE        buy | sell
    TRADE_UNITS       positive integer
    TRADE_STOP_PIPS   integer pips (0 = no stop)
"""
import os
import sys
import json
from oandapyV20 import API
from oandapyV20.endpoints import orders, pricing

ACC = os.environ["OANDA_ACCOUNT_ID"]
TOK = os.environ["OANDA_API_TOKEN"]
ENV = os.environ.get("OANDA_ENV", "practice")

PIP_MAP = {
    "EUR_USD": 0.0001, "GBP_USD": 0.0001, "AUD_USD": 0.0001, "NZD_USD": 0.0001,
    "USD_JPY": 0.01,   "EUR_JPY": 0.01,   "GBP_JPY": 0.01,
    "USD_CAD": 0.0001, "USD_CHF": 0.0001,
    "XAU_USD": 0.01,   "XAG_USD": 0.0001,
    "SPX500_USD": 0.1, "NAS100_USD": 0.1, "US30_USD": 1.0, "DE30_EUR": 0.1,
    "BCO_USD": 0.01,   "WTICO_USD": 0.01, "NATGAS_USD": 0.01,
}

def arg(i, env_key, default=None):
    return sys.argv[i] if len(sys.argv) > i else os.environ.get(env_key, default)

INST = arg(1, "TRADE_INSTRUMENT")
SIDE = arg(2, "TRADE_SIDE", "buy").lower()
UNITS = int(arg(3, "TRADE_UNITS", "1000"))
STOP_PIPS = float(arg(4, "TRADE_STOP_PIPS", "30"))

if INST not in PIP_MAP:
    print(f"ERROR: unknown instrument '{INST}'. Add it to PIP_MAP if needed.")
    sys.exit(1)
if SIDE not in ("buy", "sell"):
    print(f"ERROR: side must be 'buy' or 'sell', got '{SIDE}'")
    sys.exit(1)
if UNITS <= 0:
    print(f"ERROR: units must be positive, got {UNITS}")
    sys.exit(1)

PIP = PIP_MAP[INST]
signed_units = UNITS if SIDE == "buy" else -UNITS

client = API(access_token=TOK, environment=ENV)

# Current price for stop computation
pr_resp = pricing.PricingInfo(accountID=ACC, params={"instruments": INST})
client.request(pr_resp)
pr = pr_resp.response["prices"][0]
bid = float(pr["bids"][0]["price"]); ask = float(pr["asks"][0]["price"])
status = pr.get("status", "?")
print(f"{INST}  bid={bid}  ask={ask}  status={status}")

if status != "tradeable":
    print(f"ERROR: {INST} is {status}. Cannot place order.")
    sys.exit(1)

# Build order
order = {
    "instrument": INST,
    "units": str(signed_units),
    "type": "MARKET",
    "timeInForce": "FOK",
    "positionFill": "DEFAULT",
    "clientExtensions": {"tag": "cloud-manual",
                          "id": f"cloud-{INST}-{SIDE}-{UNITS}-{os.environ.get('GITHUB_RUN_ID', 'local')}"},
    "tradeClientExtensions": {"tag": "cloud-manual"},
}

# Attach stop loss if requested
if STOP_PIPS > 0:
    if SIDE == "buy":
        stop_price = ask - STOP_PIPS * PIP
    else:
        stop_price = bid + STOP_PIPS * PIP
    precision = 5 if PIP < 0.001 else 3
    stop_price = round(stop_price, precision)
    order["stopLossOnFill"] = {"price": f"{stop_price}"}
    print(f"Stop loss at {stop_price} ({STOP_PIPS} pips away)")

# Submit
print(f"Placing: {SIDE.upper()} {UNITS} {INST}...")
r = orders.OrderCreate(accountID=ACC, data={"order": order})
client.request(r)
resp = r.response
fill = resp.get("orderFillTransaction")
if fill:
    tid = fill.get("tradeOpened", {}).get("tradeID")
    print(f"FILLED  trade={tid}  {fill['instrument']}  units={fill['units']}  price={fill['price']}")
    sys.exit(0)
else:
    print("NOT FILLED:")
    print(json.dumps(resp, indent=2)[:1000])
    sys.exit(1)
