"""Generate the weekly journal markdown from OANDA trade history.

Run by .github/workflows/weekly_report.yml on Sunday 23:00 UTC, which
then commits journal/YYYY-Www.md to the repo. Can also be run locally:

    python weekly_report.py            # last completed Mon-Sun in UTC
    python weekly_report.py 2026-W21   # explicit ISO week
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from oandapyV20 import API
from oandapyV20.endpoints import trades as trades_ep, accounts

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

import risk_gate  # share limit constants


def iso_week_bounds(iso_week):
    """Return (monday_00z, next_monday_00z) for an ISO week like '2026-W21'.
    If iso_week is None, use the most recent *completed* week."""
    if iso_week is None:
        now = datetime.now(timezone.utc)
        # Most recent completed week = the week BEFORE this one
        this_monday = now.replace(hour=0, minute=0, second=0, microsecond=0) - \
                      timedelta(days=now.weekday())
        start = this_monday - timedelta(days=7)
        return start, this_monday, start.strftime("%G-W%V")
    year, wk = iso_week.split("-W")
    monday = datetime.strptime(f"{year}-{wk}-1", "%G-%V-%u").replace(tzinfo=timezone.utc)
    return monday, monday + timedelta(days=7), iso_week


def parse_t(ts):
    if not ts:
        return None
    clean = ts.split(".")[0] + "+00:00"
    try:
        return datetime.fromisoformat(clean)
    except ValueError:
        return None


def fetch_closed_in_window(client, account_id, start, end):
    r = trades_ep.TradesList(accountID=account_id,
                             params={"state": "CLOSED", "count": 500})
    client.request(r)
    out = []
    for t in r.response.get("trades", []):
        dt = parse_t(t.get("closeTime"))
        if dt is None or dt < start or dt >= end:
            continue
        out.append(t)
    return out


def fetch_open(client, account_id):
    r = trades_ep.OpenTrades(accountID=account_id)
    client.request(r)
    return r.response.get("trades", [])


def fmt_money(x):
    return f"{x:+,.2f}"


def render(week_label, start, end, trades, opens, nav, kill_switch_present):
    lines = []
    lines.append(f"# Weekly Journal — {week_label}")
    lines.append("")
    lines.append(f"Week: **{start.strftime('%Y-%m-%d')} → {(end - timedelta(seconds=1)).strftime('%Y-%m-%d')}** (UTC)")
    lines.append(f"NAV at report time: **{nav:,.2f}**")
    lines.append("")

    if not trades:
        lines.append("## Summary")
        lines.append("No closed trades this week.")
    else:
        pnls = [float(t.get("realizedPL", 0) or 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total = sum(pnls)
        wr = 100.0 * len(wins) / len(trades)
        week_limit = -nav * risk_gate.WEEKLY_LIMIT_PCT
        pct_of_limit = (total / week_limit * 100.0) if total < 0 and week_limit < 0 else 0.0

        lines.append("## Summary")
        lines.append("")
        lines.append(f"| metric | value |")
        lines.append(f"|---|---|")
        lines.append(f"| Trades | {len(trades)} ({len(wins)}W / {len(losses)}L) |")
        lines.append(f"| Win rate | {wr:.1f}% |")
        lines.append(f"| Total P&L | **{fmt_money(total)}** |")
        lines.append(f"| Avg win | {fmt_money(sum(wins)/len(wins)) if wins else '—'} |")
        lines.append(f"| Avg loss | {fmt_money(sum(losses)/len(losses)) if losses else '—'} |")
        lines.append(f"| Weekly limit | {fmt_money(week_limit)} (-{risk_gate.WEEKLY_LIMIT_PCT*100:.1f}% NAV) |")
        if total < 0:
            lines.append(f"| Drawdown used | {pct_of_limit:.1f}% of weekly limit |")
        lines.append("")

        # By instrument
        by_inst = defaultdict(lambda: {"n": 0, "pl": 0.0, "w": 0, "l": 0})
        for t in trades:
            k = t["instrument"]
            pl = float(t.get("realizedPL", 0) or 0)
            by_inst[k]["n"] += 1
            by_inst[k]["pl"] += pl
            if pl > 0:
                by_inst[k]["w"] += 1
            else:
                by_inst[k]["l"] += 1
        lines.append("## By instrument")
        lines.append("")
        lines.append("| instrument | trades | W/L | P&L |")
        lines.append("|---|---:|---:|---:|")
        for k in sorted(by_inst.keys(), key=lambda x: by_inst[x]["pl"], reverse=True):
            v = by_inst[k]
            lines.append(f"| {k} | {v['n']} | {v['w']}/{v['l']} | {fmt_money(v['pl'])} |")
        lines.append("")

        # Best / worst
        best = max(trades, key=lambda t: float(t.get("realizedPL", 0) or 0))
        worst = min(trades, key=lambda t: float(t.get("realizedPL", 0) or 0))
        lines.append("## Notable trades")
        lines.append("")
        lines.append(f"- **Best:**  {best['instrument']} {best.get('initialUnits','?')} units, "
                     f"opened {best.get('openTime','?')[:19]}, closed {best.get('closeTime','?')[:19]}, "
                     f"P&L **{fmt_money(float(best.get('realizedPL', 0) or 0))}**")
        lines.append(f"- **Worst:** {worst['instrument']} {worst.get('initialUnits','?')} units, "
                     f"opened {worst.get('openTime','?')[:19]}, closed {worst.get('closeTime','?')[:19]}, "
                     f"P&L **{fmt_money(float(worst.get('realizedPL', 0) or 0))}**")
        lines.append("")

    # Open positions snapshot
    lines.append("## Open positions at report time")
    lines.append("")
    if not opens:
        lines.append("None.")
    else:
        lines.append("| instrument | units | open price | unrealized P&L | opened |")
        lines.append("|---|---:|---:|---:|---|")
        for t in opens:
            lines.append(f"| {t['instrument']} | {t.get('currentUnits','?')} | "
                         f"{t.get('price','?')} | {fmt_money(float(t.get('unrealizedPL', 0) or 0))} | "
                         f"{t.get('openTime','?')[:19]} |")
    lines.append("")

    # Risk health
    lines.append("## Risk gate health")
    lines.append("")
    lines.append(f"- Kill switch at report time: **{'ACTIVE' if kill_switch_present else 'inactive'}**")
    lines.append(f"- Daily limit: -{risk_gate.DAILY_LIMIT_PCT*100:.1f}% of NAV")
    lines.append(f"- Weekly limit: -{risk_gate.WEEKLY_LIMIT_PCT*100:.1f}% of NAV")
    lines.append("")
    lines.append("---")
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()[:19]}Z by weekly_report.py_")
    return "\n".join(lines) + "\n"


def main():
    iso = sys.argv[1] if len(sys.argv) > 1 else None
    start, end, label = iso_week_bounds(iso)
    client = API(access_token=os.environ["OANDA_API_TOKEN"],
                 environment=os.environ.get("OANDA_ENV", "practice"))
    acct = os.environ["OANDA_ACCOUNT_ID"]

    trades = fetch_closed_in_window(client, acct, start, end)
    opens = fetch_open(client, acct)
    summary = accounts.AccountSummary(accountID=acct)
    client.request(summary)
    nav = float(summary.response["account"]["NAV"])

    body = render(label, start, end, trades, opens, nav, risk_gate.kill_switch_active())

    journal_dir = os.path.join(os.path.dirname(__file__), "journal")
    os.makedirs(journal_dir, exist_ok=True)
    out_path = os.path.join(journal_dir, f"{label}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"Wrote {out_path}  ({len(trades)} closed trades, {len(opens)} open)")


if __name__ == "__main__":
    main()
