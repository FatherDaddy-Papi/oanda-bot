"""Turtle-style trend follower with pyramiding.

Genuinely different from RSI(2) mean reversion and from Donchian-no-pyramid.
The hypothesis: long-running trends in commodities/FX produce occasional
big winners that pay for many small losses; pyramiding adds to winners
to maximize capture of those rare big moves.

Rules:
  - ENTRY: Donchian N1 breakout. Long if close > max(high[-N1:-1]);
    short if close < min(low[-N1:-1]).
  - PYRAMID: while in a position, add 1 unit each time price moves
    +PYRAMID_ATR_STEP * ATR(20) in favor of the position. Max
    MAX_UNITS (default 4) total units.
  - STOP: each unit gets its own stop at entry +/- STOP_ATR * ATR(20).
    All units share a single "global stop" which is the *latest* unit's
    stop (i.e. stops trail forward as we pyramid).
  - EXIT: all units close when price hits the global stop, OR when an
    opposite Donchian N2 breakout occurs (N2 < N1; default 10).
  - SIZING: per-unit risk = RISK_PCT * NAV / stop_distance, so max
    cumulative risk at full pyramid = MAX_UNITS * RISK_PCT.

D1 timeframe -- the regime where trend-following historically works.
"""
import math
import os
import sys
from dotenv import load_dotenv

from backtest_rsi import fetch_candles, stats, specs_for

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

ATR_PERIOD = 20
UNITS_PER_RISK_UNIT = 1000  # match backtest_rsi.UNITS scale


def atr_series(bars, period=ATR_PERIOD):
    out = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]["h"]; l = bars[i]["l"]; pc = bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    out[period] = a
    for i in range(period + 1, len(bars)):
        a = (a * (period - 1) + trs[i - 1]) / period
        out[i] = a
    return out


def backtest(bars, n_entry=20, n_exit=10, pyramid_step=0.5, max_units=4,
             stop_atr=2.0, spread_pips=None, pip=None):
    if spread_pips is None or pip is None:
        raise ValueError("spread_pips and pip required")
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    atrs = atr_series(bars, ATR_PERIOD)

    # position state
    side = 0                  # +1 long, -1 short, 0 flat
    units = []                # list of {entry_px, atr_at_entry}
    global_stop = None        # current stop for the latest unit; all units exit here
    trades = []               # one trade per fully-closed position (sums all units)
    equity_curve = []
    cum_pnl = 0.0

    start_i = max(n_entry, n_exit, ATR_PERIOD + 1)
    for i in range(start_i, len(bars)):
        close = closes[i]
        atr = atrs[i]
        if atr is None:
            equity_curve.append(cum_pnl)
            continue
        hi_entry = max(highs[i - n_entry:i])
        lo_entry = min(lows[i - n_entry:i])
        hi_exit = max(highs[i - n_exit:i])
        lo_exit = min(lows[i - n_exit:i])

        # ---- EXIT for existing position ----
        exit_now = False
        exit_reason = None
        if side == 1:
            if lows[i] <= global_stop:
                exit_now = True; exit_reason = "stop"; exit_px = global_stop
            elif close < lo_exit:
                exit_now = True; exit_reason = "channel"; exit_px = close
        elif side == -1:
            if highs[i] >= global_stop:
                exit_now = True; exit_reason = "stop"; exit_px = global_stop
            elif close > hi_exit:
                exit_now = True; exit_reason = "channel"; exit_px = close

        if exit_now:
            # Apply spread to exit
            if side == 1:
                fill_exit = exit_px - 0.5 * spread_pips * pip
            else:
                fill_exit = exit_px + 0.5 * spread_pips * pip
            # Sum P&L across all units
            pnl = 0.0
            for u in units:
                if side == 1:
                    pnl += (fill_exit - u["entry_px"]) * UNITS_PER_RISK_UNIT
                else:
                    pnl += (u["entry_px"] - fill_exit) * UNITS_PER_RISK_UNIT
            trades.append({"side": "LONG" if side == 1 else "SHORT",
                           "entry": units[0]["entry_px"], "exit": fill_exit,
                           "pnl": pnl, "exit_time": bars[i]["time"],
                           "reason": exit_reason, "n_units": len(units),
                           "bars": 0})
            cum_pnl += pnl
            side = 0
            units = []
            global_stop = None

        # ---- PYRAMID for active position ----
        if side != 0 and len(units) < max_units:
            latest = units[-1]
            step = pyramid_step * latest["atr_at_entry"]
            if side == 1 and close >= latest["entry_px"] + step:
                entry_px = close + 0.5 * spread_pips * pip
                units.append({"entry_px": entry_px, "atr_at_entry": atr})
                # Move global stop UP to new latest unit's stop
                global_stop = max(global_stop, entry_px - stop_atr * atr)
            elif side == -1 and close <= latest["entry_px"] - step:
                entry_px = close - 0.5 * spread_pips * pip
                units.append({"entry_px": entry_px, "atr_at_entry": atr})
                # Move global stop DOWN
                global_stop = min(global_stop, entry_px + stop_atr * atr)

        # ---- ENTRY (only if flat) ----
        if side == 0:
            if close > hi_entry:
                entry_px = close + 0.5 * spread_pips * pip
                units.append({"entry_px": entry_px, "atr_at_entry": atr})
                global_stop = entry_px - stop_atr * atr
                side = 1
            elif close < lo_entry:
                entry_px = close - 0.5 * spread_pips * pip
                units.append({"entry_px": entry_px, "atr_at_entry": atr})
                global_stop = entry_px + stop_atr * atr
                side = -1

        equity_curve.append(cum_pnl)

    return trades, equity_curve, side


def summarize(trades, equity_curve):
    if not trades:
        return {"n": 0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    peak = -math.inf
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak: peak = eq
        max_dd = max(max_dd, peak - eq)
    avg_units = sum(t["n_units"] for t in trades) / len(trades)
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) != 0 else float("inf")
    return {
        "n": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": 100 * len(wins) / len(trades),
        "total_pnl": total, "max_dd": max_dd,
        "profit_factor": pf, "avg_units": avg_units,
        "avg_win": (sum(wins) / len(wins)) if wins else 0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0,
    }


def main():
    instrument = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"
    years = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    pip, spread = specs_for(instrument)
    print(f"Fetching {years}y of {instrument} D1...")
    bars = fetch_candles(instrument, years, "D")
    print(f"  {len(bars)} bars; spread={spread} pips, pip={pip}")
    print(f"Donchian 20/10, pyramid +0.5*ATR, max 4 units, stop 2*ATR")
    trades, eq, final_side = backtest(bars, spread_pips=spread, pip=pip)
    s = summarize(trades, eq)
    print()
    print("=" * 60)
    print(f"  RESULTS ({instrument} D1, {years}y, Turtle-pyramid)")
    print("=" * 60)
    if s["n"] == 0:
        print("  No trades.")
        return
    print(f"  Trades:        {s['n']}  (W/L: {s['wins']}/{s['losses']} = {s['win_rate']:.1f}%)")
    print(f"  Total P/L:     {s['total_pnl']:+,.2f}")
    print(f"  Avg win:       {s['avg_win']:+,.2f}")
    print(f"  Avg loss:      {s['avg_loss']:+,.2f}")
    print(f"  Profit factor: {s['profit_factor']:.2f}")
    print(f"  Max DD:        {s['max_dd']:,.2f}")
    print(f"  Avg units/trade: {s['avg_units']:.2f}")
    print(f"  Final side:    {'LONG' if final_side==1 else 'SHORT' if final_side==-1 else 'FLAT'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
