"""Deterministic tests for the Gate 11 correlation cap -- apply_group_cap().

No network: the cap math is a pure function, so we test it directly across the
empty / partially-full / full / over-full / short / zero cases.

Run:  python test_correlation_cap.py        (exit 0 = all pass, 1 = any failure)
"""
import sys

from oanda_trade import GROUP_GROSS_CAP_PCT, apply_group_cap, group_members

results = []


def expect(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def main():
    nav = 100_000.0                       # group budget = 25,000 notional
    px = 1_000.0                          # price per unit, so units*px = notional

    # Group membership
    expect("NAS100 and BTC share a group", "BTC_USD" in group_members("NAS100_USD"))
    expect("XAU is a singleton group", group_members("XAU_USD") == ["XAU_USD"])

    # 1. Empty group, within budget -> unchanged
    capped, bound = apply_group_cap(10.0, px, nav, 0.0)          # 10k < 25k
    expect("uncapped when group empty and within budget", capped == 10.0 and not bound)

    # 2. Sibling uses 20k of 25k -> new target scaled to the 5k remainder
    capped, bound = apply_group_cap(10.0, px, nav, 20_000.0)     # wants 10k, allowed 5k
    expect("scales to remaining budget", bound and abs(capped * px - 5_000.0) < 1e-6)

    # 3. Group already at / over budget -> target zeroed
    capped, bound = apply_group_cap(10.0, px, nav, 25_000.0)
    expect("zeroes when group at budget", capped == 0.0 and bound)
    capped, bound = apply_group_cap(10.0, px, nav, 30_000.0)
    expect("zeroes when group over budget", capped == 0.0 and bound)

    # 4. Short target capped, sign preserved
    capped, bound = apply_group_cap(-10.0, px, nav, 20_000.0)
    expect("preserves short sign while scaling",
           bound and capped < 0 and abs(abs(capped) * px - 5_000.0) < 1e-6)

    # 5. Zero target -> no-op
    capped, bound = apply_group_cap(0.0, px, nav, 0.0)
    expect("zero target stays zero", capped == 0.0 and not bound)

    n_fail = sum(1 for _, ok in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} passed")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
