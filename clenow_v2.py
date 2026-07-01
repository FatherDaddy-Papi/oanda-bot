"""Clenow V2: correlation-capped cross-sectional momentum.

Motivation: the frozen Clenow ensemble FAILED its OOS test (2026-06-30).
Root cause (clenow_diagnose.py): vol-parity weighting concentrated the
book into correlated low-ATR US equity indices (>80% in 3 of 5 OOS
weeks). The in-sample DSR 0.985 was partly disguised equity beta.

The fix here is DELIBERATELY PARAMETER-FREE so it can't be fitted to the
OOS data we've already seen: at most ONE instrument per a-priori
asset-class group (highest ensemble trend strength in each group wins).
Groups are defined by domain knowledge, NOT by measuring return
correlations in the data. Everything else is identical to the frozen
ensemble, so this isolates the effect of the cap.

Pre-registered decisive test (write-down before results):
  - If in-sample DSR still clears 0.95 WITH the cap -> a real diversified
    edge exists; freeze V2 for a long (3-6 month) OOS.
  - If in-sample Sharpe collapses -> the frozen "edge" was concentration,
    nothing real underneath; close the book honestly.

Groups (a-priori by asset class):
  EQUITY:  NAS100_USD, SPX500_USD, US30_USD, DE30_EUR
  METALS:  XAU_USD, XAG_USD
  OIL:     BCO_USD, WTICO_USD
  NATGAS:  NATGAS_USD
  FX_ANTIUSD: EUR_USD, GBP_USD, AUD_USD
  FX_USD:  USD_JPY, USD_CAD
"""
import math
from statistics import stdev

from clenow_ensemble import (
    LOOKBACKS, ATR_PERIOD, ensemble_trend_strength, sma, atr_pct,
    fetch_universe, summarize, sharpe_ann_weekly,
)
from clenow_dsr import pbo as compute_pbo
from backtest_rsi import specs_for
from deflated_sharpe import moments, sharpe_from_returns, psr, expected_max_sr

GROUPS = {
    "NAS100_USD": "EQUITY", "SPX500_USD": "EQUITY", "US30_USD": "EQUITY", "DE30_EUR": "EQUITY",
    "XAU_USD": "METALS", "XAG_USD": "METALS",
    "BCO_USD": "OIL", "WTICO_USD": "OIL",
    "NATGAS_USD": "NATGAS",
    "EUR_USD": "FX_ANTIUSD", "GBP_USD": "FX_ANTIUSD", "AUD_USD": "FX_ANTIUSD",
    "USD_JPY": "FX_USD", "USD_CAD": "FX_USD",
}

RESIDUAL_MAS = [50, 100, 200]
RESIDUAL_TOP_KS = [3, 5, 8]


def group_of(inst):
    return GROUPS.get(inst, inst)  # unknown instrument = its own group


def backtest_capped(bars_by_inst, ma_period, top_k):
    """Identical to clenow_ensemble.backtest_ensemble EXCEPT: when choosing
    the top_k, skip any instrument whose group is already represented."""
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

        # GROUP CAP: greedily take highest-ts instruments, one per group
        chosen = []
        used_groups = set()
        for r in rankings:
            g = group_of(r[0])
            if g in used_groups:
                continue
            chosen.append(r)
            used_groups.add(g)
            if len(chosen) >= top_k:
                break

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
            friction = spread_rel if entered_now else 0.0
            wk_ret += w * (raw_ret - friction)

        weekly_returns.append(wk_ret)
        equity *= (1.0 + wk_ret)
        equity_curve.append(equity)
        held = new_held

    return weekly_returns, equity_curve


def main():
    bars = fetch_universe(years=5)

    print(f"\n--- Clenow V2 (group-capped) default ma=100, top_k=5 ---")
    wr, eq = backtest_capped(bars, 100, 5)
    s = summarize(wr, eq)
    print(f"  Sharpe (ann):   {s['sharpe_ann']:+.2f}")
    print(f"  Total return:   {s['total_return_pct']:+.1f}%")
    print(f"  Max DD:         {s['max_dd_pct']:.2f}%")
    print(f"  Weekly WR:      {s['win_rate']:.1f}%   n={s['n']}")

    print(f"\n--- Split-half robustness ---")
    mid = len(wr) // 2
    sr1, sr2 = sharpe_ann_weekly(wr[:mid]), sharpe_ann_weekly(wr[mid:])
    print(f"  First half  Sharpe (ann): {sr1:+.2f}")
    print(f"  Second half Sharpe (ann): {sr2:+.2f}   (diff {abs(sr1-sr2):.2f})")

    print(f"\n--- Residual grid (ma x top_k = {len(RESIDUAL_MAS)*len(RESIDUAL_TOP_KS)}) ---")
    results = {}
    for ma in RESIDUAL_MAS:
        for k in RESIDUAL_TOP_KS:
            r, e = backtest_capped(bars, ma, k)
            results[(ma, k)] = {"weekly_returns": r, "summary": summarize(r, e),
                                "sharpe_weekly": sharpe_from_returns(r) if r else 0.0}
    ranked = sorted(results.items(), key=lambda kv: kv[1]["sharpe_weekly"], reverse=True)
    print(f"  {'ma':>4}{'k':>3}   {'SR wk':>8}{'SR ann':>8}{'ret%':>8}{'DD%':>7}")
    for (ma, k), r in ranked:
        s = r["summary"]
        print(f"  {ma:>4}{k:>3}   {r['sharpe_weekly']:>+8.4f}{s['sharpe_ann']:>+8.2f}"
              f"{s['total_return_pct']:>+8.1f}{s['max_dd_pct']:>7.2f}")

    print(f"\n--- DSR + PBO on residual grid ---")
    best_key, best = ranked[0]
    wr_best = best["weekly_returns"]
    _, _, skew, kurt = moments(wr_best)
    sr_best = best["sharpe_weekly"]
    n_obs = len(wr_best)
    trial_srs = [r["sharpe_weekly"] for r in results.values()]
    sr_var = stdev(trial_srs) ** 2 if len(trial_srs) > 1 else 0.0
    sr_bench = expected_max_sr(sr_var, len(results))
    psr_val = psr(sr_best, 0.0, n_obs, skew, kurt)
    dsr_val = psr(sr_best, sr_bench, n_obs, skew, kurt)
    pbo_val = compute_pbo({k: v["weekly_returns"] for k, v in results.items()}, n_splits=14)
    print(f"  best: ma={best_key[0]}, top_k={best_key[1]}   Sharpe ann {best['summary']['sharpe_ann']:+.2f}")
    print(f"  skew {skew:+.3f}  kurt {kurt:.3f}")
    print(f"  PSR: {psr_val:.3f}   DSR: {dsr_val:.3f}   PBO: {pbo_val:.3f}")

    print(f"\n{'='*60}")
    print(f"  V2 vs frozen Clenow (was: DSR 0.985, PBO 0.398, ann ~1.39)")
    print(f"{'='*60}")
    verdict = "EDGE SURVIVES CAP" if (dsr_val > 0.95 and pbo_val < 0.5) else \
              "EDGE COLLAPSES -- was concentration"
    print(f"  Verdict: {verdict}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
