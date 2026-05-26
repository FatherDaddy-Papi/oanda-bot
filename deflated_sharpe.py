"""Deflated Sharpe Ratio for the RSI(2)+SMA strategy.

Implements Bailey & Lopez de Prado (2014) "The Deflated Sharpe Ratio".

Two-step honesty check:
  1. PSR  — Probabilistic Sharpe Ratio. Given the observed SR, sample size,
            and skew/kurtosis of returns, what's the probability the *true*
            SR exceeds the benchmark? (Single-strategy.)
  2. DSR  — Deflated Sharpe. Same idea, but the benchmark is raised to
            E[max SR_null] across the N strategies we tried, correcting
            for backtest overfitting bias.

Returns are computed per-trade (one observation per closed trade), which
is the form used in the original paper for bet-by-bet strategies.

Usage:
    python deflated_sharpe.py [INSTRUMENT] [YEARS]
    python deflated_sharpe.py EUR_USD 2
"""
import os
import sys
import math
from statistics import mean, stdev
from dotenv import load_dotenv
from backtest_rsi import fetch_candles, backtest, stats, specs_for

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Grid we'll search. Keep this list explicit and audited — N matters for DSR.
RSI_THRESHOLDS = [(5, 95), (8, 92), (10, 90), (15, 85), (20, 80)]
SMA_PERIODS = [3, 5, 8]
TREND_SMAS = [0, 100, 200]
RSI_PERIOD = 2  # fixed — varying this too would explode N without much gain

UNITS = 1000  # match backtest_rsi.UNITS for return scale
EULER_MASCHERONI = 0.5772156649015329


# ---------- normal distribution helpers (no scipy dep) ----------

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p):
    """Inverse normal CDF via Beasley-Springer-Moro approximation."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0,1), got {p}")
    a = [-3.969683028665376e+01,  2.209460984245205e+02,
         -2.759285104469687e+02,  1.383577518672690e+02,
         -3.066479806614716e+01,  2.506628277459239e+00]
    b = [-5.447609879822406e+01,  1.615858368580409e+02,
         -1.556989798598866e+02,  6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
          4.374664141464968e+00,  2.938163982698783e+00]
    d = [ 7.784695709041462e-03,  3.224671290700398e-01,
          2.445134137142996e+00,  3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
            ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)


# ---------- moments ----------

def moments(xs):
    n = len(xs)
    if n < 4:
        return 0.0, 0.0, 0.0, 3.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    if var <= 0:
        return m, 0.0, 0.0, 3.0
    sd = math.sqrt(var)
    skew = sum(((x - m) / sd) ** 3 for x in xs) / n
    kurt = sum(((x - m) / sd) ** 4 for x in xs) / n  # raw (not excess)
    return m, sd, skew, kurt


def sharpe_from_returns(rets):
    """Per-observation Sharpe (not annualized). Returns 0 if degenerate."""
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    if var <= 0:
        return 0.0
    return m / math.sqrt(var)


# ---------- PSR / DSR ----------

def psr(sr_obs, sr_bench, n_obs, skew, kurt):
    """Probability that true SR > sr_bench, given sample SR and higher moments.

    sr_obs, sr_bench are per-observation (same frequency). Returns prob in [0,1].
    """
    denom_sq = 1.0 - skew * sr_obs + ((kurt - 1.0) / 4.0) * sr_obs ** 2
    if denom_sq <= 0 or n_obs < 2:
        return float("nan")
    z = (sr_obs - sr_bench) * math.sqrt(n_obs - 1) / math.sqrt(denom_sq)
    return norm_cdf(z)


def expected_max_sr(trial_sr_variance, n_trials):
    """E[max SR_null] across n_trials, per Bailey-Lopez de Prado eq 11.

    Assumes trial SRs ~ N(0, trial_sr_variance) under null. Uses
    Gumbel-based approximation of the maximum of n iid normals.
    """
    if n_trials < 2 or trial_sr_variance <= 0:
        return 0.0
    sd = math.sqrt(trial_sr_variance)
    z1 = norm_ppf(1 - 1.0 / n_trials)
    z2 = norm_ppf(1 - 1.0 / (n_trials * math.e))
    return sd * ((1 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)


# ---------- main pipeline ----------

def trade_returns(trades):
    """Per-trade fractional return on a fixed notional (UNITS at entry price)."""
    rets = []
    for t in trades:
        notional = t["entry"] * UNITS
        if notional <= 0:
            continue
        rets.append(t["pnl"] / notional)
    return rets


def evaluate(bars, rsi_lo, rsi_hi, sma_p, trend_sma, spread_pips, pip):
    trades, eq, _ = backtest(bars, rsi_period=RSI_PERIOD, rsi_lo=rsi_lo, rsi_hi=rsi_hi,
                             sma_period=sma_p, trend_sma=trend_sma,
                             spread_pips=spread_pips, pip=pip)
    s = stats(trades, eq)
    rets = trade_returns(trades)
    sr = sharpe_from_returns(rets) if rets else 0.0
    return {"sr": sr, "n": s.get("n", 0), "pf": s.get("profit_factor", 0),
            "rets": rets, "trades": trades,
            "params": (rsi_lo, rsi_hi, sma_p, trend_sma)}


def run(instrument, years):
    pip, spread = specs_for(instrument)
    print(f"\n[{instrument}] fetching {years}y H1 candles...")
    bars = fetch_candles(instrument, years, "H1")
    print(f"  {len(bars)} bars; spread={spread} pips, pip={pip}")
    print(f"  grid: {len(RSI_THRESHOLDS)} thresholds x {len(SMA_PERIODS)} SMA x {len(TREND_SMAS)} trend = "
          f"{len(RSI_THRESHOLDS)*len(SMA_PERIODS)*len(TREND_SMAS)} configs")

    results = []
    for lo, hi in RSI_THRESHOLDS:
        for sma_p in SMA_PERIODS:
            for trend in TREND_SMAS:
                results.append(evaluate(bars, lo, hi, sma_p, trend, spread, pip))

    results.sort(key=lambda r: r["sr"], reverse=True)
    best = results[0]
    n_trials = len(results)
    trial_srs = [r["sr"] for r in results]
    sr_variance = stdev(trial_srs) ** 2 if len(trial_srs) > 1 else 0.0
    sr_bench_null = expected_max_sr(sr_variance, n_trials)

    # PSR ignores multi-trial penalty (benchmark = 0)
    # DSR raises the bar to E[max SR] under null
    rets = best["rets"]
    _, _, skew, kurt = moments(rets) if rets else (0, 0, 0, 3)
    n_obs = len(rets)
    sr_best = best["sr"]
    psr_value = psr(sr_best, 0.0, n_obs, skew, kurt) if n_obs > 1 else float("nan")
    dsr_value = psr(sr_best, sr_bench_null, n_obs, skew, kurt) if n_obs > 1 else float("nan")

    # Annualized SR for human readability — trades/year * sqrt of per-trade
    if n_obs > 1 and years > 0:
        trades_per_year = n_obs / years
        sr_ann = sr_best * math.sqrt(trades_per_year)
    else:
        sr_ann = 0.0

    print(f"\n  best params: RSI<{best['params'][0]}/>{best['params'][1]}, "
          f"SMA({best['params'][2]}), trend SMA({best['params'][3]})")
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
    summary = []
    for inst in instruments:
        summary.append(run(inst, years))
    print("\n" + "=" * 70)
    print(f"  {'instrument':<12} {'best SR (ann)':>14} {'PSR':>8} {'DSR':>8}   verdict")
    print("=" * 70)
    for r in summary:
        v = "REAL" if r["dsr"] > 0.95 else "weak/none"
        print(f"  {r['instrument']:<12} {r['sr_ann']:>+14.2f} {r['psr']:>8.3f} {r['dsr']:>8.3f}   {v}")
    print("=" * 70)


if __name__ == "__main__":
    main()
