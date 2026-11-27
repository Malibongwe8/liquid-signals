"""Intraday signal engine: analyse FX, indices and synthetic volatility
indices, render a candlestick entry/TP/SL chart, push to Telegram.

Timeframes: any of 5m / 15m / 1h (config ``timeframes``). Candles come from
Yahoo Finance OHLC for FX ("EURUSD=X"), cash indices ("^DJI"/"^NDX") and crypto
("BTC-USD"), and from the Deriv WebSocket API for synthetic volatility indices
(R_10/R_25/R_50/R_100).

Signal logic
------------
Direction comes from the SMA regime (fast > slow -> BUY, fast < slow -> SELL).
Levels are ATR-scaled:
    entry = latest close
    SL    = entry -/+ sl_atr * ATR
    TP    = entry +/- tp_atr * ATR
where ATR is the true range over ``atr_period`` candles.

A message is sent only when an instrument+timeframe regime *changes*, so the
channel is not spammed with the same bias every loop.

Usage:
    python forex_signal.py            # one pass
    python forex_signal.py --test     # render a chart + send one TEST message
    python forex_signal.py --watch    # loop every poll_seconds
    python forex_signal.py --help     # status/stats/positions/pause/resume
"""
from __future__ import annotations

import fcntl
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

import feeds
import telegram
import tradebook

ROOT = Path(__file__).resolve().parent
CHARTS = ROOT / "charts"
STATE = ROOT / "forex_state.json"
CONTROL = ROOT / "control.json"
WORKFLOW = "signal-scan"


def load_config():
    return json.loads((ROOT / "config.json").read_text())


def _paused():
    """True when the operator has paused signal sending (control.json)."""
    if CONTROL.exists():
        try:
            return bool(json.loads(CONTROL.read_text()).get("paused"))
        except Exception:
            return False
    return False


def _write_control(paused):
    CONTROL.write_text(json.dumps({
        "paused": paused,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }, indent=2))


def _gh(*cmd):
    """Run the gh CLI; returns (returncode, combined output)."""
    try:
        r = subprocess.run(["gh", *cmd], capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, "gh CLI not found"
    except Exception as exc:  # pragma: no cover
        return 1, str(exc)


def _repo_slug():
    """owner/name parsed from the git 'origin' remote, or None."""
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=15)
        m = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?\s*$", r.stdout.strip())
        return m.group(1) if m else None
    except Exception:
        return None


def set_paused(paused):
    """Pause/ resume locally (control.json) and on the GitHub runner (gh)."""
    _write_control(paused)
    print(f"scheduler -> {'PAUSED' if paused else 'RUNNING'}  (control.json)")
    repo = _repo_slug()
    if not repo:
        print("  (no GitHub remote detected; only local scans are affected)")
        return
    verb = "disable" if paused else "enable"
    rc, out = _gh("workflow", verb, WORKFLOW, "--repo", repo)
    print(f"  gh workflow {verb} {WORKFLOW}: {'ok' if rc == 0 else out}")
    if paused:
        rc, ids = _gh("run", "list", "--repo", repo, "-w", WORKFLOW,
                      "--status", "in_progress", "--json", "databaseId",
                      "-q", ".[].databaseId")
        if rc == 0:
            for rid in ids.split():
                if rid.strip().isdigit():
                    _gh("run", "cancel", rid.strip(), "--repo", repo)
                    print(f"  cancelled in-flight run {rid.strip()}")


def params_for(cfg, asset_class):
    """Strategy parameters for an asset class: default overlaid by its block."""
    params = dict(cfg["strategy"]["default"])
    params.update(cfg["strategy"].get(asset_class, {}))
    return params


def sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def atr(candles, n):
    """Average true range over the last n candles."""
    if len(candles) <= n:
        return None
    trs = []
    for i in range(len(candles) - n, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(pc - l)))
    return sum(trs) / len(trs)


# Yahoo range needed to get enough candles at each interval.
PERIOD_FOR = {"5m": "5d", "15m": "1mo", "1h": "1mo", "4h": "3mo", "1d": "6mo"}


def instrument(item):
    """Resolve a watchlist entry -> (yahoo_symbol, name, decimals, unit, unit_name).

    ``forex`` entries carry base/quote (e.g. EUR/USD -> "EURUSD=X"); ``index``
    and ``spot`` (crypto) entries carry a Yahoo symbol directly (e.g. "^VIX",
    "BTC-USD") plus a display ``name`` and optional ``decimals``. Pips apply to
    FX; indices are quoted in points, crypto in USD.
    """
    if item["asset_class"] == "forex":
        base, quote = item["base"], item["quote"]
        if quote.upper() == "JPY":
            return f"{base}{quote}=X", f"{base}/{quote}", 3, 0.01, "pips"
        return f"{base}{quote}=X", f"{base}/{quote}", 5, 0.0001, "pips"
    if item["asset_class"] == "synthetic":
        sym = item["symbol"]  # Deriv symbol, e.g. "R_10"
        return (sym, item.get("name", sym), item.get("decimals", 2),
                item.get("unit", 1.0), item.get("unit_name", "pts"))
    sym = item["symbol"]
    default_unit = "USD" if item["asset_class"] == "spot" else "pts"
    return (sym, item.get("name", sym), item.get("decimals", 2),
            item.get("unit", 1.0), item.get("unit_name", default_unit))


def analyze(item, interval, params):
    symbol, name, decimals, unit, unit_name = instrument(item)
    period = item.get("period") or PERIOD_FOR.get(interval, "1mo")
    candles, _ = feeds.candles_for(item["asset_class"], symbol, interval, period)
    closes = [c["c"] for c in candles]
    if len(closes) < params["slow"] + 1:
        return None
    fast = sma(closes, params["fast"])
    slow = sma(closes, params["slow"])
    a = atr(candles, params["atr_period"])
    if a is None:
        return None
    return {
        "candles": candles,
        "label": f"{name} {interval}",
        "symbol": symbol,
        "feed": item["asset_class"],
        "period": period,
        "decimals": decimals,
        "unit": unit,
        "unit_name": unit_name,
        "price": closes[-1],
        "fast": fast,
        "slow": slow,
        "atr": a,
        "regime": "up" if fast > slow else "down",
    }


def build_signal(a, params):
    direction = "BUY" if a["regime"] == "up" else "SELL"
    entry, atr_ = a["price"], a["atr"]
    if direction == "BUY":
        sl, tp = entry - params["sl_atr"] * atr_, entry + params["tp_atr"] * atr_
    else:
        sl, tp = entry + params["sl_atr"] * atr_, entry - params["tp_atr"] * atr_
    risk = abs(entry - sl)
    rr = abs(tp - entry) / risk if risk else 0.0
    return {"direction": direction, "entry": entry, "tp": tp, "sl": sl, "rr": rr}


def render_chart(a, s, path):
    candles = a["candles"][-150:]
    n = len(candles)
    d = a["decimals"]

    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=130)
    for i, c in enumerate(candles):
        up = c["c"] >= c["o"]
        color = "#1a9850" if up else "#d73027"
        ax.vlines(i, c["l"], c["h"], color=color, lw=0.8)
        lo, hi = sorted((c["o"], c["c"]))
        ax.add_patch(Rectangle((i - 0.3, lo), 0.6, max(hi - lo, 1e-9),
                               facecolor=color, edgecolor=color, lw=0.4))

    ax.axhline(s["entry"], color="#555555", ls="--", lw=1.1,
               label=f"entry {s['entry']:.{d}f}")
    ax.axhline(s["tp"], color="#1a9850", lw=1.5, label=f"TP {s['tp']:.{d}f}")
    ax.axhline(s["sl"], color="#d73027", lw=1.5, label=f"SL {s['sl']:.{d}f}")

    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [datetime.fromtimestamp(candles[i]["t"], timezone.utc).strftime("%m-%d %H:%M")
         for i in ticks], rotation=30, ha="right", fontsize=7)

    ax.set_title(f"{a['label']}  {s['direction']} signal  (candles)",
                 color="#1a9850" if s["direction"] == "BUY" else "#d73027",
                 fontweight="bold")
    ax.set_ylabel("price")
    ax.grid(alpha=0.2)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def caption_for(a, s, params, now):
    d, unit, un = a["decimals"], a["unit"], a["unit_name"]
    tp_u = abs(s["tp"] - s["entry"]) / unit
    sl_u = abs(s["entry"] - s["sl"]) / unit
    return (
        f"{a['label']}  {s['direction']}\n"
        f"Entry: {s['entry']:.{d}f}\n"
        f"TP:    {s['tp']:.{d}f}  (+{tradebook.fmt_units(tp_u, d)} {un})\n"
        f"SL:    {s['sl']:.{d}f}  (-{tradebook.fmt_units(sl_u, d)} {un})\n"
        f"R:R   1:{s['rr']:.1f}\n"
        f"Basis: trend {a['regime']} (SMA{params['fast']}/{params['slow']}), "
        f"ATR{params['atr_period']} {a['atr']:.{d}f}\n"
        f"Time:  {now}"
    )


def scan(cfg, send=True):
    """Serialize scans across processes so trades are never settled twice."""
    if send and _paused():
        print("  [paused] sending is paused (run: python forex_signal.py --resume)")
        return []
    lock = open(ROOT / "scan.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("  [skip] another scan is already running")
        lock.close()
        return []
    try:
        return _scan(cfg, send)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _scan(cfg, send=True):
    tfs = cfg.get("timeframes", ["1h"])

    chat_id = telegram.chat_id() or telegram.resolve_chat_id()

    CHARTS.mkdir(exist_ok=True)
    db = tradebook.load()
    closed = tradebook.settle(db)
    if send:
        for t in closed:
            try:
                telegram.send_message(chat_id, tradebook.result_message(t))
                print(f"  [closed] {t['key']} {t['status']} "
                      f"{t['result_units']:+.0f} {t['unit_name']} ({t['r']:+.2f}R)")
            except Exception as exc:
                print(f"  [closed] {t['key']} !! send failed: {exc}")
    tradebook.save(db)

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    items = [w for w in cfg["watchlist"]
             if w["asset_class"] in ("forex", "index", "spot", "synthetic")]
    sent = []
    for item in items:
        name = item.get("name") or f"{item.get('base', '')}/{item.get('quote', '')}"
        params = params_for(cfg, item["asset_class"])
        for tf in tfs:
            key = f"{name} {tf}"
            try:
                a = analyze(item, tf, params)
            except Exception as exc:
                print(f"  [skip] {key}: {exc}")
                continue
            if a is None:
                print(f"  [skip] {key}: not enough data")
                continue

            d = a["decimals"]
            prev = state.get(key)
            bias_line = f"  {key:<18} px={a['price']:.{d}f}  regime={a['regime']}"
            if prev == a["regime"]:
                print(bias_line + "   (no change)")
                continue

            s = build_signal(a, params)
            if send:
                png = CHARTS / f"{key.replace('/', '-').replace(' ', '_')}.png"
                try:
                    render_chart(a, s, png)
                    telegram.send_photo(chat_id, png, caption_for(a, s, params, now))
                except Exception as exc:
                    print(bias_line + f"   !! failed: {exc}")
                    continue
                tradebook.open_trade(db, key, item, tf, a["symbol"], a["period"],
                                     s, a, now)
            state[key] = a["regime"]
            sent.append((key, s["direction"]))
            if send:
                print(bias_line + f"   -> sent {s['direction']} "
                                  f"(entry {s['entry']:.{d}f}, "
                                  f"TP {s['tp']:.{d}f}, SL {s['sl']:.{d}f})")
            else:
                print(bias_line + f"   [seeded {s['direction']}]")

    STATE.write_text(json.dumps(state, indent=2))
    tradebook.save(db)
    return sent


def send_test(cfg):
    chat_id = telegram.chat_id() or telegram.resolve_chat_id()
    CHARTS.mkdir(exist_ok=True)
    item = [w for w in cfg["watchlist"]
            if w["asset_class"] in ("forex", "index", "spot", "synthetic")][0]
    params = params_for(cfg, item["asset_class"])
    a = analyze(item, cfg.get("timeframes", ["1h"])[0], params)
    s = build_signal(a, params)
    png = CHARTS / "test.png"
    render_chart(a, s, png)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    res = telegram.send_photo(chat_id, png, "TEST\n" + caption_for(a, s, params, now))
    print("sent test to chat", chat_id, "ok=", res.get("ok"))
    return res


def send_report(cfg):
    chat_id = telegram.chat_id() or telegram.resolve_chat_id()
    db = tradebook.load()
    tradebook.settle(db)
    tradebook.save(db)
    res = telegram.send_message(chat_id, tradebook.report_text(db))
    print("sent report to chat", chat_id, "ok=", res.get("ok"))
    return res


HELP = """\
liquid-signals - intraday signal engine

  python forex_signal.py              one scan now (sends any new signals)
  python forex_signal.py --watch      loop a scan every poll_seconds
  python forex_signal.py --seed       record baselines without sending
  python forex_signal.py --test       send one TEST chart to Telegram
  python forex_signal.py --report     send the daily report to Telegram

  python forex_signal.py --status     run state + open book + win-rates
  python forex_signal.py --daily      today's closed-trade stats
  python forex_signal.py --weekly     last 7 days stats
  python forex_signal.py --monthly    last 30 days stats
  python forex_signal.py --positions  list the open paper positions

  python forex_signal.py --pause      stop sending (disables the GitHub loop)
  python forex_signal.py --resume     resume sending (re-enables the loop)
"""


def main():
    cfg = load_config()
    args = sys.argv[1:]

    if not args:
        if _paused():
            print("PAUSED - not scanning. Run: python forex_signal.py --resume")
            return
        scan(cfg)
        return

    if args[0] in ("-h", "--help", "help"):
        print(HELP)
    elif "--pause" in args:
        set_paused(True)
    elif "--resume" in args:
        set_paused(False)
    elif "--positions" in args:
        print(tradebook.positions_text(tradebook.load()))
    elif "--daily" in args:
        db = tradebook.load()
        print(f"DAILY STATS  {tradebook.today()}")
        print(tradebook.stats_line("Today", tradebook.stats(db, tradebook.today())))
    elif "--weekly" in args:
        print("WEEKLY STATS  (last 7 days)")
        print(tradebook.stats_line("Last 7d", tradebook.stats_since(tradebook.load(), 7)))
    elif "--monthly" in args:
        print("MONTHLY STATS  (last 30 days)")
        print(tradebook.stats_line("Last 30d", tradebook.stats_since(tradebook.load(), 30)))
    elif "--status" in args or "--stats" in args:
        print(tradebook.status_text(tradebook.load(), _paused()))
    elif "--test" in args:
        send_test(cfg)
    elif "--report" in args:
        send_report(cfg)
    elif "--seed" in args:
        scan(cfg, send=False)
    elif "--watch" in args:
        interval = cfg.get("poll_seconds", 300)
        print(f"watching instruments every {interval}s (Ctrl-C to stop)")
        while True:
            if _paused():
                print("  paused - not scanning")
            else:
                scan(cfg)
            time.sleep(interval)
    else:
        print(HELP)


if __name__ == "__main__":
    main()
