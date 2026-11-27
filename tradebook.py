"""Paper-trade bookkeeper for the intraday signal engine.

Every fired signal opens one paper position per instrument+timeframe. Positions
are settled against subsequent candles: a touch of TP is a win, a touch of SL is
a loss. When one candle spans both levels the SL is assumed to hit first
(conservative). The full ledger and a running win-rate live in
``paper_trades.json``.

Sizing is nominal: each trade is tracked in price units (pips for FX, points for
indices) and as an R-multiple, so results are comparable across instruments
without committing capital.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feeds

ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / "paper_trades.json"

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=8))


def load():
    return json.loads(LEDGER.read_text()) if LEDGER.exists() else {"seq": 0, "trades": []}


def save(db):
    LEDGER.write_text(json.dumps(db, indent=2))


def open_trade(db, key, item, tf, symbol, period, sig, a, now):
    """Open a paper position for a fresh signal (one open per key)."""
    if any(t["status"] == "open" and t["key"] == key for t in db["trades"]):
        return None
    db["seq"] += 1
    trade = {
        "id": db["seq"],
        "key": key,
        "item": item,
        "tf": tf,
        "symbol": symbol,
        "feed": a.get("feed", "index"),
        "period": period,
        "direction": sig["direction"],
        "entry": sig["entry"],
        "tp": sig["tp"],
        "sl": sig["sl"],
        "unit": a["unit"],
        "unit_name": a["unit_name"],
        "decimals": a["decimals"],
        "opened_at": int(time.time()),
        "opened_human": now,
        "status": "open",
        "exit": None,
        "closed_at": None,
        "result_units": None,
        "r": None,
    }
    db["trades"].append(trade)
    return trade


def settle(db):
    """Close any open trade whose TP/SL was touched. Returns the closed trades."""
    closed = []
    for t in db["trades"]:
        if t["status"] != "open":
            continue
        try:
            candles, _ = feeds.candles_for(t.get("feed", "index"), t["symbol"],
                                           t["tf"], t["period"])
        except Exception:
            continue
        for c in candles:
            if c["t"] <= t["opened_at"]:
                continue
            if t["direction"] == "BUY":
                if c["l"] <= t["sl"]:
                    _close(db, t, t["sl"], "loss")
                    closed.append(t)
                    break
                if c["h"] >= t["tp"]:
                    _close(db, t, t["tp"], "win")
                    closed.append(t)
                    break
            else:  # SELL
                if c["h"] >= t["sl"]:
                    _close(db, t, t["sl"], "loss")
                    closed.append(t)
                    break
                if c["l"] <= t["tp"]:
                    _close(db, t, t["tp"], "win")
                    closed.append(t)
                    break
    return closed


def _close(db, t, exit_price, status):
    sign = 1 if t["direction"] == "BUY" else -1
    risk = abs(t["entry"] - t["sl"]) / t["unit"]
    t["status"] = status
    t["exit"] = exit_price
    t["closed_at"] = int(time.time())
    t["result_units"] = (exit_price - t["entry"]) / t["unit"] * sign
    t["r"] = (t["result_units"] / risk) if risk else 0.0


def fmt_units(v, decimals):
    """Human-readable unit distance across very different instrument scales."""
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:.0f}"
    if a >= 1:
        return f"{v:.1f}"
    return f"{v:.{decimals}f}"


def result_message(t):
    d = t["decimals"]
    un = t["unit_name"]
    return (
        f"CLOSED  {t['key']}  {t['direction']}\n"
        f"Result: {t['status'].upper()}  {fmt_units(t['result_units'], d)} {un}  "
        f"({t['r']:+.2f}R)\n"
        f"Entry {t['entry']:.{d}f}  ->  Exit {t['exit']:.{d}f}\n"
        f"TP {t['tp']:.{d}f}  /  SL {t['sl']:.{d}f}"
    )


def _day(ts):
    return datetime.fromtimestamp(ts, TZ).date().isoformat()


def today():
    """Today's date string (YYYY-MM-DD) in Asia/Shanghai."""
    return _day(int(time.time()))


def _summarize(done):
    wins = sum(1 for t in done if t["status"] == "win")
    total = len(done)
    return {
        "closed": total,
        "wins": wins,
        "losses": total - wins,
        "winrate": (wins / total * 100) if total else 0.0,
        "units": sum(t["result_units"] for t in done),
        "r": sum(t["r"] for t in done),
    }


def _open_count(db):
    return sum(1 for t in db["trades"] if t["status"] == "open")


def stats(db, day=None):
    """Win-rate summary. ``day`` (YYYY-MM-DD in Asia/Shanghai) filters closes."""
    done = [t for t in db["trades"] if t["status"] in ("win", "loss")]
    if day:
        done = [t for t in done if _day(t["closed_at"]) == day]
    s = _summarize(done)
    s["day"] = day
    s["open"] = _open_count(db)
    return s


def stats_since(db, days):
    """Win-rate summary over the last ``days`` calendar days (inclusive)."""
    end = datetime.fromtimestamp(int(time.time()), TZ).date()
    start = end - timedelta(days=days - 1)
    done = [t for t in db["trades"] if t["status"] in ("win", "loss")
            and start.isoformat() <= _day(t["closed_at"]) <= end.isoformat()]
    s = _summarize(done)
    s["days"] = days
    s["open"] = _open_count(db)
    return s


def stats_line(label, s):
    """One-line summary used by the terminal commands."""
    return (f"{label:<10} {s['wins']}W / {s['losses']}L   "
            f"winrate {s['winrate']:.0f}%   ({s['closed']} closed)   "
            f"net {s['units']:+.0f} units   ({s['r']:+.2f}R)")


def positions_text(db):
    """Human-readable list of currently open paper positions."""
    open_trades = [t for t in db["trades"] if t["status"] == "open"]
    if not open_trades:
        return "No open paper positions."
    lines = [f"OPEN POSITIONS  ({len(open_trades)})"]
    for t in open_trades:
        d = t["decimals"]
        lines.append(
            f"  #{t['id']:<3} {t['key']:<18} {t['direction']:<4} "
            f"entry {t['entry']:.{d}f}  TP {t['tp']:.{d}f}  SL {t['sl']:.{d}f}  "
            f"[{t['opened_human']}]")
    return "\n".join(lines)


def status_text(db, paused):
    """Compact dashboard: run state, open book, and win-rates over time."""
    lines = [
        f"STATUS   {'PAUSED' if paused else 'RUNNING'}",
        "",
        positions_text(db),
        "",
        stats_line("Today", stats(db, today())),
        stats_line("Last 7d", stats_since(db, 7)),
        stats_line("Last 30d", stats_since(db, 30)),
        stats_line("All-time", stats(db)),
    ]
    return "\n".join(lines)


def report_text(db):
    ds, os_ = stats(db, today()), stats(db)
    lines = [
        f"DAILY REPORT  {today()}",
        "",
        f"Today:  {ds['wins']}W / {ds['losses']}L  "
        f"winrate {ds['winrate']:.0f}%  ({ds['closed']} closed)",
        f"        net {ds['units']:+.0f} units  ({ds['r']:+.2f}R)",
        "",
        f"All-time: {os_['wins']}W / {os_['losses']}L  "
        f"winrate {os_['winrate']:.0f}%  ({os_['closed']} closed)",
        f"          net {os_['units']:+.0f} units  ({os_['r']:+.2f}R)",
        f"Open positions now: {os_['open']}",
    ]
    return "\n".join(lines)
