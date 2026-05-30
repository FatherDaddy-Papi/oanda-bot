"""Gate 9 silent-halt monitor.

A risk-gate halt (kill switch via RISK_HALT.txt, or a daily/weekly loss limit)
stops the bots from entering but lets their jobs exit 0 -- so it raises no failure
alert. This script queries the live gate and reports whether trading is halted, so
a scheduled workflow can open/close an alert issue.

Always exits 0 (so the monitor job stays green); the halt state is communicated on
stdout and, under GitHub Actions, via $GITHUB_OUTPUT (halted=true|false, reason=...).

Run locally:  python halt_check.py
"""
import os
import sys

from oandapyV20 import API

import risk_gate

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

ACC = os.environ.get("OANDA_ACCOUNT_ID")
TOKEN = os.environ.get("OANDA_API_TOKEN")
ENV = os.environ.get("OANDA_ENV", "practice")


def set_output(halted: bool, reason: str):
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    reason = " ".join(str(reason).split())  # single line
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"halted={'true' if halted else 'false'}\n")
        f.write(f"reason={reason}\n")


def main():
    if not ACC or not TOKEN:
        print("halt_check: missing OANDA creds; cannot determine state. No alert.")
        set_output(False, "creds missing")
        sys.exit(0)

    client = API(access_token=TOKEN, environment=ENV)
    try:
        allow, reason = risk_gate.check(client, ACC)
    except Exception as e:  # noqa: BLE001
        # Can't query -> stay quiet here; sustained outages are alerted by the
        # trading workflow's own failure path. Avoid double-alerting.
        print(f"halt_check: query failed, staying quiet: {e}")
        set_output(False, f"query failed: {e}")
        sys.exit(0)

    if allow:
        print(f"healthy: {reason}")
        set_output(False, reason)
    else:
        print(f"HALTED: {reason}")
        set_output(True, reason)
    sys.exit(0)


if __name__ == "__main__":
    main()
