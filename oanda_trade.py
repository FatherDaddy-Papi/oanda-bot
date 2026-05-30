"""Execute harness signals on OANDA (PRACTICE account only) with position management.

Computes the same 4-vote signal as the harness, but from OANDA's own daily candles
so prices and ATR stops are self-consistent with where it will actually fill.

Position management (daily rebalance):
  * Reads the current net position per instrument.
  * FLAT signal      -> close any open position.
  * No position      -> open to target with an ATR stop-loss.
  * Same direction, size within HOLD_TOLERANCE -> hold (no churn, keep existing stop).
  * Reversal or large resize -> close fully, reopen to target with a fresh ATR stop.

The four instruments are disjoint from the FX bot's universe, so the net position
per instrument is unambiguously the harness's own.

Safety:
  * Refuses to run unless OANDA_ENV == 'practice'.
  * Default is DRY-RUN. Pass --live to send orders.
  * Skips instruments that are not currently tradeable (e.g. weekend).
  * 25%-of-NAV margin cap across the four positions.

Usage:
  python oanda_trade.py          # dry-run plan (incl. current positions)
  python oanda_trade.py --live   # rebalance live (markets must be open)
"""
from __future__ import annotations

import argparse
import os

import pandas as pd
from dotenv import load_dotenv
from oandapyV20 import API
from oandapyV20.endpoints import accounts, instruments, orders, positions, pricing

from harness import signals as sig

ENV_PATH = r"C:\Users\theod\projects\oanda-paper-trading\.env"

# harness market -> OANDA instrument
MARKETS = {
    "NASDAQ": "NAS100_USD",
    "OIL":    "WTICO_USD",
    "BTC":    "BTC_USD",
    "GOLD":   "XAU_USD",
}
ORDER_TAG = "harness"

# Risk settings (conservative; this account also runs the FX bot)
RISK_PER_TRADE_PCT = 0.40    # % of NAV risked to the stop at full conviction
ATR_STOP_MULT = 2.0
MAX_TOTAL_MARGIN_PCT = 25.0  # cap on combined margin of the four harness positions
HOLD_TOLERANCE = 0.20        # don't re-trade if within 20% of target same-direction
SIGNAL_PARAMS = {
    "ema_fast": 20, "ema_slow": 50, "regime_ma": 200,
    "donchian": 20, "momentum": 90, "atr": 14,
}


def make_client():
    # Local runs read the OANDA project's .env; CI injects the same vars as secrets.
    if os.path.exists(ENV_PATH):
        load_dotenv(ENV_PATH)
    env = os.getenv("OANDA_ENV", "practice")
    if env != "practice":
        raise SystemExit(f"REFUSING TO RUN: OANDA_ENV={env!r} is not 'practice'.")
    client = API(access_token=os.getenv("OANDA_API_TOKEN"), environment=env)
    return client, os.getenv("OANDA_ACCOUNT_ID")


def fetch_candles(client, inst, count=400) -> pd.DataFrame:
    r = instruments.InstrumentsCandles(
        instrument=inst, params={"granularity": "D", "count": count, "price": "M"})
    client.request(r)
    rows = []
    for c in r.response["candles"]:
        if not c["complete"]:
            continue
        m = c["mid"]
        rows.append({"datetime": c["time"][:10], "open": float(m["o"]),
                     "high": float(m["h"]), "low": float(m["l"]),
                     "close": float(m["c"]), "volume": float(c["volume"])})
    return pd.DataFrame(rows).set_index("datetime")


def account_state(client, acc):
    s = accounts.AccountSummary(accountID=acc)
    client.request(s)
    a = s.response["account"]
    return float(a["NAV"]), float(a["marginAvailable"]), a["currency"]


def specs_and_prices(client, acc):
    ai = accounts.AccountInstruments(accountID=acc)
    client.request(ai)
    specs = {i["name"]: i for i in ai.response["instruments"]}
    pr = pricing.PricingInfo(accountID=acc, params={"instruments": ",".join(MARKETS.values())})
    client.request(pr)
    px = {p["instrument"]: {"bid": float(p["bids"][0]["price"]),
                            "ask": float(p["asks"][0]["price"]),
                            "tradeable": p.get("tradeable", False)}
          for p in pr.response["prices"]}
    return specs, px


def current_positions(client, acc) -> dict[str, float]:
    r = positions.OpenPositions(accountID=acc)
    client.request(r)
    out = {}
    for p in r.response["positions"]:
        net = float(p["long"]["units"]) + float(p["short"]["units"])
        out[p["instrument"]] = net
    return {inst: out.get(inst, 0.0) for inst in MARKETS.values()}


def round_units(units, spec):
    step = float(spec["minimumTradeSize"])
    return float(f"{round(units / step) * step:.6f}")


def round_price(price, spec):
    return f"{price:.{int(spec['displayPrecision'])}f}"


def units_str(u):
    return str(int(u)) if float(u).is_integer() else str(u)


def build_plan(client, acc):
    nav, margin_avail, ccy = account_state(client, acc)
    specs, px = specs_and_prices(client, acc)
    pos = current_positions(client, acc)
    plan = []
    for market, inst in MARKETS.items():
        spec, price = specs[inst], px[inst]
        s = sig.compute(market, fetch_candles(client, inst), SIGNAL_PARAMS)
        ref = price["ask"] if s.direction == "LONG" else price["bid"]

        target, stop_price = 0.0, None
        if s.direction != "FLAT" and s.atr > 0:
            risk_cash = nav * (RISK_PER_TRADE_PCT / 100.0) * abs(s.composite)
            stop_dist = ATR_STOP_MULT * s.atr
            target = round_units(risk_cash / stop_dist, spec)
            if s.direction == "SHORT":
                target = -target
            stop_price = (ref - stop_dist) if s.direction == "LONG" else (ref + stop_dist)

        plan.append({"market": market, "inst": inst, "signal": s, "ref": ref,
                     "tradeable": price["tradeable"], "current": pos[inst],
                     "target": target, "stop": stop_price, "spec": spec,
                     "margin": abs(target) * ref * float(spec["marginRate"])})

    # Margin cap across the four
    total = sum(p["margin"] for p in plan)
    cap = nav * (MAX_TOTAL_MARGIN_PCT / 100.0)
    if total > cap and total > 0:
        scale = cap / total
        for p in plan:
            p["target"] = round_units(p["target"] * scale, p["spec"])
            p["margin"] = abs(p["target"]) * p["ref"] * float(p["spec"]["marginRate"])

    # Decide the action for each instrument
    for p in plan:
        cur, tgt = p["current"], p["target"]
        if abs(tgt) < 1e-9:
            p["action"] = "CLOSE" if abs(cur) > 1e-9 else "NONE"
        elif abs(cur) < 1e-9:
            p["action"] = "OPEN"
        elif (cur > 0) == (tgt > 0) and abs(tgt - cur) <= HOLD_TOLERANCE * abs(tgt):
            p["action"] = "HOLD"
        else:
            p["action"] = "RESET"   # reversal or material resize
    return nav, margin_avail, ccy, plan


def close_position(client, acc, inst, current):
    data = {"longUnits": "ALL"} if current > 0 else {"shortUnits": "ALL"}
    r = positions.PositionClose(accountID=acc, instrument=inst, data=data)
    client.request(r)
    return r.response


def open_to_target(client, acc, p):
    spec = p["spec"]
    data = {"order": {
        "instrument": p["inst"], "units": units_str(p["target"]),
        "type": "MARKET", "timeInForce": "FOK", "positionFill": "DEFAULT",
        "stopLossOnFill": {"price": round_price(p["stop"], spec)},
        "clientExtensions": {"tag": ORDER_TAG, "id": f"{ORDER_TAG}-{p['inst']}"},
        "tradeClientExtensions": {"tag": ORDER_TAG}}}
    r = orders.OrderCreate(accountID=acc, data=data)
    client.request(r)
    return r.response


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually place/modify orders")
    args = ap.parse_args()

    client, acc = make_client()
    nav, margin_avail, ccy, plan = build_plan(client, acc)

    print(f"\nACCOUNT {acc} (practice)  NAV={nav:,.2f} {ccy}  marginAvail={margin_avail:,.2f}")
    print("\n  MARKET   INST         SIG    SCORE  CURRENT       TARGET        STOP         ACTION  OK?")
    print("  " + "-" * 96)
    for p in plan:
        s = p["signal"]
        stop = f"{p['stop']:,.3f}" if p["stop"] is not None else "-"
        ok = "open" if p["tradeable"] else "CLOSED"
        print(f"  {p['market']:<8} {p['inst']:<12} {s.direction:<6} {s.composite:+.2f}  "
              f"{p['current']:<13,.4f} {p['target']:<13,.4f} {stop:<12} {p['action']:<7} {ok}")

    todo = [p for p in plan if p["action"] in ("OPEN", "CLOSE", "RESET")]
    actionable = [p for p in todo if p["tradeable"]]
    blocked = [p for p in todo if not p["tradeable"]]

    if not args.live:
        print(f"\nDRY-RUN. {len(actionable)} action(s) ready, {len(blocked)} blocked by "
              f"closed market, {sum(p['action']=='HOLD' for p in plan)} hold. "
              f"Re-run with --live to execute.")
        return

    if not actionable:
        print("\nNothing actionable (markets closed, all FLAT, or all holding). No orders sent.")
        return

    print("\n=== EXECUTING LIVE (practice) ===")
    for p in actionable:
        try:
            if p["action"] in ("CLOSE", "RESET") and abs(p["current"]) > 1e-9:
                close_position(client, acc, p["inst"], p["current"])
                print(f"  CLOSED {p['inst']} ({p['current']:+.4f} units)")
            if p["action"] in ("OPEN", "RESET"):
                resp = open_to_target(client, acc, p)
                fill = resp.get("orderFillTransaction")
                if fill:
                    print(f"  OPENED {p['inst']}  units={fill['units']}  price={fill['price']}  "
                          f"tradeID={fill.get('tradeOpened', {}).get('tradeID')}")
                else:
                    reason = resp.get("orderCancelTransaction", {}).get("reason", "see response")
                    print(f"  NOT FILLED {p['inst']}: {reason}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {p['inst']} error: {exc}")


if __name__ == "__main__":
    main()
