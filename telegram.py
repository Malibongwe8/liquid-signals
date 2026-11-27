"""Minimal Telegram Bot API client (stdlib only).

Credentials are resolved in this order (first one found wins):

  1. environment variables ``TELEGRAM_TOKEN`` / ``TELEGRAM_CHAT_ID``
     (used by GitHub Actions, fed from repository secrets)
  2. a git-ignored ``secrets.json`` next to this file:
     ``{"telegram_token": "...", "telegram_chat_id": 123}``
  3. ``config.json`` under ``telegram`` (blank in the committed repo)

All requests go through urllib, which honours the sandbox HTTP(S)_PROXY
automatically.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _config():
    return json.loads((ROOT / "config.json").read_text())


def _secrets():
    path = ROOT / "secrets.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _token():
    tok = os.environ.get("TELEGRAM_TOKEN")
    if tok:
        return tok
    sec = _secrets()
    if sec.get("telegram_token"):
        return sec["telegram_token"]
    return _config()["telegram"].get("token", "")


def chat_id():
    """Configured chat id from env / secrets.json / config.json, else None."""
    cid = os.environ.get("TELEGRAM_CHAT_ID")
    if cid:
        return int(cid)
    sec = _secrets()
    if sec.get("telegram_chat_id"):
        return int(sec["telegram_chat_id"])
    cfg_cid = _config()["telegram"].get("chat_id")
    return cfg_cid or None


def _api(method, params=None):
    url = f"https://api.telegram.org/bot{_token()}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def send_message(chat_id, text):
    return _api("sendMessage", {"chat_id": chat_id, "text": text})


def send_photo(chat_id, png_path, caption=""):
    boundary = "----tg" + uuid.uuid4().hex
    png = Path(png_path).read_bytes()
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
         f'filename="chart.png"\r\nContent-Type: image/png\r\n\r\n').encode()
        + png + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{_token()}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def resolve_chat_id():
    """Find the most recent chat that messaged the bot."""
    updates = _api("getUpdates")
    for u in reversed(updates.get("result", [])):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat and "id" in chat:
            return chat["id"]
    raise RuntimeError(
        "No Telegram chat found. Open Telegram, message @AmaterazuBot with "
        "/start, then retry."
    )
