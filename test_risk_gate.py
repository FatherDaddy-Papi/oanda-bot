"""Deterministic tests for risk_gate.check() -- Gate 6 evidence.

No network: the AccountSummary request is answered by a fake client, and closed
trade P&L is injected directly, so the kill-switch / daily-limit / weekly-limit /
allow branches are each exercised independently of the live account or today's date.

Run:  python test_risk_gate.py        (exit 0 = all pass, 1 = any failure)
"""
import os
import sys

import risk_gate


class FakeClient:
    """Answers only the AccountSummary request that risk_gate.check() makes.
    TradesList is bypassed by monkeypatching closed_pl_since."""
    def __init__(self, nav):
        self.nav = nav

    def request(self, r):
        r.response = {"account": {"NAV": str(self.nav)}}


results = []


def expect(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def with_pl(day_pl, week_pl):
    """Make closed_pl_since return day then week P&L, in the order check() calls it."""
    seq = iter([day_pl, week_pl])
    risk_gate.closed_pl_since = lambda *a, **k: next(seq)


def ensure_no_halt():
    if os.path.exists(risk_gate.KILL_SWITCH_FILE):
        os.remove(risk_gate.KILL_SWITCH_FILE)


def main():
    orig = risk_gate.closed_pl_since
    ensure_no_halt()
    nav = 100_000.0          # daily limit -2000, weekly limit -5000
    client = FakeClient(nav)

    try:
        # 1. Within limits -> allowed
        with_pl(-50.0, -120.0)
        allow, reason = risk_gate.check(client, "acc")
        expect("allows entries when within limits", allow is True and reason.startswith("OK"))

        # 2. Daily limit breached
        with_pl(-2_000.0, -2_000.0)
        allow, reason = risk_gate.check(client, "acc")
        expect("blocks on daily limit", allow is False and "DAILY LIMIT" in reason)

        # 3. Weekly limit breached while daily is fine
        with_pl(-100.0, -5_000.0)
        allow, reason = risk_gate.check(client, "acc")
        expect("blocks on weekly limit", allow is False and "WEEKLY LIMIT" in reason)

        # 4. Just inside both limits -> allowed (boundary)
        with_pl(-1_999.99, -4_999.99)
        allow, reason = risk_gate.check(client, "acc")
        expect("allows just inside both limits", allow is True)

        # 5. Kill switch present -> blocked regardless of P&L (short-circuits first)
        with_pl(0.0, 0.0)
        open(risk_gate.KILL_SWITCH_FILE, "w").close()
        allow, reason = risk_gate.check(client, "acc")
        expect("blocks when kill switch present", allow is False and "KILL SWITCH" in reason)

        # 6. Kill switch removed -> allowed again (flip back through both states)
        os.remove(risk_gate.KILL_SWITCH_FILE)
        with_pl(0.0, 0.0)
        allow, reason = risk_gate.check(client, "acc")
        expect("allows after kill switch removed", allow is True)
    finally:
        risk_gate.closed_pl_since = orig
        ensure_no_halt()

    n_fail = sum(1 for _, ok in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} passed")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
