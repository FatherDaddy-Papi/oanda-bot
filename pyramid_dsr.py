"""Rigorous DSR/PBO test of the Turtle pyramid strategy on commodities.

The sanity check (backtest_pyramid.py) showed PF 1.0-1.8 on commodities.
This subjects it to the same rigor that judged Clenow: pre-declared grid,
correlation cap, DSR + PBO, and a concentration check.

Correlation cap (a-priori, before looking at results):
  BCO and WTICO are both crude oil (~0.95 corr) -- counting both is the
  Clenow mistake. De-duplicated commodity basket:
      {XAU_USD, XAG_USD, BCO_USD, NATGAS_USD}   (one oil, not two)
  Portfolio = equal-weight across these 4, so no single name dominates.

Pre-declared grid (18 configs):
  n_entry     in {20, 40, 55}
  pyramid_step in {0.5, 1.0}
  stop_atr    in {1.5, 2.0, 3.0}
  n_exit = n_entry // 2  (fixed rule)   max_units = 4 (fixed)

Method: run pyramid per instrument -> per-trade returns -> monthly return
series per instrument -> equal-weight portfolio monthly series (aligned
calendar) -> per-config monthly Sharpe -> DSR on best (18 trials) + PBO.

Concentration check: report each instrument's standalone PF/Sharpe for the
best config. If the edge is one instrument (e.g. NATGAS), it's a fluke,
not a robust commodity edge -- same trap as Clenow.
"""
import math
from collections import defaultdict
from statistics import stdev

from backtest_pyramid import backtest, UNITS_PER_RISK_UNIT
from backtest_rsi import fetch_candles, specs_for, stats
from deflated_sharpe import moments, sharpe_from_returns, psr, expected_max_sr
from clenow_dsr import pbo as compute_pbo

BASKET = ["XAU_USD", "XAG_USD", "BCO_USD", "NATGAS_USD"]  # WTICO dropped (redundant oil)
YEARS = 5

N_ENTRIES = [20, 40, 55]
PYRAMID_STEPS = [0.5, 1.0]
STOP_ATRS = [1.5, 2.0, 3.0]
GRID = [(ne, ps, sa) for ne in N_ENTRIES for ps in PYRAMID_STEPS for sa in STOP_ATRS]


def trade_returns_by_month(trades):
    """Map 'YYYY-MM' -> summed per-trade fractional return that month."""
    by_month = defaultdict(float)
    for t in trades:
        notional = t["entry"] * UNITS_PER_RISK_UNIT
        if notional <= 0:
            continue
        r = t["pnl"] / notional
        ym = t["exit_time"][:7]  # YYYY-MM
        by_month[ym] += r
    return by_month


def all_months(lo, hi):
    y, m = int(lo[:4]), int(lo[5:7])
    ey, em = int(hi[:4]), int(hi[5:7])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1; y += 1
    return out


def run():
    print(f"Fetching {YEARS}y D1 for commodity basket {BASKET}...")
    bars = {inst: fetch_candles(inst, YEARS, "D") for inst in BASKET}
    for inst in BASKET:
        print(f"  {inst:<12} {len(bars[inst])} bars")

    # For each config, build an equal-weight portfolio monthly return series
    # on a common global month axis (so PBO can align across configs).
    # Also keep per-instrument trades for the concentration check.
    config_results = {}
    per_inst_trades = {}  # (config, inst) -> trades

    # First pass: collect every instrument's monthly map for every config,
    # and the global month range across everything.
    cfg_inst_maps = {}
    global_present = set()
    for cfg in GRID:
        ne, ps, sa = cfg
        nx = ne // 2
        inst_month_maps = {}
        for inst in BASKET:
            pip, spread = specs_for(inst)
            trades, _, _ = backtest(bars[inst], n_entry=ne, n_exit=nx,
                                    pyramid_step=ps, max_units=4, stop_atr=sa,
                                    spread_pips=spread, pip=pip)
            inst_month_maps[inst] = trade_returns_by_month(trades)
            per_inst_trades[(cfg, inst)] = trades
            global_present.update(inst_month_maps[inst].keys())
        cfg_inst_maps[cfg] = inst_month_maps

    # Common global month axis -> every config gets an equal-length series.
    axis = all_months(min(global_present), max(global_present))
    for cfg in GRID:
        inst_month_maps = cfg_inst_maps[cfg]
        port = []
        for ym in axis:
            vals = [inst_month_maps[inst].get(ym, 0.0) for inst in BASKET]
            port.append(sum(vals) / len(BASKET))
        config_results[cfg] = port

    # rank configs by monthly Sharpe
    ranked = sorted(GRID, key=lambda c: sharpe_from_returns(config_results[c]) if len(config_results[c]) > 1 else -9,
                    reverse=True)
    print(f"\nGrid: {len(GRID)} configs. Top 5 by monthly Sharpe:")
    print(f"  {'n_entry':>7}{'p_step':>7}{'stop':>6}{'SR_mo':>8}{'SR_ann':>8}{'months':>8}")
    for c in ranked[:5]:
        s = config_results[c]
        sr = sharpe_from_returns(s) if len(s) > 1 else 0
        print(f"  {c[0]:>7}{c[1]:>7}{c[2]:>6}{sr:>+8.3f}{sr*math.sqrt(12):>+8.2f}{len(s):>8}")

    best = ranked[0]
    port = config_results[best]
    n_obs = len(port)
    sr_best = sharpe_from_returns(port)
    _, _, skew, kurt = moments(port)
    trial_srs = [sharpe_from_returns(config_results[c]) if len(config_results[c]) > 1 else 0 for c in GRID]
    sr_var = stdev(trial_srs) ** 2 if len(trial_srs) > 1 else 0.0
    sr_bench = expected_max_sr(sr_var, len(GRID))
    psr_val = psr(sr_best, 0.0, n_obs, skew, kurt)
    dsr_val = psr(sr_best, sr_bench, n_obs, skew, kurt)

    # All configs already share the global month axis -> directly PBO-alignable.
    pbo_val = compute_pbo(config_results, n_splits=10) if n_obs >= 20 else float("nan")

    print(f"\n{'='*64}")
    print(f"  BEST CONFIG: n_entry={best[0]}, pyramid_step={best[1]}, stop_atr={best[2]}")
    print(f"{'='*64}")
    print(f"  Monthly obs:   {n_obs}")
    print(f"  Sharpe (mo):   {sr_best:+.3f}   (ann {sr_best*math.sqrt(12):+.2f})")
    print(f"  skew {skew:+.2f}  kurt {kurt:.2f}")
    print(f"  Trials: {len(GRID)}   E[max SR_null]: {sr_bench:+.3f}")
    print(f"  PSR:  {psr_val:.3f}")
    print(f"  DSR:  {dsr_val:.3f}")
    print(f"  PBO:  {pbo_val:.3f}")

    # Concentration check: per-instrument PF for the best config
    print(f"\n  Concentration check (best config, standalone per instrument):")
    print(f"  {'inst':<12}{'trades':>7}{'PF':>7}{'P/L':>12}")
    contribs = []
    for inst in BASKET:
        trs = per_inst_trades[(best, inst)]
        st = stats(trs, [t["pnl"] for t in trs])
        pf = st.get("profit_factor", 0) if st.get("n", 0) else 0
        pl = st.get("total_pnl", 0) if st.get("n", 0) else 0
        contribs.append((inst, st.get("n", 0), pf, pl))
        print(f"  {inst:<12}{st.get('n',0):>7}{pf:>7.2f}{pl:>+12.2f}")
    profitable = sum(1 for _, n, pf, _ in contribs if n > 0 and pf > 1.0)
    print(f"  -> {profitable}/{len(BASKET)} instruments individually PF>1")

    print(f"\n{'='*64}")
    survives = dsr_val > 0.95 and (pbo_val < 0.5 if not math.isnan(pbo_val) else False) and profitable >= 3
    print(f"  VERDICT: {'SURVIVES -- worth a forward test' if survives else 'FAILS the rigor'}")
    if not survives:
        reasons = []
        if dsr_val <= 0.95: reasons.append(f"DSR {dsr_val:.2f}<=0.95")
        if not math.isnan(pbo_val) and pbo_val >= 0.5: reasons.append(f"PBO {pbo_val:.2f}>=0.5")
        if profitable < 3: reasons.append(f"only {profitable}/4 instruments PF>1 (concentrated)")
        print(f"  Reasons: {', '.join(reasons)}")
    print(f"{'='*64}")


if __name__ == "__main__":
    run()
