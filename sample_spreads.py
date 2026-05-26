"""Sample current bid/ask spread for each deployed instrument.

Run a few times across sessions (London open, NY open, off-peak) to calibrate
realistic spread defaults for the backtester.
"""
import os
from dotenv import load_dotenv
from oandapyV20 import API
from oandapyV20.endpoints import pricing

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

ACC = os.environ["OANDA_ACCOUNT_ID"]
client = API(access_token=os.environ["OANDA_API_TOKEN"],
             environment=os.environ.get("OANDA_ENV", "practice"))

INSTRUMENTS = ["EUR_USD", "XAU_USD", "NAS100_USD", "XAG_USD", "GBP_JPY"]
PIP = {"EUR_USD": 0.0001, "XAU_USD": 0.01, "NAS100_USD": 0.1,
       "XAG_USD": 0.0001, "GBP_JPY": 0.01}

r = pricing.PricingInfo(accountID=ACC, params={"instruments": ",".join(INSTRUMENTS)})
client.request(r)

print(f"{'instrument':<12} {'bid':>12} {'ask':>12} {'spread_abs':>12} {'spread_pips':>12} {'status':>10}")
print("-" * 76)
for p in r.response["prices"]:
    inst = p["instrument"]
    bid = float(p["bids"][0]["price"])
    ask = float(p["asks"][0]["price"])
    spread_abs = ask - bid
    spread_pips = spread_abs / PIP[inst]
    status = p.get("status", "?")
    print(f"{inst:<12} {bid:>12.5f} {ask:>12.5f} {spread_abs:>12.5f} {spread_pips:>12.1f} {status:>10}")
