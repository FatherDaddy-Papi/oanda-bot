"""
Final tune: vary RSI thresholds, test at 1-pip AND 2-pip spread.
Goal: find a threshold combo that's profitable even with worse (more realistic) spread.
"""
import os
from dotenv import load_dotenv
from backtest_rsi import fetch_candles, backtest, stats

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

INSTRUMENT = "EUR_USD"
YEARS = 2
GRAN = "H1"
PIP = 0.0001

# (rsi_lo, rsi_hi) combos
THRESHOLDS = [(5, 95), (8, 92), (10, 90), (15, 85), (20, 80)]
SPREADS = [1.0, 1.5, 2.0]


def main():
    print(f"Fetching {YEARS}y {INSTRUMENT} {GRAN}...")
    bars = fetch_candles(INSTRUMENT, YEARS, GRAN)
    print(f"  {len(bars)} bars\n")

    print("=" * 80)
    print(f"  {'RSI thresholds':<16}  " + "  ".join(f"@{s}pip".rjust(22) for s in SPREADS))
    print("=" * 80)
    for lo, hi in THRESHOLDS:
        cells = []
        for spread in SPREADS:
            trades, eq, _ = backtest(bars, rsi_period=2, rsi_lo=lo, rsi_hi=hi,
                                      sma_period=5, trend_sma=200,
                                      spread_pips=spread, pip=PIP)
            s = stats(trades, eq)
            if s.get("n", 0) == 0:
                cells.append("  (no trades)".rjust(22))
            else:
                marker = "+" if s["profit_factor"] > 1.0 else "-"
                cells.append(f"{marker} n={s['n']:>4} PF={s['profit_factor']:.2f} P/L={s['total_pnl']:+6.1f}".rjust(22))
        print(f"  <{lo:<3}/>{hi:<3}        " + "  ".join(cells))
    print("=" * 80)
    print()
    print("(+ = profitable, - = losing; PF>1.0 = positive edge)")


if __name__ == "__main__":
    main()
