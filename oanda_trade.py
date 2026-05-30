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
import json
import os
import sys
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from oandapyV20 import API
from oandapyV20.endpoints import accounts, instruments, orders, positions, pricing
from oandapyV20.exceptions import V20Error

from harness import signals as sig

# Transient upstream failures (OANDA 5xx / Cloudflare 522 / connection timeouts).
# These are not code bugs -- retry briefly, then skip the run; the next cron retries.
TRANSIENT_ERRORS = (V20Error, requests.exceptions.RequestException)

# Shared kill switch + daily/weekly NAV loss limits, same module the RSI bot uses.
# Present when deployed inside the oanda-bot repo; absent in the standalone project.
try:
    import risk_gate
except ImportError:
    risk_gate = None

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

# Gate 11 -- correlation cap. Positively-correlated instruments where holding both
# the same direction doubles one bet. Same-direction NOTIONAL within a group is
# capped at GROUP_GROSS_CAP_PCT of NAV. Singletons have no correlated sibling.
# Relies on the harness matrix running sequentially (max-parallel: 1) so each
# market sees its siblings' freshly-updated positions.
CORRELATION_GROUPS = {
    "risk_on": ["NAS100_USD", "BTC_USD"],   # US equity index + crypto
    "metals":  ["XAU_USD"],
    "energy":  ["WTICO_USD"],
}
GROUP_GROSS_CAP_PCT = 25.0   # max same-direction notional per correlation group, % of NAV


def group_members(inst):
    """Instruments in inst's correlation group (including inst itself)."""
    for members in CORRELATION_GROUPS.values():
        if inst in members:
            return members
    return [inst]


def apply_group_cap(target_units, ref_price, nav, sibling_same_dir_notional):
    """Scale target_units so the group's same-direction notional stays within
    GROUP_GROSS_CAP_PCT of NAV. Returns (capped_units, bound: bool).

    Pure function: build_plan computes sibling_same_dir_notional from live
    positions/prices and passes it in; the unit test exercises this directly.
    """
    if target_units == 0:
        return 0.0, False
    cap_notional = nav * (GROUP_GROSS_CAP_PCT / 100.0)
    allowed = cap_notional - sibling_same_dir_notional
    tgt_notional = abs(target_units) * ref_price
    if allowed <= 0:
        return 0.0, True
    if tgt_notional <= allowed:
        return target_units, False
    return target_units * (allowed / tgt_notional), True


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


def specs_and_prices(client, acc, insts):
    ai = accounts.AccountInstruments(accountID=acc)
    client.request(ai)
    specs = {i["name"]: i for i in ai.response["instruments"]}
    pr = pricing.PricingInfo(accountID=acc, params={"instruments": ",".join(insts)})
    client.request(pr)
    px = {p["instrument"]: {"bid": float(p["bids"][0]["price"]),
                            "ask": float(p["asks"][0]["price"]),
                            "tradeable": p.get("tradeable", False)}
          for p in pr.response["prices"]}
    return specs, px


def current_positions(client, acc, insts) -> dict[str, float]:
    r = positions.OpenPositions(accountID=acc)
    client.request(r)
    out = {}
    for p in r.response["positions"]:
        net = float(p["long"]["units"]) + float(p["short"]["units"])
        out[p["instrument"]] = net
    return {inst: out.get(inst, 0.0) for inst in insts}


def round_units(units, spec):
    step = float(spec["minimumTradeSize"])
    return float(f"{round(units / step) * step:.6f}")


def round_price(price, spec):
    return f"{price:.{int(spec['displayPrecision'])}f}"


def units_str(u):
    return str(int(u)) if float(u).is_integer() else str(u)


def reject_reason(exc):
    """Pull a concise rejection reason out of a V20Error's JSON body."""
    try:
        d = json.loads(str(exc))
    except Exception:  # noqa: BLE001
        return str(exc)[:120]
    return (d.get("errorCode")
            or d.get("orderRejectTransaction", {}).get("rejectReason")
            or d.get("orderCancelTransaction", {}).get("reason")
            or "rejected")


def build_plan(client, acc, markets):
    nav, margin_avail, ccy = account_state(client, acc)
    insts = [MARKETS[m] for m in markets]
    # Also fetch group siblings so existing correlated exposure can be valued.
    needed = sorted(set(insts) | {s for inst in insts for s in group_members(inst)})
    specs, px = specs_and_prices(client, acc, needed)
    pos = current_positions(client, acc, needed)
    # Per-instrument margin cap (total budget / 4) so the cap behaves the same
    # whether this process handles one market or all four (matrix-safe).
    per_inst_cap = nav * (MAX_TOTAL_MARGIN_PCT / 100.0) / len(MARKETS)
    plan = []
    for market in markets:
        inst = MARKETS[market]
        spec, price = specs[inst], px[inst]
        s = sig.compute(market, fetch_candles(client, inst), SIGNAL_PARAMS)
        ref = price["ask"] if s.direction == "LONG" else price["bid"]

        target, stop_price, cap_note = 0.0, None, ""
        if s.direction != "FLAT" and s.atr > 0:
            risk_cash = nav * (RISK_PER_TRADE_PCT / 100.0) * abs(s.composite)
            stop_dist = ATR_STOP_MULT * s.atr
            target = round_units(risk_cash / stop_dist, spec)
            if s.direction == "SHORT":
                target = -target
            stop_price = (ref - stop_dist) if s.direction == "LONG" else (ref + stop_dist)

            # Cap this instrument's margin individually.
            margin = abs(target) * ref * float(spec["marginRate"])
            if margin > per_inst_cap and margin > 0:
                target = round_units(target * (per_inst_cap / margin), spec)

            # Correlation cap: sum siblings' SAME-direction notional, then bound.
            sib_notional = 0.0
            for sib in group_members(inst):
                if sib == inst:
                    continue
                sp = pos.get(sib, 0.0)
                if sp != 0.0 and (sp > 0) == (target > 0) and sib in px:
                    sib_notional += abs(sp) * px[sib]["ask"]
            capped, bound = apply_group_cap(target, ref, nav, sib_notional)
            if bound:
                target = round_units(capped, spec)
                budget = nav * GROUP_GROSS_CAP_PCT / 100.0
                cap_note = (f"corr-cap: group same-dir notional {sib_notional:,.0f} "
                            f"of {budget:,.0f} budget")

            stop_price = None if abs(target) < 1e-9 else (
                (ref - stop_dist) if target > 0 else (ref + stop_dist))

        plan.append({"market": market, "inst": inst, "signal": s, "ref": ref,
                     "tradeable": price["tradeable"], "current": pos[inst],
                     "target": target, "stop": stop_price, "spec": spec,
                     "cap_note": cap_note,
                     "margin": abs(target) * ref * float(spec["marginRate"])})

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
    ap.add_argument("market", nargs="?", choices=list(MARKETS),
                    help="single market to handle (default: all four)")
    ap.add_argument("--live", action="store_true", help="actually place/modify orders")
    args = ap.parse_args()

    markets = [args.market] if args.market else list(MARKETS)
    client, acc = make_client()

    # Read phase, with a short retry so a brief OANDA blip doesn't fail the job.
    # A genuine code bug raises a non-transient exception and still exits 1.
    plan = None
    for attempt in range(1, 4):
        try:
            nav, margin_avail, ccy, plan = build_plan(client, acc, markets)
            break
        except TRANSIENT_ERRORS as e:
            msg = " ".join(str(e).split())[:160]
            print(f"  OANDA API error (attempt {attempt}/3): {msg}")
            if attempt < 3:
                time.sleep(5)
    if plan is None:
        # Transient blips are absorbed by the 3 retries above; reaching here means a
        # sustained outage. Exit non-zero so the Bot Alert workflow (Gate 9) notifies.
        # No orders were sent.
        print("OANDA API unavailable after 3 attempts (sustained outage) -- "
              "failing to trigger the alert; no orders were sent.")
        sys.exit(1)

    print(f"\nACCOUNT {acc} (practice)  NAV={nav:,.2f} {ccy}  marginAvail={margin_avail:,.2f}")
    print("\n  MARKET   INST         SIG    SCORE  CURRENT       TARGET        STOP         ACTION  OK?")
    print("  " + "-" * 96)
    for p in plan:
        s = p["signal"]
        stop = f"{p['stop']:,.3f}" if p["stop"] is not None else "-"
        ok = "open" if p["tradeable"] else "CLOSED"
        print(f"  {p['market']:<8} {p['inst']:<12} {s.direction:<6} {s.composite:+.2f}  "
              f"{p['current']:<13,.4f} {p['target']:<13,.4f} {stop:<12} {p['action']:<7} {ok}")
        if p.get("cap_note"):
            print(f"      ~ {p['cap_note']}")

    todo = [p for p in plan if p["action"] in ("OPEN", "CLOSE", "RESET")]
    actionable = [p for p in todo if p["tradeable"]]
    blocked = [p for p in todo if not p["tradeable"]]

    # Pre-entry risk gate, shared with the RSI bot. Exits/closes are NEVER gated —
    # de-risking is always allowed; only new exposure is blocked.
    if risk_gate is None:
        allow_entries, gate_reason = False, "risk_gate module unavailable -> entries blocked (fail-safe)"
    else:
        try:
            allow_entries, gate_reason = risk_gate.check(client, acc)
        except Exception as e:  # noqa: BLE001
            allow_entries, gate_reason = False, f"risk gate query failed -> entries blocked: {e}"
    print(f"\n  risk gate: {gate_reason}")

    if not args.live:
        opens = sum(p["action"] in ("OPEN", "RESET") for p in actionable)
        print(f"\nDRY-RUN. {len(actionable)} action(s) ready ({opens} need the entry gate), "
              f"{len(blocked)} blocked by closed market, "
              f"{sum(p['action']=='HOLD' for p in plan)} hold. Re-run with --live to execute.")
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
                if not allow_entries:
                    print(f"  SKIP open {p['inst']}: entries blocked by risk gate")
                    continue
                resp = open_to_target(client, acc, p)
                fill = resp.get("orderFillTransaction")
                if fill:
                    print(f"  OPENED {p['inst']}  units={fill['units']}  price={fill['price']}  "
                          f"tradeID={fill.get('tradeOpened', {}).get('tradeID')}")
                else:
                    reason = resp.get("orderCancelTransaction", {}).get("reason", "see response")
                    print(f"  NOT FILLED {p['inst']}: {reason}")
        except V20Error as ve:
            # Hard broker rejection (4xx): log the reason cleanly and move on.
            # No retry, no phantom position -- this run simply skips the instrument.
            print(f"  REJECTED {p['inst']}: {reject_reason(ve)} (no fill, no retry)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {p['inst']} unexpected error: {exc}")


if __name__ == "__main__":
    main()
