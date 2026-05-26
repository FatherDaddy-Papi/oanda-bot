"""Pre-entry risk gate: kill switch + daily/weekly loss limits.

Stateless. Closed-trade P&L is queried from OANDA each call (source of truth),
so no local persistence is needed beyond the kill switch file.

Kill switch:  presence of RISK_HALT.txt in the repo root halts ALL entries.
              Existing positions can still close on their own rules.
              To halt:   touch RISK_HALT.txt && git commit && git push
              To resume: git rm RISK_HALT.txt && git commit && git push

Limits are % of current NAV (good-enough proxy for start-of-period NAV; on
a 2% drawdown the difference is ~0.04 pp). Adjust constants below to taste.
"""
import os
from datetime import datetime, timezone, timedelta
from oandapyV20.endpoints import trades as trades_ep, accounts

DAILY_LIMIT_PCT = 0.02
WEEKLY_LIMIT_PCT = 0.05
KILL_SWITCH_FILE = os.path.join(os.path.dirname(__file__), "RISK_HALT.txt")


def _start_of_utc_day(now=None):
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_utc_week(now=None):
    now = now or datetime.now(timezone.utc)
    sod = _start_of_utc_day(now)
    return sod - timedelta(days=sod.weekday())  # Monday 00:00 UTC


def _parse_oanda_time(ts):
    if not ts:
        return None
    clean = ts.split(".")[0] + "+00:00"
    try:
        return datetime.fromisoformat(clean)
    except ValueError:
        return None


def closed_pl_since(client, account_id, since_dt):
    """Sum realizedPL across recently-closed trades with closeTime >= since_dt.

    Uses TradesList with state=CLOSED, count=500. For this bot's volume
    (3 instruments, ~1 trade per instrument per day) that's >2 months of history.
    """
    r = trades_ep.TradesList(accountID=account_id,
                             params={"state": "CLOSED", "count": 500})
    client.request(r)
    total = 0.0
    for t in r.response.get("trades", []):
        dt = _parse_oanda_time(t.get("closeTime"))
        if dt is None or dt < since_dt:
            continue
        total += float(t.get("realizedPL", 0) or 0)
    return total


def kill_switch_active():
    return os.path.exists(KILL_SWITCH_FILE)


def check(client, account_id):
    """Pre-entry gate. Returns (allow_entries: bool, reason: str).

    Always log the reason so the cron audit trail is clear about why a bar
    was skipped. Exits in tick_once.py are NOT gated — they run regardless.
    """
    if kill_switch_active():
        return False, f"KILL SWITCH active ({os.path.basename(KILL_SWITCH_FILE)} present)"

    summary = accounts.AccountSummary(accountID=account_id)
    client.request(summary)
    nav = float(summary.response["account"]["NAV"])

    day_pl = closed_pl_since(client, account_id, _start_of_utc_day())
    week_pl = closed_pl_since(client, account_id, _start_of_utc_week())
    day_limit = -nav * DAILY_LIMIT_PCT
    week_limit = -nav * WEEKLY_LIMIT_PCT

    if day_pl <= day_limit:
        return False, (f"DAILY LIMIT hit: day P/L {day_pl:+.2f} <= {day_limit:.2f} "
                       f"(-{DAILY_LIMIT_PCT*100:.1f}% of {nav:.2f} NAV)")
    if week_pl <= week_limit:
        return False, (f"WEEKLY LIMIT hit: week P/L {week_pl:+.2f} <= {week_limit:.2f} "
                       f"(-{WEEKLY_LIMIT_PCT*100:.1f}% of {nav:.2f} NAV)")
    return True, (f"OK  day P/L {day_pl:+.2f}/{day_limit:.2f}  "
                  f"week {week_pl:+.2f}/{week_limit:.2f}  NAV {nav:.2f}")
