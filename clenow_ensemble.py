"""Ensemble version of Clenow momentum: averages trend strength across
multiple lookbacks to neutralize the parameter-pick risk that PBO 0.84
flagged on the single-best-config approach.

For each rebalance bar and each instrument, compute trend_strength at
each of LOOKBACKS = [60, 90, 120, 180] and average them. Rank by the
averaged signal; everything else (vol-parity weighting, ma filter,
weekly rebalance, friction) is unchanged.

Tests we run:
  1. Single ensemble run at a sensible default (ma=100, top_k=5).
  2. Compare its Sharpe against the original 36-grid E[max SR_null]
     -- this asks "does a robust signal beat the multi-test null?"
  3. Split-half: compute Sharpe on first half and second half
     independently. Consistent halves -> robust. Wildly different ->
     still fragile despite the averaging.
  4. Small leftover grid (ma x top_k = 9 configs) -> ensemble DSR
     and PBO. Now that lookback is averaged out, the residual grid
     measures the *remaining* overfitting risk only.
"""
import math
from statistics import stdev

from clenow_momentum import (
    fetch_universe, sma, atr_pct, trend_strength as ts_single,
    UNIVERSE, ATR_PERIOD, summarize,
)
from clenow_dsr import pbo as compute_pbo
from backtest_rsi import specs_for
from deflated_sharpe import (
    moments, sharpe_from_returns, psr, expected_max_sr,
)

LOOKBACKS = [60, 90, 120, 180]
RESIDUAL_MAS = [50, 100, 200]
RESIDUAL_TOP_KS = [3, 5, 8]


def ensemble_trend_strength(closes):
    """Average trend_strength across LOOKBACKS. Closes must contain
    at least max(LOOKBACKS) values."""
    if len(closes) < max(LOOKBACKS):
        return None
    scores = []
    for lb in LOOKBACKS:
        s = ts_single(closes[-lb:])
        scores.append(s)
    return sum(scores) / len(scores)


def backtest_ensemble(bars_by_inst, ma_period, top_k):
    insts = list(bars_by_inst.keys())
    ref = bars_by_inst[insts[0]]
    dates_ref = [b["time"][:10] for b in ref]
    bars_by_date = {inst: {b["time"][:10]: b for b in bars}
                    for inst, bars in bars_by_inst.items()}

    warmup = max(max(LOOKBACKS), ma_period) + 2
    held = {}
    weekly_returns = []
    equity = 1.0
    equity_curve = [equity]

    for i in range(warmup, len(ref) - 1, 5):
        rankings = []
        for inst in insts:
            d = dates_ref[i]
            if d not in bars_by_date[inst]:
                continue
            bars_inst = [b for b in bars_by_inst[inst] if b["time"][:10] <= d]
            if len(bars_inst) < warmup:
                continue
            closes = [b["c"] for b in bars_inst]
            cl = closes[-1]
            ts = ensemble_trend_strength(closes)
            if ts is None:
                continue
            ma = sma(closes, ma_period)
            if ma is None or cl <= ma:
                continue
            a = atr_pct(bars_inst, ATR_PERIOD)
            if a is None or a <= 0:
                continue
            rankings.append((inst, ts, cl, a))

        rankings.sort(key=lambda r: r[1], reverse=True)
        chosen = rankings[:top_k]
        if not chosen:
            weekly_returns.append(0.0)
            equity_curve.append(equity)
            continue

        inv_vols = [1.0 / r[3] for r in chosen]
        total_iv = sum(inv_vols)
        weights = {r[0]: iv / total_iv for r, iv in zip(chosen, inv_vols)}
        new_held = {r[0]: r[2] for r in chosen}

        i_next = min(i + 5, len(ref) - 1)
        wk_ret = 0.0
        for inst, w in weights.items():
            p_now = bars_by_date[inst].get(dates_ref[i])
            p_next = bars_by_date[inst].get(dates_ref[i_next])
            if p_now is None or p_next is None:
                continue
            raw_ret = (p_next["c"] - p_now["c"]) / p_now["c"]
            pip, spread_pips = specs_for(inst)
            spread_rel = (spread_pips * pip) / p_now["c"]
            entered_now = inst not in held
            friction = (spread_rel) if entered_now else 0.0
            wk_ret += w * (raw_ret - friction)

        weekly_returns.append(wk_ret)
        equity *= (1.0 + wk_ret)
        equity_curve.append(equity)
        held = new_held

    return weekly_returns, equity_curve


def split_half(returns):
    n = len(returns)
    mid = n // 2
    return returns[:mid], returns[mid:]


def sharpe_ann_weekly(rets):
    sr_w = sharpe_from_returns(rets)
    return sr_w * math.sqrt(52)


def main():
    bars = fetch_universe(years=5)

    print(f"\n--- Test 1: default ensemble (ma=100, top_k=5) ---")
    wr, eq = backtest_ensemble(bars, ma_period=100, top_k=5)
    s = summarize(wr, eq)
    print(f"  Sharpe (ann):    {s['sharpe_ann']:+.2f}")
    print(f"  Total return:    {s['total_return_pct']:+.1f}%")
    print(f"  Max DD:          {s['max_dd_pct']:.2f}%")
    print(f"  Weekly WR:       {s['win_rate']:.1f}%")
    print(f"  n obs:           {s['n']}")

    print(f"\n--- Test 2: split-half robustness (same default config) ---")
    h1, h2 = split_half(wr)
    sr1 = sharpe_ann_weekly(h1)
    sr2 = sharpe_ann_weekly(h2)
    print(f"  First half  (n={len(h1)}):  Sharpe (ann) {sr1:+.2f}")
    print(f"  Second half (n={len(h2)}):  Sharpe (ann) {sr2:+.2f}")
    diff = abs(sr1 - sr2)
    half_verdict = "ROBUST (halves within 0.5)" if diff < 0.5 else \
                   "borderline (halves within 1.0)" if diff < 1.0 else \
                   "FRAGILE (halves disagree by >1.0)"
    print(f"  Halves diff: {diff:.2f}  =>  {half_verdict}")

    print(f"\n--- Test 3: residual grid (ma x top_k = {len(RESIDUAL_MAS)*len(RESIDUAL_TOP_KS)} configs) ---")
    results = {}
    for ma in RESIDUAL_MAS:
        for k in RESIDUAL_TOP_KS:
            r, e = backtest_ensemble(bars, ma_period=ma, top_k=k)
            results[(ma, k)] = {
                "weekly_returns": r,
                "summary": summarize(r, e),
                "sharpe_weekly": sharpe_from_returns(r) if r else 0.0,
            }
    ranked = sorted(results.items(), key=lambda kv: kv[1]["sharpe_weekly"], reverse=True)
    print(f"  {'ma':>4} {'k':>3}   {'SR week':>9} {'SR ann':>8} {'ret%':>8} {'DD%':>7}")
    for (ma, k), r in ranked:
        s = r["summary"]
        print(f"  {ma:>4} {k:>3}   {r['sharpe_weekly']:>+9.4f} {s['sharpe_ann']:>+8.2f} "
              f"{s['total_return_pct']:>+8.1f} {s['max_dd_pct']:>7.2f}")

    print(f"\n--- Test 4: DSR + PBO on residual grid ---")
    best_key, best = ranked[0]
    wr_best = best["weekly_returns"]
    _, _, skew, kurt = moments(wr_best)
    sr_best = best["sharpe_weekly"]
    n_obs = len(wr_best)
    trial_srs = [r["sharpe_weekly"] for r in results.values()]
    sr_variance = stdev(trial_srs) ** 2 if len(trial_srs) > 1 else 0.0
    sr_bench_null = expected_max_sr(sr_variance, len(results))
    psr_val = psr(sr_best, 0.0, n_obs, skew, kurt)
    dsr_val = psr(sr_best, sr_bench_null, n_obs, skew, kurt)
    rets_by_config = {k: v["weekly_returns"] for k, v in results.items()}
    pbo_val = compute_pbo(rets_by_config, n_splits=14)
    print(f"  Best ensemble config: ma={best_key[0]}, top_k={best_key[1]}")
    print(f"  Sharpe ann:   {best['summary']['sharpe_ann']:+.2f}")
    print(f"  skew {skew:+.3f}  kurt {kurt:.3f}")
    print(f"  E[max SR_null] (per-week, N={len(results)}): {sr_bench_null:+.4f}")
    print(f"  PSR: {psr_val:.3f}")
    print(f"  DSR: {dsr_val:.3f}")
    print(f"  PBO: {pbo_val:.3f}")

    print(f"\n--- Test 5: compare default ensemble vs ORIGINAL 36-grid null ---")
    # E[max SR_null] from the original 36-grid (where lookback varied)
    # was 0.0772 weekly. The ensemble removes lookback dimension entirely,
    # so its "fair" null is much smaller. But we can still ask: does the
    # ensemble's central Sharpe exceed the original test's bar?
    default_sr_weekly = sharpe_from_returns(wr)
    print(f"  Default ensemble Sharpe (per-week): {default_sr_weekly:+.4f}")
    print(f"  Original 36-grid E[max SR_null]:    +0.0772")
    print(f"  Verdict: {'EXCEEDS old null bar' if default_sr_weekly > 0.0772 else 'fails old null bar'}")


if __name__ == "__main__":
    main()
