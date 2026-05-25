"""
Out-of-sample validation for RSI(2)+SMA(200) on EUR_USD H1.

Splits 2 years of data 50/50:
  In-sample  (older year): training period — the data we'd hypothetically have used to design rules
  Out-of-sample (recent year): unseen data — the real test

If the edge holds in both halves, it's more likely to be real.
If only the in-sample is profitable, we curve-fit.

Usage:
    python validate.py
"""
import os
from dotenv import load_dotenv
from oandapyV20 import API
from backtest_rsi import fetch_candles, backtest, stats

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PARAMS = dict(rsi_period=2, rsi_lo=10, rsi_hi=90, sma_period=5, trend_sma=200)
INSTRUMENT = "EUR_USD"
GRAN = "H1"
YEARS = 2


def report(label, trades, eq):
    s = stats(trades, eq)
    print(f"--- {label} ---")
    if s["n"] == 0:
        print("  No trades.")
        return
    print(f"  Trades:        {s['n']}")
    print(f"  Win rate:      {s['win_rate']:.1f}%  ({s['wins']}/{s['losses']})")
    print(f"  Net P/L:       {s['total_pnl']:+,.2f}")
    print(f"  Expectancy:    {s['expectancy']:+,.4f}/trade")
    print(f"  Profit factor: {s['profit_factor']:.2f}")
    print(f"  Max drawdown:  {s['max_dd']:,.2f}")
    print(f"  Avg win/loss:  +{s['avg_win']:.2f} / {s['avg_loss']:.2f}")


def main():
    print(f"Fetching {YEARS}y of {INSTRUMENT} {GRAN}...")
    bars = fetch_candles(INSTRUMENT, YEARS, GRAN)
    print(f"  {len(bars)} bars")
    mid = len(bars) // 2
    in_sample = bars[:mid]
    out_sample = bars[mid:]
    print(f"  In-sample:  {in_sample[0]['time'][:10]} .. {in_sample[-1]['time'][:10]}  ({len(in_sample)} bars)")
    print(f"  Out-sample: {out_sample[0]['time'][:10]} .. {out_sample[-1]['time'][:10]}  ({len(out_sample)} bars)")
    print()

    print(f"Strategy: RSI({PARAMS['rsi_period']})<{PARAMS['rsi_lo']}/>{PARAMS['rsi_hi']}, "
          f"exit SMA({PARAMS['sma_period']}), trend SMA({PARAMS['trend_sma']})")
    print()

    is_trades, is_eq, _ = backtest(in_sample, **PARAMS)
    oos_trades, oos_eq, _ = backtest(out_sample, **PARAMS)

    report("IN-SAMPLE (year 1)", is_trades, is_eq)
    print()
    report("OUT-OF-SAMPLE (year 2)", oos_trades, oos_eq)
    print()
    print("=" * 50)
    print("VERDICT")
    print("=" * 50)
    is_s = stats(is_trades, is_eq)
    oos_s = stats(oos_trades, oos_eq)
    if is_s["n"] == 0 or oos_s["n"] == 0:
        print("  Not enough trades to judge.")
        return
    is_ok = is_s["profit_factor"] > 1.0
    oos_ok = oos_s["profit_factor"] > 1.0
    if is_ok and oos_ok:
        print("  Both halves profitable. Edge is plausible.")
    elif is_ok and not oos_ok:
        print("  In-sample profitable, out-of-sample NOT. Likely overfit / regime change.")
    elif not is_ok and oos_ok:
        print("  Out-of-sample profitable but in-sample wasn't. Lucky, or recent regime favors us.")
    else:
        print("  Neither half profitable. No real edge.")


if __name__ == "__main__":
    main()
