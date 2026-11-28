# Trading Signal Engine

A small, config-driven engine that watches a fixed universe of markets and
pushes **live trade signals** (candlestick chart + Entry / TP / SL) to Telegram.
Every signal also opens a **paper trade** that is settled against later candles,
so the system reports a running win-rate and a daily summary.

Everything that changes the behaviour lives in [`config.json`](config.json) —
edit that file, not the code, to change the markets or the strategy.

---

## Universe

| Group          | Instruments                                             | Data source            | When        |
| -------------- | ------------------------------------------------------- | ---------------------- | ----------- |
| Crypto         | BTCUSD, ETHUSD, ETHBTC                                  | Yahoo Finance          | Always (24/7) |
| Metals         | XAUUSD (`GC=F`)                                         | Yahoo Finance          | Mon–Fri     |
| FX majors (7)  | EUR/USD, GBP/USD, USD/JPY, USD/CHF, USD/CAD, AUD/USD, NZD/USD | Yahoo Finance     | Mon–Fri     |
| FX minors (15) | EUR/GBP, EUR/JPY, GBP/JPY, EUR/CHF, EUR/AUD, EUR/CAD, GBP/CHF, GBP/AUD, AUD/JPY, AUD/NZD, CAD/JPY, CHF/JPY, NZD/JPY, EUR/NZD, GBP/NZD | Yahoo Finance | Mon–Fri |
| Cash indices   | US30 (`^DJI`), US100 (`^NDX`)                            | Yahoo Finance          | Mon–Fri     |
| Volatility     | Volatility 10/25/50/100 (`R_10`/`R_25`/`R_50`/`R_100`)  | Deriv WebSocket API    | Mon–Fri     |

Each instrument is scanned on every timeframe in `timeframes` (5m / 15m / 1h).

### Weekend universe (crypto only)

Because FX and stock indices are closed at the weekend, the engine scans a
reduced universe on `schedule.weekend_days` (UTC `Sat`/`Sun`):

* items flagged `"weekend": true` (BTCUSD, ETHUSD, ETHBTC) are **always** scanned;
* items flagged `"weekend_only": true` (ETHUSD, ETHBTC) are **skipped** Mon–Fri and
  only appear at the weekend;
* everything else (FX, indices, volatility) is skipped at the weekend.

### Groups (signal filter)

You can restrict scanning to a **group** of instruments, defined in
`config.json` under `groups`:

| Group | Label              | Members                                                  |
| ----- | ------------------ | -------------------------------------------------------- |
| `A`   | All pairs open     | every instrument (the default)                           |
| `B`   | Majors currencies  | the 7 FX majors                                          |
| `C`   | Metals + crypto    | XAUUSD, BTCUSD, ETHUSD, ETHBTC                           |

The active group is set in `groups.active`, and can be switched live without
restarting anything:

* **Telegram** — `/group` shows the list and the active group; `/group a|b|c`
  switches it (operator only).
* **CLI** — `python forex_signal.py --groups` lists them;
  `python forex_signal.py --group c` switches (and pushes it to the live loop).

The choice is stored in `control.json` (like the pause flag) so it survives
across chained runs. A group filter is applied **after** the weekend rule, so on
weekends group C still resolves to the crypto that actually trades
(BTCUSD/ETHUSD/ETHBTC). Picking an explicit group (B/C) also scans its
`weekend_only` members on weekdays, i.e. `/group c` gives all four members.

> Group A keeps the smart default: FX/indices/volatility + BTCUSD on weekdays,
> crypto only at the weekend. Groups B and C scan exactly what they list.

## Strategy (unchanged core)

* **Direction** comes from the SMA *regime*: `fast > slow` → **BUY**, `fast < slow` → **SELL**.
* **Levels** are ATR-scaled:
  * `entry = latest close`
  * `SL = entry − sl_atr × ATR` (BUY) / `entry + sl_atr × ATR` (SELL)
  * `TP = entry + tp_atr × ATR` (BUY) / `entry − tp_atr × ATR` (SELL)
* A message is sent **only when the regime changes** for an instrument+timeframe
  (state dedup), so the channel is not spammed with the same bias every loop.

## Loss re-entry

When a paper trade closes as a **loss** (its SL was hit), the engine
automatically re-issues the same idea once, so a stop-out is followed by a
second attempt around the old stop:

* **Entry** moves to where the **SL** was (`old SL`);
* **TP** stays exactly where it was;
* **SL** is placed `reentry.sl_mult` × the original risk **away from the new entry**;
* direction is unchanged (a stopped-out BUY re-enters as a BUY).

Tune it in `config.json`:

```json
"reentry": { "enabled": true, "sl_mult": 2.0, "max": 1 }
```

`max` caps how many times a position may recover after losses (`1` = a single
re-entry; a re-entry that also loses is not re-issued), so **each signal is
re-issued at most once**. Each re-entry is linked to its parent (`reentry_of`)
so the chain is visible in the ledger.

To make an updated signal easy to spot, the re-entry is styled differently: its
Telegram caption is **italic** (`parse_mode=HTML`) and the chart carries a **red
banner** — `RE-ENTRY (previous SL hit)` — with a red strip reading
"LOSS RE-ISSUED — entry = old stop, TP unchanged".

## Files

| File                    | Role                                                         |
| ----------------------- | ------------------------------------------------------------ |
| `config.json`           | Watchlist, timeframes, strategy parameters, poll interval    |
| `forex_signal.py`       | Engine: analyse → render chart → send → open paper trade     |
| `feeds.py`              | Market-data layer (Yahoo OHLC, Binance, Deriv WebSocket)     |
| `telegram.py`           | Telegram Bot API client (sendMessage / sendPhoto)            |
| `tradebook.py`          | Paper-trade ledger, settlement, win-rate, daily report       |
| `secrets.json`          | **git-ignored** local credentials (token + chat id)          |
| `.github/workflows/`    | Scheduled runs on GitHub Actions                             |

Runtime files created at run time (and git-ignored): `forex_state.json`,
`paper_trades.json`, `control.json`, `telegram_offset.json`, `subscribers.json`,
`charts/`.

## Credentials

The Telegram token/chat id are **never committed**. Resolution order:

1. env vars `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` (used by GitHub Actions),
2. `secrets.json` (local, git-ignored),
3. the blank `telegram` block in `config.json`.

## Running locally

```bash
pip install -r requirements.txt
python forex_signal.py            # one scan (sends any new signals)
python forex_signal.py --seed     # seed state on first run (no message flood)
python forex_signal.py --test     # send one test chart to Telegram
python forex_signal.py --report   # send the daily win-rate report
python forex_signal.py --watch    # loop every poll_seconds
python forex_signal.py --help     # list every command
```

## Controls & stats

You can drive the running system from **Telegram** (just message the bot) or
from the **CLI** on any machine.

### From Telegram

The runner polls the bot on every scan and replies in-chat, so these work
any time the loop is live:

| Message      | What it does                                             |
| ------------ | ------------------------------------------------------- |
| `/start`     | **Join the signal list** — you receive every signal from then on |
| `/status`    | Run state (PAUSED/RUNNING) + active group + win-rates   |
| `/group`     | Show the groups; `/group a\|b\|c` switches the scan group |
| `/daily`     | Today's closed-trade stats                              |
| `/weekly`    | Last 7 days stats                                       |
| `/monthly`   | Last 30 days stats                                      |
| `/positions` | List the currently open paper positions                 |
| `/pause`     | Stop sending signals (the loop keeps running so you can resume) |
| `/resume`    | Start sending signals again                             |
| `/menu`      | List the commands                                       |

### Who receives signals

Signals are broadcast to the **owner** (the configured chat id) **and to every
subscriber** in `subscribers.json`. Anyone who messages the bot `/start` is
added to that list and starts receiving signals immediately — no restart needed
(the runner polls for commands on every scan). `subscribers.json` is runtime
state, carried between runs in the GitHub cache and never committed.

Only the **owner** may `/pause` and `/resume`; a subscriber trying it is told
"Only the operator can…" Anyone who has not joined gets "Send /start to join the
signal list." `/pause` only **mutes** signals and paper trades — the loop stays
alive precisely so `/resume` still reaches it. Run
`python forex_signal.py --setup-bot` once to register the `/` menu in the
Telegram client (the workflow also does this on every run).

### From the CLI

| Command      | What it does                                                        |
| ------------ | ------------------------------------------------------------------- |
| `--status`   | Run state (PAUSED/RUNNING) + open positions + today/7d/30d/all-time |
| `--daily`    | Today's closed-trade stats (wins/losses, win-rate, net R)           |
| `--weekly`   | Last 7 days stats                                                   |
| `--monthly`  | Last 30 days stats                                                  |
| `--positions`| List the currently open paper positions                             |
| `--groups`   | List the scan groups (A/B/C)                                        |
| `--group X`  | Scan only group X (A=all, B=majors, C=metals+crypto)                |
| `--pause`    | Stop sending signals everywhere                                     |
| `--resume`   | Start sending signals again                                         |
| `--setup-bot`| Register the Telegram `/` command menu                              |

`--pause` writes the `control.json` flag **and** dispatches it to the live
GitHub loop via the `gh` CLI (your local flag alone would not reach the runner).
`--resume` dispatches the opposite and the loop unmutes on its next poll. The
machine running these two needs `gh` authenticated:

```bash
gh auth login        # one-off (or export GH_TOKEN=<token>)
```

If `gh` is missing or not logged in, `--pause`/`--resume` still toggle the local
flag and print a clear message that the GitHub side was not changed.

```bash
python forex_signal.py --pause     # mute
python forex_signal.py --status    # confirm: STATUS PAUSED
python forex_signal.py --resume    # unmute
python forex_signal.py --group c   # scan only XAUUSD/BTCUSD/ETHUSD/ETHBTC
```

`--group X` works the same way as `--pause`: it writes the local flag and
dispatches `group:X` to the live loop (which applies it on its next poll). On
the runner the Telegram `/group` command sets it directly.

## Running on GitHub

The workflow [`.github/workflows/signals.yml`](.github/workflows/signals.yml)
runs **continuously and automatically**: one job stays alive for ~55 minutes
scanning every 5 minutes, then queues the next job, and so on forever. It also
sends the daily report at ~21:00 Asia/Shanghai.

Set three repository secrets first
(**Settings → Secrets and variables → Actions**):

| Secret              | Value                                                        |
| ------------------- | ------------------------------------------------------------ |
| `TELEGRAM_TOKEN`    | your bot token                                               |
| `TELEGRAM_CHAT_ID`  | your chat id                                                 |
| `GH_PAT`            | a token that can re-trigger the workflow (see below)         |

**Why a self-chaining loop instead of a plain cron?** GitHub's scheduled
triggers are unreliable — they are frequently delayed or dropped entirely, and
a job cannot re-trigger itself with the default `GITHUB_TOKEN`. So each job ends
by dispatching the next run through `GH_PAT`; a `concurrency` group makes that
next run queue behind the current one, giving a gap-free 5-minute cadence with
no overlaps. The hourly `schedule` is kept only as a backup kicker. This needs a
**public** repository, where GitHub-hosted Actions minutes are free/unlimited.

`GH_PAT` must be able to start workflows:

* **Classic** token with the `repo` + `workflow` scopes, or
* **Fine-grained** token with **Actions: read and write** on this repository.

The regime state and paper-trade ledger are carried between runs with a rolling
`actions/cache` entry, so nothing is committed back to git on every run.

> If the chain ever stops (e.g. `GH_PAT` expires), the hourly schedule will
> restart it; otherwise run `python forex_signal.py --resume` to kick it, or use
> **Run workflow** in the Actions tab.


## Adding or changing instruments

Append an entry to `watchlist` in `config.json`:

```json
{ "asset_class": "forex",     "group": "minors", "base": "EUR", "quote": "TRY" }
{ "asset_class": "index",     "group": "cash",   "name": "US500", "symbol": "^GSPC" }
{ "asset_class": "spot",      "group": "crypto", "name": "SOLUSD", "symbol": "SOL-USD", "decimals": 2 }
{ "asset_class": "spot",      "group": "crypto", "name": "ETHBTC", "symbol": "ETH-BTC", "decimals": 6, "weekend": true, "weekend_only": true }
{ "asset_class": "synthetic", "group": "volatility", "name": "Volatility 75", "symbol": "R_75" }
```

`asset_class` selects the feed: `forex` / `index` / `spot` use Yahoo Finance
(carrying a Yahoo `symbol`, or `base`+`quote` for FX); `synthetic` uses the
Deriv WebSocket API and carries the Deriv `symbol` (e.g. `R_10`). Add
`"weekend": true` to keep an instrument in the weekend (crypto) universe, and
`"weekend_only": true` to show it *only* at the weekend.

## Tuning the strategy

Edit `strategy.default` in `config.json` (`fast`, `slow`, `atr_period`,
`sl_atr`, `tp_atr`). To give one asset class its own values, add a block with
the same name, e.g.:

```json
"strategy": {
  "default": { "fast": 10, "slow": 30, "atr_period": 14, "sl_atr": 1.5, "tp_atr": 2.5 },
  "synthetic": { "fast": 8, "slow": 21 }
}
```

Blocks are merged over `default`, so you only specify what differs.

## Notes

* Signal logic is for **research/paper trading** — no live orders are placed.
* Deriv exposes synthetic indices only over WebSocket; the client in
  `feeds.py` speaks the required minimum of RFC 6455 with no dependencies and
  honours an HTTP(S) proxy when one is set. Some cloud IP ranges are blocked by
  Deriv/Cloudflare — if `R_*` instruments time out the scan simply skips them.
