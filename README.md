# Trading Signal Engine

A small, config-driven engine that watches a fixed universe of markets and
pushes **live trade signals** (candlestick chart + Entry / TP / SL) to Telegram.
Every signal also opens a **paper trade** that is settled against later candles,
so the system reports a running win-rate and a daily summary.

Everything that changes the behaviour lives in [`config.json`](config.json) —
edit that file, not the code, to change the markets or the strategy.

---

## Universe

| Group          | Instruments                                             | Data source            |
| -------------- | ------------------------------------------------------- | ---------------------- |
| Crypto         | BTCUSD                                                  | Yahoo Finance          |
| FX majors (7)  | EUR/USD, GBP/USD, USD/JPY, USD/CHF, USD/CAD, AUD/USD, NZD/USD | Yahoo Finance     |
| FX minors (15) | EUR/GBP, EUR/JPY, GBP/JPY, EUR/CHF, EUR/AUD, EUR/CAD, GBP/CHF, GBP/AUD, AUD/JPY, AUD/NZD, CAD/JPY, CHF/JPY, NZD/JPY, EUR/NZD, GBP/NZD | Yahoo Finance |
| Cash indices   | US30 (`^DJI`), US100 (`^NDX`)                            | Yahoo Finance          |
| Volatility     | Volatility 10/25/50/100 (`R_10`/`R_25`/`R_50`/`R_100`)  | Deriv WebSocket API    |

Each instrument is scanned on every timeframe in `timeframes` (5m / 15m / 1h).

## Strategy (unchanged core)

* **Direction** comes from the SMA *regime*: `fast > slow` → **BUY**, `fast < slow` → **SELL**.
* **Levels** are ATR-scaled:
  * `entry = latest close`
  * `SL = entry − sl_atr × ATR` (BUY) / `entry + sl_atr × ATR` (SELL)
  * `TP = entry + tp_atr × ATR` (BUY) / `entry − tp_atr × ATR` (SELL)
* A message is sent **only when the regime changes** for an instrument+timeframe
  (state dedup), so the channel is not spammed with the same bias every loop.

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
`paper_trades.json`, `control.json`, `charts/`.

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

| Command      | What it does                                                        |
| ------------ | ------------------------------------------------------------------- |
| `--status`   | Run state (PAUSED/RUNNING) + open positions + today/7d/30d/all-time |
| `--daily`    | Today's closed-trade stats (wins/losses, win-rate, net R)           |
| `--weekly`   | Last 7 days stats                                                   |
| `--monthly`  | Last 30 days stats                                                  |
| `--positions`| List the currently open paper positions                             |
| `--pause`    | Stop sending signals everywhere                                     |
| `--resume`   | Start sending signals again                                         |

`--pause` writes a local `control.json` flag (so any local scan no-ops) **and**
disables the GitHub workflow via the `gh` CLI, cancelling any in-flight run.
`--resume` reverses both. Because the live system runs on GitHub, the machine
running these two commands needs `gh` authenticated:

```bash
gh auth login        # one-off (or export GH_TOKEN=<token>)
```

If `gh` is missing or not logged in, `--pause`/`--resume` still toggle the local
flag and print a clear message that the GitHub side was not changed.

```bash
python forex_signal.py --pause     # stop
python forex_signal.py --status    # confirm: STATUS PAUSED
python forex_signal.py --resume    # start again
```

## Running on GitHub

The workflow [`.github/workflows/signals.yml`](.github/workflows/signals.yml)
keeps a job alive that **scans every 5 minutes**, and sends the daily report at
21:00 Asia/Shanghai. Set two repository secrets first
(**Settings → Secrets and variables → Actions**):

| Secret              | Value                     |
| ------------------- | ------------------------- |
| `TELEGRAM_TOKEN`    | your bot token            |
| `TELEGRAM_CHAT_ID`  | your chat id              |

**Why a loop instead of a 5-minute cron?** GitHub's scheduled triggers are
unreliable at short intervals (they can be delayed or skipped), so the job is
kicked twice an hour (`:00` and `:30`) and each kick runs its own 5-minute scan
loop for ~30 minutes. That gives a continuous 5-minute cadence with only two
cron events per hour. A `concurrency` group guarantees the two loops never
overlap (overlapping scans would double-settle trades). This needs a **public**
repository, where GitHub-hosted Actions minutes are free and unlimited.

The regime state and paper-trade ledger are carried between runs with a rolling
`actions/cache` entry, so nothing is committed back to git on every run.

> Note: GitHub disables scheduled workflows after ~60 days of repository
> inactivity. Open the repo, or hit **Run workflow**, to keep it alive.

## Adding or changing instruments

Append an entry to `watchlist` in `config.json`:

```json
{ "asset_class": "forex",     "group": "minors", "base": "EUR", "quote": "TRY" }
{ "asset_class": "index",     "group": "cash",   "name": "US500", "symbol": "^GSPC" }
{ "asset_class": "spot",      "group": "crypto", "name": "ETHUSD", "symbol": "ETH-USD", "decimals": 2 }
{ "asset_class": "synthetic", "group": "volatility", "name": "Volatility 75", "symbol": "R_75" }
```

`asset_class` selects the feed: `forex` / `index` / `spot` use Yahoo Finance
(carrying a Yahoo `symbol`, or `base`+`quote` for FX); `synthetic` uses the
Deriv WebSocket API and carries the Deriv `symbol` (e.g. `R_10`).

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
