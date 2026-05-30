"""Gate 8 -- deliberate order-rejection test (PRACTICE account).

Sends a market order designed to be rejected -- units far over the instrument's
maximum (also MARKET_HALTED on weekends) -- and verifies the bot's contract:
  * no fill,
  * a cancel/reject transaction WITH a reason is returned (handled, not crashed),
  * no phantom position is created.

Partial fills are structurally impossible: every bot order is timeInForce FOK
(fill-or-kill), so an order fills in full or is killed. This exercises the kill path.

SAFE: FOK + over-max units cannot fill; a safety net closes any unexpected fill.
Run:  python test_order_rejection.py        (exit 0 = pass, 1 = fail)
"""
import json
import os
import sys

from dotenv import load_dotenv
from oandapyV20 import API
from oandapyV20.endpoints import accounts, orders, positions, pricing
from oandapyV20.exceptions import V20Error

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
ENV = os.getenv("OANDA_ENV", "practice")
if ENV != "practice":
    raise SystemExit(f"REFUSING: OANDA_ENV={ENV!r} is not 'practice'")
client = API(access_token=os.getenv("OANDA_API_TOKEN"), environment=ENV)
ACC = os.getenv("OANDA_ACCOUNT_ID")
INST = "NAS100_USD"
TAG = "gate8-reject-test"


def net_position(inst):
    r = positions.OpenPositions(accountID=ACC)
    client.request(r)
    for p in r.response["positions"]:
        if p["instrument"] == inst:
            return float(p["long"]["units"]) + float(p["short"]["units"])
    return 0.0


def main():
    ai = accounts.AccountInstruments(accountID=ACC, params={"instruments": INST})
    client.request(ai)
    spec = ai.response["instruments"][0]
    max_units = float(spec["maximumOrderUnits"])
    prec = int(spec["displayPrecision"])

    pr = pricing.PricingInfo(accountID=ACC, params={"instruments": INST})
    client.request(pr)
    ask = float(pr.response["prices"][0]["asks"][0]["price"])
    stop = round(ask * 0.9, prec)                 # valid stop below a long entry
    units = int(max_units * 5) + 1                # far over the maximum -> reject

    before = net_position(INST)
    data = {"order": {
        "instrument": INST, "units": str(units), "type": "MARKET",
        "timeInForce": "FOK", "positionFill": "DEFAULT",
        "stopLossOnFill": {"price": f"{stop}"},
        "clientExtensions": {"tag": TAG, "id": f"{TAG}-1"},
        "tradeClientExtensions": {"tag": TAG}}}

    # A rejection arrives one of two ways: a 201 with an orderCancelTransaction
    # (e.g. MARKET_HALTED), or a 4xx that oandapyV20 raises as V20Error (e.g.
    # UNITS_LIMIT_EXCEEDED). Both must be handled without a fill or a phantom.
    fill, reason = None, None
    try:
        resp = client.request(orders.OrderCreate(accountID=ACC, data=data))
        fill = resp.get("orderFillTransaction")
        cx = resp.get("orderCancelTransaction") or resp.get("orderRejectTransaction") or {}
        reason = cx.get("reason") or cx.get("rejectReason")
    except V20Error as ve:
        try:
            d = json.loads(str(ve))
        except Exception:
            d = {}
        reason = (d.get("errorCode")
                  or d.get("orderRejectTransaction", {}).get("rejectReason"))
    after = net_position(INST)

    # Safety net: an unexpected fill must never linger on the account.
    if fill is not None:
        try:
            cu = net_position(INST)
            d = {"longUnits": "ALL"} if cu > 0 else {"shortUnits": "ALL"}
            client.request(positions.PositionClose(accountID=ACC, instrument=INST, data=d))
            print("!! UNEXPECTED FILL -- closed it as a safety net")
        except Exception as e:  # noqa: BLE001
            print(f"!! UNEXPECTED FILL and close failed: {e}")

    checks = [
        ("order was NOT filled", fill is None),
        ("a reject/cancel reason was returned", bool(reason)),
        ("no phantom position created", abs(after - before) < 1e-9),
    ]
    print(f"  instrument={INST}  units={units} (max {max_units})")
    print(f"  reject reason: {reason}")
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    n_fail = sum(1 for _, ok in checks if not ok)
    print(f"\n{len(checks) - n_fail}/{len(checks)} passed")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
