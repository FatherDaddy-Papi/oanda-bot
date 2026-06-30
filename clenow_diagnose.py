"""Diagnose the OOS losing week (2026-06-02 rebalance, -3.80%).

Re-runs the frozen ensemble selection logic for the OOS rebalances and,
for the target week, prints exactly what was held, the vol-parity
weights, and each instrument's contribution to the weekly return. The
question we're answering: was the -3.80% a single freak gap within the
strategy's normal risk envelope, or a structural flaw (e.g. vol-parity
concentrating into whatever just got volatile)?

Uses the frozen signal functions verbatim. Does not touch
clenow_ensemble.py.
"""
import math

from clenow_ensemble import (
    LOOKBACKS, ATR_PERIOD, ensemble_trend_strength, sma, atr_pct,
    fetch_universe,
)
from backtest_rsi import specs_for

MA_PERIOD = 100
TOP_K = 5
FREEZE_DATE = "2026-05-26"


def walk(bars_by_inst, ma_period, top_k, focus_from=FREEZE_DATE):
    insts = list(bars_by_inst.keys())
    ref = bars_by_inst[insts[0]]
    dates_ref = [b["time"][:10] for b in ref]
    bars_by_date = {inst: {b["time"][:10]: b for b in bars}
                    for inst, bars in bars_by_inst.items()}
    warmup = max(max(LOOKBACKS), ma_period) + 2
    held = {}

    for i in range(warmup, len(ref) - 1, 5):
        d = dates_ref[i]
        rankings = []
        for inst in insts:
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
        i_next = min(i + 5, len(ref) - 1)
        d_next = dates_ref[i_next]

        if chosen:
            inv_vols = [1.0 / r[3] for r in chosen]
            tiv = sum(inv_vols)
            weights = {r[0]: iv / tiv for r, iv in zip(chosen, inv_vols)}
            new_held = {r[0]: r[2] for r in chosen}
        else:
            weights = {}
            new_held = {}

        if d >= focus_from:
            wk_ret = 0.0
            rows = []
            for (inst, ts, cl, a) in chosen:
                w = weights[inst]
                p_now = bars_by_date[inst].get(d)
                p_next = bars_by_date[inst].get(d_next)
                if p_now is None or p_next is None:
                    continue
                raw = (p_next["c"] - p_now["c"]) / p_now["c"]
                pip, spread_pips = specs_for(inst)
                spread_rel = (spread_pips * pip) / p_now["c"]
                entered = inst not in held
                fric = spread_rel if entered else 0.0
                contrib = w * (raw - fric)
                wk_ret += contrib
                rows.append((inst, w, a, raw, "new" if entered else "roll", contrib))
            print(f"\n=== rebalance {d} -> {d_next}   week return {wk_ret*100:+.2f}% ===")
            print(f"  {'inst':<12}{'weight':>8}{'atr%':>8}{'raw%':>8}{'flag':>6}{'contrib%':>10}")
            for inst, w, a, raw, flag, contrib in sorted(rows, key=lambda x: x[5]):
                print(f"  {inst:<12}{w*100:>7.1f}%{a*100:>7.2f}%{raw*100:>7.2f}%{flag:>6}{contrib*100:>9.2f}%")

        held = new_held


def main():
    bars = fetch_universe(years=5)
    walk(bars, MA_PERIOD, TOP_K)


if __name__ == "__main__":
    main()
