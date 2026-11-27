"""Market-data feeds for the signal engine.

Every feed returns ``(series, label)`` where ``series`` is a list of
``(timestamp_seconds, price)`` points in ascending time order.

Sources are public and need no API key:

  crypto      -> Binance spot klines
  stock       -> Yahoo Finance chart API
  forex       -> Frankfurter (ECB reference rates)
  prediction  -> Polymarket (implied probability of an outcome)

When Liquid's REST API / Co-Invest MCP is available, swap these
functions out; the rest of the engine only depends on the ``(series, label)``
contract.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

UA = {"User-Agent": "Mozilla/5.0 (compatible; signal-engine/0.1)"}


def _get_json(url: str, headers: dict | None = None, timeout: int = 25):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def crypto(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 300):
    """Binance spot klines -> close price series."""
    url = (f"https://api.binance.com/api/v3/klines"
           f"?symbol={symbol}&interval={interval}&limit={limit}")
    rows = _get_json(url)
    series = [(int(r[0]) // 1000, float(r[4])) for r in rows]
    return series, f"{symbol} {interval}"


def stock(symbol: str = "AAPL", period: str = "6mo", interval: str = "1d"):
    """Yahoo Finance chart -> close price series."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={period}&interval={interval}")
    data = _get_json(url)
    res = data["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    series = [(int(t), float(c)) for t, c in zip(ts, closes) if c is not None]
    return series, f"{symbol} {interval}"


def forex(base: str = "EUR", quote: str = "USD", days: int = 180):
    """Frankfurter daily reference rates -> price series."""
    end = date.today()
    start = end - timedelta(days=days)
    url = (f"https://api.frankfurter.dev/v1/{start}..{end}"
           f"?base={base}&symbols={quote}")
    data = _get_json(url)
    rates = data["rates"]
    series = []
    for day in sorted(rates):
        ts = int(datetime.fromisoformat(day).timestamp())
        series.append((ts, float(rates[day][quote])))
    return series, f"{base}/{quote} daily"


def yahoo_ohlc(symbol: str, period: str = "1mo", interval: str = "1h"):
    """Yahoo Finance chart -> list of OHLC dicts, for any symbol.

    Works for FX ("EURUSD=X"), cash indices ("^GSPC"), volatility indices
    ("^VIX") and equities alike, and supports intraday intervals
    (1m/5m/15m/1h) so candles can be drawn on short timeframes.
    """
    sym = urllib.parse.quote(symbol, safe="=^")
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?range={period}&interval={interval}")
    data = _get_json(url)
    res = data["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    candles = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        candles.append({"t": int(t), "o": float(o), "h": float(h),
                        "l": float(l), "c": float(c)})
    return candles, f"{symbol} {interval}"


def forex_ohlc(base: str = "EUR", quote: str = "USD",
               period: str = "1mo", interval: str = "1h"):
    """Yahoo Finance FX candles -> list of OHLC dicts.

    Frankfurter only serves daily closes; Yahoo gives intraday OHLC so we can
    draw candlesticks on intraday timeframes.
    """
    candles, _ = yahoo_ohlc(f"{base}{quote}=X", period, interval)
    return candles, f"{base}/{quote} {interval}"


# --- Deriv synthetic indices (Volatility 10/25/50/100) ---------------------
#
# Deriv exposes synthetic indices only over a WebSocket API. We speak the
# minimum of RFC 6455 needed to request candle history, so there is no third
# party dependency. Outbound traffic is tunnelled through HTTP(S)_PROXY when
# one is configured (sandboxes), and dialled directly otherwise (CI runners).

GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
DERIV_HOST = "ws.derivws.com"
DERIV_PATH = "/websockets/v3?app_id=1089"


def _open_socket(host: str, port: int, timeout: int = 20):
    """TCP connect, via HTTP CONNECT tunnel when a proxy is configured."""
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    bypass = any(host == h.strip() for h in no_proxy.split(",") if h.strip())
    if not proxy or bypass:
        return socket.create_connection((host, port), timeout=timeout)
    pu = urllib.parse.urlparse(proxy)
    raw = socket.create_connection((pu.hostname, pu.port), timeout=timeout)
    raw.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = raw.recv(4096)
        if not chunk:
            raise ConnectionError("proxy closed the connection")
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b" 200 " not in status:
        raise RuntimeError(f"proxy CONNECT failed: {status!r}")
    return raw


def _ws_connect(host: str, path: str, timeout: int = 20):
    ctx = ssl.create_default_context()
    raw = _open_socket(host, 443, timeout)
    sock = ctx.wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: https://app.deriv.com\r\n\r\n".encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("websocket handshake closed")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n", 1)[0]:
        raise RuntimeError(f"websocket handshake failed: {head.splitlines()[0]!r}")
    return sock, rest


def _recv_exact(sock, n: int, buf: bytes):
    while len(buf) < n:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf[:n], buf[n:]


def _read_frame(sock, buf: bytes):
    hdr, buf = _recv_exact(sock, 2, buf)
    opcode = hdr[0] & 0x0F
    masked = hdr[1] & 0x80
    length = hdr[1] & 0x7F
    if length == 126:
        ext, buf = _recv_exact(sock, 2, buf)
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext, buf = _recv_exact(sock, 8, buf)
        length = struct.unpack(">Q", ext)[0]
    maskkey = b""
    if masked:
        maskkey, buf = _recv_exact(sock, 4, buf)
    payload, buf = _recv_exact(sock, length, buf)
    if masked:
        payload = bytes(b ^ maskkey[i % 4] for i, b in enumerate(payload))
    return opcode, payload, buf


def _ws_send(sock, data, opcode: int = 0x1):
    if isinstance(data, str):
        data = data.encode()
    mask = os.urandom(4)
    n = len(data)
    header = bytearray([0x80 | opcode])
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    header += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header))


def deriv_ohlc(symbol: str = "R_10", granularity: int = 3600, count: int = 300):
    """Deriv synthetic-index candles -> list of OHLC dicts.

    Symbols R_10 / R_25 / R_50 / R_100 map to Volatility 10 / 25 / 50 / 100.
    ``granularity`` is the candle width in seconds.
    """
    sock, buf = _ws_connect(DERIV_HOST, DERIV_PATH)
    try:
        _ws_send(sock, json.dumps({
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "granularity": granularity,
            "style": "candles",
        }))
        deadline = time.time() + 25
        while time.time() < deadline:
            opcode, payload, buf = _read_frame(sock, buf)
            if opcode == 0x8:
                break
            if opcode == 0x9:  # ping -> pong
                _ws_send(sock, payload, opcode=0xA)
                continue
            if opcode != 0x1:
                continue
            msg = json.loads(payload)
            if "error" in msg:
                raise RuntimeError("deriv: " + str(msg["error"].get("message")))
            if msg.get("msg_type") == "candles":
                candles = [
                    {"t": int(c["epoch"]), "o": float(c["open"]),
                     "h": float(c["high"]), "l": float(c["low"]),
                     "c": float(c["close"])}
                    for c in msg["candles"]
                ]
                if candles:
                    return candles, f"{symbol} g{granularity}"
        raise RuntimeError(f"deriv: no candles returned for {symbol}")
    finally:
        sock.close()


def candles_for(asset_class: str, symbol: str, tf: str, period: str = "1mo"):
    """Unified OHLC loader: Deriv synthetics vs Yahoo (fx / index / spot)."""
    if asset_class == "synthetic":
        return deriv_ohlc(symbol, GRANULARITY.get(tf, 3600), 300)
    return yahoo_ohlc(symbol, period, tf)


def prediction(slug: str):
    """Polymarket implied-probability history for the first outcome."""
    markets = _get_json(f"https://gamma-api.polymarket.com/markets?slug={slug}")
    if not markets:
        raise ValueError(f"no Polymarket market for slug {slug!r}")
    m = markets[0]
    outcome = json.loads(m["outcomes"])[0]
    token = json.loads(m["clobTokenIds"])[0]
    hist = _get_json("https://clob.polymarket.com/prices-history"
                     f"?market={token}&interval=1w&fidelity=60")
    series = [(int(p["t"]), float(p["p"])) for p in hist.get("history", [])]
    return series, f"[{outcome}] {m['question'][:46]}"


FEEDS = {
    "crypto": crypto,
    "stock": stock,
    "forex": forex,
    "prediction": prediction,
}


def load(item: dict):
    """Load one watchlist item -> ``(series, label)``.

    Only keys the target feed accepts are passed, so a single watchlist can
    carry feed-specific options (e.g. crypto ``interval`` vs forex ``period``).
    """
    import inspect

    kind = item["asset_class"]
    fn = FEEDS[kind]
    accepted = inspect.signature(fn).parameters
    kwargs = {k: v for k, v in item.items()
              if k != "asset_class" and k in accepted}
    return fn(**kwargs)
