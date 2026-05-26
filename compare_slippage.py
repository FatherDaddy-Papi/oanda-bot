"""Compare backtest results under old (broken) vs new (per-instrument) slippage.

Same strategy + same bars; only the spread/pip assumption changes. This isolates
how much of the prior edge was an artifact of modeling ~zero spread on metals,
indices, and JPY pairs.
"""
import os
from dotenv import load_dotenv
from backtest_rsi import fetch_candles, backtest, stats, specs_for

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

INSTRUMENTS = ["EUR_USD", "XAU_USD", "NAS100_USD", "XAG_USD", "GBP_JPY"]
YEARS = 2
GRAN = "H1"
PARAMS = dict(rsi_period=2, rsi_lo=10, rsi_hi=90, sma_period=5, trend_sma=200)

OLD_SPREAD_PIPS = 1.0
OLD_PIP = 0.0001


def fmt(s):
    if not s or s.get("n", 0) == 0:
        return "no trades"
    return f"n={s['n']:>3} PF={s['profit_factor']:5.2f} P/L={s['total_pnl']:+9.2f} DD={s['max_dd']:7.2f} win={s['win_rate']:4.1f}%"


def main():
    print(f"{'instrument':<12} {'OLD (1pip flat, pip=0.0001)':<55}  {'NEW (per-instrument real)':<55}")
    print("-" * 125)
    for inst in INSTRUMENTS:
        bars = fetch_candles(inst, YEARS, GRAN)
        old_trades, old_eq, _ = backtest(bars, **PARAMS, spread_pips=OLD_SPREAD_PIPS, pip=OLD_PIP)
        pip, spread = specs_for(inst)
        new_trades, new_eq, _ = backtest(bars, **PARAMS, spread_pips=spread, pip=pip)
        os_, ns = stats(old_trades, old_eq), stats(new_trades, new_eq)
        print(f"{inst:<12} {fmt(os_):<55}  {fmt(ns):<55}")
    print()
    print("Note: NEW uses single off-peak spread snapshot (2026-05-26).")
    print("If a strategy is BAD under NEW but OK under OLD, that PF was a slippage artifact.")


if __name__ == "__main__":
    main()
