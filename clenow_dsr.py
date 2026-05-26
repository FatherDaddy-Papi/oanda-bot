"""DSR + PBO analysis for the Clenow-shaped momentum strategy.

Pre-declared grid (no iterative expansion):
    lookback   in {60, 90, 120, 180}
    ma_period  in {50, 100, 200}
    top_k      in {3, 5, 8}

That's 4 * 3 * 3 = 36 configs. State this upfront so DSR is honest.

Reports:
  - Per-config Sharpe (annualized, weekly observations)
  - Best config + its PSR (single-strategy) and DSR (corrected for N=36)
  - PBO (Probability of Backtest Overfitting), per Bailey-Borwein-LdP 2014:
        split observations into M chunks, for each of the C(M, M/2) ways of
        forming an in-sample/out-of-sample partition, find the in-sample best
        and check whether its OOS rank is in the bottom half. PBO = fraction
        of splits where the IS-best lands OOS-bottom-half. <= 0.5 is the
        no-better-than-coin-flip baseline; closer to 0 is better.
"""
import math
import itertools
from statistics import stdev

from clenow_momentum import (
    fetch_universe, backtest_portfolio, summarize,
)
from deflated_sharpe import (
    moments, sharpe_from_returns, psr, expected_max_sr,
)

LOOKBACKS = [60, 90, 120, 180]
MA_PERIODS = [50, 100, 200]
TOP_KS = [3, 5, 8]
GRID = [(lb, ma, k) for lb in LOOKBACKS for ma in MA_PERIODS for k in TOP_KS]


def pbo(returns_by_config, n_splits=14):
    """Probability of Backtest Overfitting via combinatorial CV.

    returns_by_config: list of (config, weekly_returns_list). All lists must
    have the same length (aligned weekly observations).
    n_splits: split into this many equal chunks; we form all C(M, M/2)
        in-sample/out-of-sample partitions.
    """
    configs = list(returns_by_config.keys())
    rets = {c: returns_by_config[c] for c in configs}
    T = min(len(r) for r in rets.values())
    if T < n_splits * 2:
        return float("nan")
    chunk = T // n_splits
    chunks = [(i * chunk, (i + 1) * chunk) for i in range(n_splits)]
    if T - chunks[-1][1] > 0:
        # extend last chunk to cover remainder
        chunks[-1] = (chunks[-1][0], T)

    def chunk_sharpe(rs, start, end):
        sub = rs[start:end]
        if len(sub) < 2:
            return 0.0
        m = sum(sub) / len(sub)
        v = sum((x - m) ** 2 for x in sub) / (len(sub) - 1)
        return m / math.sqrt(v) if v > 0 else 0.0

    half = n_splits // 2
    split_indices = list(range(n_splits))
    overfit_count = 0
    total = 0
    for is_idxs in itertools.combinations(split_indices, half):
        oos_idxs = [i for i in split_indices if i not in is_idxs]
        # in-sample Sharpe per config = avg of chunk Sharpes in IS chunks
        is_scores = {c: sum(chunk_sharpe(rets[c], *chunks[i]) for i in is_idxs) / half
                     for c in configs}
        oos_scores = {c: sum(chunk_sharpe(rets[c], *chunks[i]) for i in oos_idxs) / (n_splits - half)
                      for c in configs}
        best_is = max(configs, key=lambda c: is_scores[c])
        # rank of best_is in OOS, descending
        oos_ranked = sorted(configs, key=lambda c: oos_scores[c], reverse=True)
        rank = oos_ranked.index(best_is)
        if rank >= len(configs) / 2:
            overfit_count += 1
        total += 1
    return overfit_count / total if total else float("nan")


def main():
    bars = fetch_universe(years=5)
    print(f"\nRunning {len(GRID)}-config grid (pre-declared)...")
    results = {}
    for lb, ma, k in GRID:
        wr, eq = backtest_portfolio(bars, lookback=lb, ma_period=ma, top_k=k)
        s = summarize(wr, eq)
        results[(lb, ma, k)] = {"weekly_returns": wr, "summary": s,
                                "sharpe_weekly": sharpe_from_returns(wr) if wr else 0.0}

    # Rank by per-observation (weekly) Sharpe -- consistent with PSR/DSR math
    ranked = sorted(results.items(), key=lambda kv: kv[1]["sharpe_weekly"], reverse=True)
    print(f"\nTop 5 configs:")
    print(f"  {'lb':>4} {'ma':>4} {'k':>3}   {'SR week':>9} {'SR ann':>8} {'ret%':>8} {'DD%':>7} {'WR%':>6}")
    for (lb, ma, k), r in ranked[:5]:
        s = r["summary"]
        print(f"  {lb:>4} {ma:>4} {k:>3}   {r['sharpe_weekly']:>+9.4f} {s['sharpe_ann']:>+8.2f} "
              f"{s['total_return_pct']:>+8.1f} {s['max_dd_pct']:>7.2f} {s['win_rate']:>6.1f}")
    print(f"\nBottom 3 configs:")
    for (lb, ma, k), r in ranked[-3:]:
        s = r["summary"]
        print(f"  {lb:>4} {ma:>4} {k:>3}   {r['sharpe_weekly']:>+9.4f} {s['sharpe_ann']:>+8.2f} "
              f"{s['total_return_pct']:>+8.1f} {s['max_dd_pct']:>7.2f} {s['win_rate']:>6.1f}")

    # Best config -> PSR + DSR
    best_key, best = ranked[0]
    wr = best["weekly_returns"]
    _, _, skew, kurt = moments(wr) if wr else (0, 0, 0, 3)
    sr_best = best["sharpe_weekly"]
    n_obs = len(wr)
    trial_srs = [r["sharpe_weekly"] for r in results.values()]
    sr_variance = stdev(trial_srs) ** 2 if len(trial_srs) > 1 else 0.0
    sr_bench_null = expected_max_sr(sr_variance, len(GRID))
    psr_val = psr(sr_best, 0.0, n_obs, skew, kurt)
    dsr_val = psr(sr_best, sr_bench_null, n_obs, skew, kurt)

    # PBO
    rets_by_config = {k: v["weekly_returns"] for k, v in results.items()}
    pbo_val = pbo(rets_by_config, n_splits=14)

    print(f"\n{'=' * 60}")
    print(f"  Best config: lookback={best_key[0]}, ma={best_key[1]}, top_k={best_key[2]}")
    print(f"{'=' * 60}")
    print(f"  Weekly observations: {n_obs}")
    print(f"  Sharpe (weekly):     {sr_best:+.4f}")
    print(f"  Sharpe (annualized): {best['summary']['sharpe_ann']:+.2f}")
    print(f"  Total return:        {best['summary']['total_return_pct']:+.1f}%")
    print(f"  Max DD:              {best['summary']['max_dd_pct']:.2f}%")
    print(f"  skew: {skew:+.3f}   kurtosis: {kurt:.3f}")
    print(f"  Trials: {len(GRID)}   trial-SR variance: {sr_variance:.5f}")
    print(f"  E[max SR_null]:      {sr_bench_null:+.4f}")
    print(f"  PSR (P[true SR > 0]):                {psr_val:.3f}")
    print(f"  DSR (P[true SR > E[max_null]]):      {dsr_val:.3f}")
    print(f"  PBO (P[IS-best ranks OOS-bottom]):   {pbo_val:.3f}")
    print()
    dsr_verdict = "REAL (DSR > 0.95)" if dsr_val > 0.95 else \
                  "borderline (0.7 < DSR < 0.95)" if dsr_val > 0.7 else \
                  "weak (DSR < 0.7)"
    pbo_verdict = "BAD (PBO > 0.5, overfit)" if pbo_val > 0.5 else \
                  "OK (PBO < 0.5)" if pbo_val < 0.5 else "borderline"
    print(f"  DSR verdict: {dsr_verdict}")
    print(f"  PBO verdict: {pbo_verdict}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
