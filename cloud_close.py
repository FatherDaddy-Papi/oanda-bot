"""
Close manual cloud-tagged positions for a given instrument (or all instruments).

Will NOT close trades tagged 'rsi-bot-*' — those are the automated bot's, leave them alone.

Required env: OANDA_ACCOUNT_ID, OANDA_API_TOKEN
Inputs (env or CLI):
    TRADE_INSTRUMENT  e.g. EUR_USD, or ALL to close everything manual
"""
import os
import sys
import json
from oandapyV20 import API
from oandapyV20.endpoints import trades as tep

ACC = os.environ["OANDA_ACCOUNT_ID"]
TOK = os.environ["OANDA_API_TOKEN"]
ENV = os.environ.get("OANDA_ENV", "practice")
INST = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TRADE_INSTRUMENT", "ALL")).upper()

client = API(access_token=TOK, environment=ENV)
r = tep.OpenTrades(accountID=ACC)
client.request(r)

closed = 0
skipped_bot = 0
errors = 0
for t in r.response.get("trades", []):
    inst = t["instrument"]
    if INST != "ALL" and inst != INST:
        continue
    ext = t.get("clientExtensions") or {}
    tag = ext.get("tag", "")
    if tag.startswith("rsi-bot"):
        skipped_bot += 1
        continue
    print(f"Closing trade {t['id']}: {inst} units={t['currentUnits']}  (tag={tag})")
    cr = tep.TradeClose(accountID=ACC, tradeID=str(t["id"]))
    try:
        client.request(cr)
        fill = cr.response.get("orderFillTransaction")
        if fill:
            print(f"  CLOSED at {fill['price']}  pnl={fill.get('pl')}")
            closed += 1
        else:
            print(f"  NOT FILLED: {json.dumps(cr.response)[:300]}")
            errors += 1
    except Exception as e:
        print(f"  ERROR: {e}")
        errors += 1

print(f"\nSummary: closed={closed}  skipped_bot_trades={skipped_bot}  errors={errors}")
if errors > 0:
    sys.exit(1)
