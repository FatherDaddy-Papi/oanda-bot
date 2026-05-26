"""Deflated Sharpe analysis for Donchian breakout strategy.

Same statistical framework as deflated_sharpe.py (Bailey-Lopez de Prado),
applied to the Donchian backtester instead of RSI. Reuses the math
helpers from deflated_sharpe to keep things honest and comparable.

Usage:
    python donchian_dsr.py [INSTRUMENT] [YEARS]
    python donchian_dsr.py              # runs all 3 survivors, 2y H1
"""
import os
import sys
import math
from statistics import stdev
from dotenv import load_dotenv

from backtest_rsi import fetch_candles, stats, specs_for
from backtest_donchian import backtest, UNITS
from deflated_sharpe import (
    moments, sharpe_from_returns, psr, expected_max_sr,
)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Grid — mirrors the size of the RSI grid (~36 configs) for fair DSR comparison
N_ENTRIES = [10, 20, 40, 55]
N_EXITS = [5, 10, 20]
STOP_ATRS = [1.5, 2.0, 3.0]


def trade_returns(trades):
    rets = []
    for t in trades:
        notional = t["entry"] * UNITS
        if notional <= 0:
            continue
        rets.append(t["pnl"] / notional)
    return rets


def evaluate(bars, n_entry, n_exit, stop_atr, spread_pips, pip):
    if n_exit >= n_entry:
        return None  # skip invalid combos (exit must be shorter than entry)
    trades, eq, _ = backtest(bars, n_entry, n_exit, stop_atr,
                             spread_pips=spread_pips, pip=pip)
    s = stats(trades, eq)
    rets = trade_returns(trades)
    sr = sharpe_from_returns(rets) if rets else 0.0
    return {"sr": sr, "n": s.get("n", 0), "pf": s.get("profit_factor", 0),
            "rets": rets, "params": (n_entry, n_exit, stop_atr)}


def run(instrument, years):
    pip, spread = specs_for(instrument)
    print(f"\n[{instrument}] fetching {years}y H1 candles...")
    bars = fetch_candles(instrument, years, "H1")
    print(f"  {len(bars)} bars; spread={spread} pips, pip={pip}")

    results = []
    for ne in N_ENTRIES:
        for nx in N_EXITS:
            for sa in STOP_ATRS:
                r = evaluate(bars, ne, nx, sa, spread, pip)
                if r is not None:
                    results.append(r)
    print(f"  grid: {len(results)} valid configs (exit < entry)")

    results.sort(key=lambda r: r["sr"], reverse=True)
    best = results[0]
    n_trials = len(results)
    trial_srs = [r["sr"] for r in results]
    sr_variance = stdev(trial_srs) ** 2 if len(trial_srs) > 1 else 0.0
    sr_bench_null = expected_max_sr(sr_variance, n_trials)

    rets = best["rets"]
    _, _, skew, kurt = moments(rets) if rets else (0, 0, 0, 3)
    n_obs = len(rets)
    sr_best = best["sr"]
    psr_value = psr(sr_best, 0.0, n_obs, skew, kurt) if n_obs > 1 else float("nan")
    dsr_value = psr(sr_best, sr_bench_null, n_obs, skew, kurt) if n_obs > 1 else float("nan")

    if n_obs > 1 and years > 0:
        trades_per_year = n_obs / years
        sr_ann = sr_best * math.sqrt(trades_per_year)
    else:
        sr_ann = 0.0

    ne, nx, sa = best["params"]
    print(f"\n  best: Donchian({ne}/{nx}), stop {sa}*ATR")
    print(f"  trades: {best['n']}   PF: {best['pf']:.2f}")
    print(f"  Sharpe (per-trade):  {sr_best:+.4f}")
    print(f"  Sharpe (annualized): {sr_ann:+.2f}")
    print(f"  skew: {skew:+.3f}   kurtosis: {kurt:.3f}")
    print(f"  trials: {n_trials}   trial-SR variance: {sr_variance:.5f}")
    print(f"  E[max SR_null] (per-trade): {sr_bench_null:+.4f}")
    print(f"  PSR (P[true SR > 0]):           {psr_value:.3f}")
    print(f"  DSR (P[true SR > E[max_null]]): {dsr_value:.3f}")
    verdict = "REAL EDGE (>0.95)" if dsr_value > 0.95 else \
              "PROBABLY NOT REAL (<0.95)"
    print(f"  Verdict: {verdict}")

    return {"instrument": instrument, "best": best, "psr": psr_value, "dsr": dsr_value,
            "sr_ann": sr_ann, "n_trials": n_trials}


def main():
    instrument = sys.argv[1] if len(sys.argv) > 1 else None
    years = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    instruments = [instrument] if instrument else ["EUR_USD", "NAS100_USD", "GBP_JPY"]
    summary = [run(inst, years) for inst in instruments]
    print("\n" + "=" * 70)
    print(f"  {'instrument':<12} {'best SR (ann)':>14} {'PSR':>8} {'DSR':>8}   verdict")
    print("=" * 70)
    for r in summary:
        v = "REAL" if r["dsr"] > 0.95 else "weak/none"
        print(f"  {r['instrument']:<12} {r['sr_ann']:>+14.2f} {r['psr']:>8.3f} {r['dsr']:>8.3f}   {v}")
    print("=" * 70)


if __name__ == "__main__":
    main()
