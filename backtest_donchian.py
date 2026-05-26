"""Donchian channel breakout backtester (Turtle Traders style).

Rules:
  - Long entry:  close > max(highs[i-N_entry .. i-1])    (N-bar breakout)
  - Long exit:   close < min(lows[i-N_exit .. i-1])      OR  stop hit
  - Short entry: close < min(lows[i-N_entry .. i-1])
  - Short exit:  close > max(highs[i-N_exit .. i-1])     OR  stop hit
  - Initial stop: STOP_ATR_MULTIPLE * ATR(14) from entry
  - 1 position at a time
  - Per-instrument realistic slippage via backtest_rsi.INSTRUMENT_SPECS

Same fetch_candles / stats helpers reused from backtest_rsi.

Usage:
    python backtest_donchian.py [INSTRUMENT] [YEARS] [N_ENTRY] [N_EXIT] [STOP_ATR]
    python backtest_donchian.py EUR_USD 2 20 10 2.0
"""
import os
import sys
import math
from dotenv import load_dotenv

from backtest_rsi import fetch_candles, stats, specs_for

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

UNITS = 1000
ATR_PERIOD = 14


def atr_series(bars, period=ATR_PERIOD):
    out = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]["h"]; l = bars[i]["l"]; pc = bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder smoothing
    a = sum(trs[:period]) / period
    out[period] = a
    for i in range(period + 1, len(bars)):
        a = (a * (period - 1) + trs[i - 1]) / period
        out[i] = a
    return out


def backtest(bars, n_entry, n_exit, stop_atr, spread_pips=None, pip=None):
    """Donchian breakout backtest. Returns (trades, equity_curve, final_pos)."""
    if spread_pips is None or pip is None:
        raise ValueError("spread_pips and pip required (use specs_for(instrument))")
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    atrs = atr_series(bars, ATR_PERIOD)
    position = 0
    entry_price = 0.0
    stop_price = 0.0
    trades = []
    equity_curve = []
    cum_pnl = 0.0

    start_i = max(n_entry, n_exit, ATR_PERIOD + 1)
    for i in range(start_i, len(bars)):
        close = closes[i]
        hi_entry = max(highs[i - n_entry:i])  # excludes current bar
        lo_entry = min(lows[i - n_entry:i])
        hi_exit = max(highs[i - n_exit:i])
        lo_exit = min(lows[i - n_exit:i])

        # ---- EXIT ----
        if position == 1:
            # Stop hit (using bar low) or channel cross
            if lows[i] <= stop_price:
                exit_px = stop_price - 0.5 * spread_pips * pip
                pnl = (exit_px - entry_price) * UNITS
                trades.append({"side": "LONG", "entry": entry_price, "exit": exit_px,
                               "pnl": pnl, "exit_time": bars[i]["time"], "reason": "stop"})
                cum_pnl += pnl
                position = 0
            elif close < lo_exit:
                exit_px = close - 0.5 * spread_pips * pip
                pnl = (exit_px - entry_price) * UNITS
                trades.append({"side": "LONG", "entry": entry_price, "exit": exit_px,
                               "pnl": pnl, "exit_time": bars[i]["time"], "reason": "channel"})
                cum_pnl += pnl
                position = 0
        elif position == -1:
            if highs[i] >= stop_price:
                exit_px = stop_price + 0.5 * spread_pips * pip
                pnl = (entry_price - exit_px) * UNITS
                trades.append({"side": "SHORT", "entry": entry_price, "exit": exit_px,
                               "pnl": pnl, "exit_time": bars[i]["time"], "reason": "stop"})
                cum_pnl += pnl
                position = 0
            elif close > hi_exit:
                exit_px = close + 0.5 * spread_pips * pip
                pnl = (entry_price - exit_px) * UNITS
                trades.append({"side": "SHORT", "entry": entry_price, "exit": exit_px,
                               "pnl": pnl, "exit_time": bars[i]["time"], "reason": "channel"})
                cum_pnl += pnl
                position = 0

        # ---- ENTRY (if flat) ----
        if position == 0 and atrs[i] is not None:
            if close > hi_entry:
                entry_price = close + 0.5 * spread_pips * pip
                stop_price = close - stop_atr * atrs[i]
                position = 1
            elif close < lo_entry:
                entry_price = close - 0.5 * spread_pips * pip
                stop_price = close + stop_atr * atrs[i]
                position = -1

        equity_curve.append(cum_pnl)

    # Adapt trade dicts to backtest_rsi.stats() shape
    for t in trades:
        t["bars"] = 0  # not tracked; stats() reads but doesn't require non-zero
    return trades, equity_curve, position


def main():
    instrument = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"
    years = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    n_entry = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    n_exit = int(sys.argv[4]) if len(sys.argv) > 4 else 10
    stop_atr = float(sys.argv[5]) if len(sys.argv) > 5 else 2.0

    pip, spread = specs_for(instrument)
    print(f"Fetching {years}y of {instrument} H1...")
    bars = fetch_candles(instrument, years, "H1")
    print(f"  {len(bars)} bars; spread={spread} pips, pip={pip}")
    print(f"Donchian({n_entry}/{n_exit}), stop {stop_atr}*ATR({ATR_PERIOD})")
    trades, eq, final_pos = backtest(bars, n_entry, n_exit, stop_atr,
                                     spread_pips=spread, pip=pip)
    s = stats(trades, eq)
    print()
    print("=" * 60)
    print(f"  RESULTS  ({instrument}, {years}y H1, Donchian {n_entry}/{n_exit}, stop {stop_atr}xATR)")
    print("=" * 60)
    if s.get("n", 0) == 0:
        print("  No trades.")
        return
    print(f"  Trades:         {s['n']}")
    print(f"  Wins / Losses:  {s['wins']} / {s['losses']}  ({s['win_rate']:.1f}%)")
    print(f"  Total P/L:      {s['total_pnl']:+,.2f}")
    print(f"  Avg win:        {s['avg_win']:+,.2f}")
    print(f"  Avg loss:       {s['avg_loss']:+,.2f}")
    print(f"  Profit factor:  {s['profit_factor']:.2f}")
    print(f"  Max drawdown:   {s['max_dd']:,.2f}")
    print(f"  Final position: {'LONG' if final_pos==1 else 'SHORT' if final_pos==-1 else 'FLAT'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
