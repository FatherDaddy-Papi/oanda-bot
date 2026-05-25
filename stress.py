"""
Stress test the RSI(2)+SMA(200) strategy across:
  - Longer history (5 years on EUR_USD)
  - Different instruments (GBP_USD, USD_JPY, AUD_USD)
  - Worse spread (2 pips instead of 1)

A strategy that survives all of these has stronger claim to a real edge.
"""
import os
from dotenv import load_dotenv
from backtest_rsi import fetch_candles, backtest, stats

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PARAMS = dict(rsi_period=2, rsi_lo=10, rsi_hi=90, sma_period=5, trend_sma=200)

# (label, instrument, years, granularity, spread_pips, pip)
SCENARIOS = [
    ("EUR_USD H1 / 2y / 1 pip  (baseline)",   "EUR_USD", 2, "H1", 1.0, 0.0001),
    ("EUR_USD H1 / 5y / 1 pip  (longer history)", "EUR_USD", 5, "H1", 1.0, 0.0001),
    ("EUR_USD H1 / 2y / 2 pips (worse spread)",  "EUR_USD", 2, "H1", 2.0, 0.0001),
    ("GBP_USD H1 / 2y / 1.5 pips (different pair)", "GBP_USD", 2, "H1", 1.5, 0.0001),
    ("USD_JPY H1 / 2y / 1 pip  (JPY pair)",      "USD_JPY", 2, "H1", 1.0, 0.01),
    ("AUD_USD H1 / 2y / 1 pip  (commodity ccy)",  "AUD_USD", 2, "H1", 1.0, 0.0001),
]


def main():
    rows = []
    for label, inst, years, gran, spread, pip in SCENARIOS:
        print(f"Running: {label} ...", flush=True)
        try:
            bars = fetch_candles(inst, years, gran)
            trades, eq, _ = backtest(bars, **PARAMS, spread_pips=spread, pip=pip)
            s = stats(trades, eq)
            rows.append((label, s))
        except Exception as e:
            print(f"  ERROR: {e}")
            rows.append((label, None))
    print()
    print("=" * 95)
    print(f"  {'SCENARIO':<48} {'TRADES':>7} {'WIN%':>6} {'P/L':>9} {'PF':>5} {'DD':>7}")
    print("=" * 95)
    for label, s in rows:
        if s is None or s.get("n", 0) == 0:
            print(f"  {label:<48}  (no result)")
            continue
        verdict = "OK " if s["profit_factor"] > 1.0 else "BAD"
        print(f"  {label:<48} {s['n']:>7} {s['win_rate']:>5.1f}% {s['total_pnl']:>+9.2f} {s['profit_factor']:>5.2f} {s['max_dd']:>7.2f}  {verdict}")
    print("=" * 95)
    print()
    survived = sum(1 for _, s in rows if s and s.get("profit_factor", 0) > 1.0)
    total = sum(1 for _, s in rows if s)
    print(f"Survived ({survived}/{total} scenarios with PF > 1.0)")


if __name__ == "__main__":
    main()
