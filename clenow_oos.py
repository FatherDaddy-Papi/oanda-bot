"""Out-of-sample test for the frozen Clenow ensemble.

Freeze was set 2026-05-26 (commit 57de419). Frozen config: lookback
ensemble {60,90,120,180}, ma_period=100, top_k=5. OOS was scheduled
for 2026-06-23; running 2026-06-30. The ~5 weeks of D1 bars after the
freeze date are data the model never saw during development.

Method:
  1. Fetch the universe through today.
  2. Re-host the EXACT frozen rebalance loop (importing the frozen
     signal functions verbatim) but record the rebalance date of each
     weekly return.
  3. FAITHFULNESS CHECK: assert the re-hosted weekly returns match the
     frozen backtest_ensemble() output element-for-element. If they
     diverge, abort -- the date tags can't be trusted.
  4. Split returns at the freeze date. Report in-sample vs OOS Sharpe.

Pass criterion (from REAL_MONEY_GATES.md Gate 3):
  OOS weekly Sharpe >= 0.10, degradation < 50% from in-sample.

IMPORTANT honesty note: ~5 weeks = ~5 weekly observations. That is a
severely underpowered sample. Treat the result as a directional sanity
check, NOT a verdict. A real OOS verdict needs months of forward data.
"""
import math

from clenow_ensemble import (
    LOOKBACKS, ATR_PERIOD, ensemble_trend_strength, sma, atr_pct,
    backtest_ensemble, fetch_universe, sharpe_ann_weekly,
)
from backtest_rsi import specs_for
from deflated_sharpe import sharpe_from_returns

FREEZE_DATE = "2026-05-26"   # set in REAL_MONEY_GATES.md Gate 3
MA_PERIOD = 100              # frozen
TOP_K = 5                    # frozen


def backtest_ensemble_dated(bars_by_inst, ma_period, top_k):
    """EXACT copy of clenow_ensemble.backtest_ensemble, but also returns
    the rebalance date for each weekly return. Faithfulness is verified
    against the frozen function before the dates are used."""
    insts = list(bars_by_inst.keys())
    ref = bars_by_inst[insts[0]]
    dates_ref = [b["time"][:10] for b in ref]
    bars_by_date = {inst: {b["time"][:10]: b for b in bars}
                    for inst, bars in bars_by_inst.items()}

    warmup = max(max(LOOKBACKS), ma_period) + 2
    held = {}
    weekly_returns = []
    rebalance_dates = []
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
            rebalance_dates.append(dates_ref[i])
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
        rebalance_dates.append(dates_ref[i])
        equity *= (1.0 + wk_ret)
        equity_curve.append(equity)
        held = new_held

    return weekly_returns, rebalance_dates


def main():
    bars = fetch_universe(years=5)

    # Faithfulness check: re-host must match frozen function exactly.
    frozen_wr, _ = backtest_ensemble(bars, MA_PERIOD, TOP_K)
    wr, dates = backtest_ensemble_dated(bars, MA_PERIOD, TOP_K)
    if len(wr) != len(frozen_wr):
        print(f"ABORT: length mismatch (re-host {len(wr)} vs frozen {len(frozen_wr)})")
        return
    max_diff = max((abs(a - b) for a, b in zip(wr, frozen_wr)), default=0.0)
    if max_diff > 1e-12:
        print(f"ABORT: re-host diverges from frozen logic (max diff {max_diff:.2e})")
        return
    print(f"Faithfulness check OK (re-host matches frozen, max diff {max_diff:.1e})")

    # Split at freeze date
    in_sample = [r for r, d in zip(wr, dates) if d < FREEZE_DATE]
    oos = [r for r, d in zip(wr, dates) if d >= FREEZE_DATE]
    oos_dates = [d for d in dates if d >= FREEZE_DATE]

    print(f"\nTotal rebalances: {len(wr)}  ({dates[0]} -> {dates[-1]})")
    print(f"In-sample (< {FREEZE_DATE}): {len(in_sample)} weeks")
    print(f"Out-of-sample (>= {FREEZE_DATE}): {len(oos)} weeks  {oos_dates}")

    is_sr_w = sharpe_from_returns(in_sample) if len(in_sample) > 1 else 0.0
    oos_sr_w = sharpe_from_returns(oos) if len(oos) > 1 else 0.0

    print(f"\n{'='*60}")
    print(f"  OOS TEST RESULT (frozen ensemble ma=100, top_k=5)")
    print(f"{'='*60}")
    print(f"  In-sample weekly Sharpe:  {is_sr_w:+.4f}  (ann {sharpe_ann_weekly(in_sample):+.2f})")
    print(f"  OOS weekly Sharpe:        {oos_sr_w:+.4f}  (ann {sharpe_ann_weekly(oos):+.2f})")
    if len(oos) > 0:
        oos_total = 1.0
        for r in oos:
            oos_total *= (1.0 + r)
        print(f"  OOS cumulative return:    {(oos_total-1)*100:+.2f}%  over {len(oos)} weeks")
        print(f"  OOS weekly returns:       {[round(r*100,2) for r in oos]} (%)")
    degradation = (1 - oos_sr_w / is_sr_w) * 100 if is_sr_w > 0 else float('nan')
    print(f"  Degradation vs in-sample: {degradation:.0f}%")
    print(f"{'='*60}")
    print(f"  Pass criteria: OOS weekly Sharpe >= 0.10 AND degradation < 50%")
    passed = oos_sr_w >= 0.10 and (is_sr_w > 0 and degradation < 50)
    print(f"  Mechanical verdict: {'PASS' if passed else 'FAIL / INCONCLUSIVE'}")
    print(f"{'='*60}")
    print(f"\n  *** n={len(oos)} weekly observations is severely underpowered. ***")
    print(f"  A single week dominates the Sharpe. This is a directional")
    print(f"  sanity check, NOT a statistical verdict. Real confidence")
    print(f"  needs 3-6+ months of forward data.")


if __name__ == "__main__":
    main()
