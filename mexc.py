#!/usr/bin/env python3
"""MEXC MA5/MA10 cross‑down notifier.

Mengambil data kandil 5 menit dari API publik MEXC dan mengirim notifikasi
Telegram ketika MA5 memotong ke bawah MA10. Bot ini tidak melakukan trading.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional
from urllib import parse, request

INTERVAL = "Min5"  # timeframe 5 menit
POLL_SECONDS_DEFAULT = 10


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ts_ms() -> int:
    return int(time.time() * 1000)


def sma(values: List[float], period: int) -> List[Optional[float]]:
    """Hitung simple moving average."""
    out: List[Optional[float]] = [None] * len(values)
    run_sum = 0.0
    for i, v in enumerate(values):
        run_sum += v
        if i >= period:
            run_sum -= values[i - period]
        if i >= period - 1:
            out[i] = run_sum / period
    return out


def last_two_closed(candles: List[Dict[str, float]]) -> List[Dict[str, float]]:
    """Ambil dua candle terakhir yang sudah close."""
    now = ts_ms()
    five = 5 * 60 * 1000
    closed = [c for c in candles if now >= c["ts"] + five]
    if len(closed) < 2:
        return []
    return closed[-2:]


def detect_cross_down(ma5: List[Optional[float]], ma10: List[Optional[float]], idx: int) -> bool:
    """True jika MA5 memotong ke bawah MA10 pada indeks idx."""
    if idx < 1 or ma5[idx] is None or ma10[idx] is None:
        return False
    prev = idx - 1
    return (ma5[prev] - ma10[prev]) >= 0 and (ma5[idx] - ma10[idx]) < 0


def load_env(path: str = ".env") -> Dict[str, str]:
    env: Dict[str, str] = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def read_env() -> Dict[str, str]:
    env = load_env()
    tg_token = env.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = env.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not tg_token or not tg_chat:
        print(
            "ENV ERROR: set TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID di .env",
            file=sys.stderr,
        )
        sys.exit(2)
    return {"token": tg_token, "chat": tg_chat}


def fetch_json(url: str, params: Optional[Dict[str, str]] = None) -> Dict[str, any]:
    if params:
        url = f"{url}?{parse.urlencode(params)}"
    with request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def get_klines_5m(symbol: str, limit: int = 210) -> List[Dict[str, float]]:
    url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}"
    data = fetch_json(url, {"interval": INTERVAL, "limit": str(limit)})
    rows = data.get("data") if isinstance(data, dict) else data
    kl: List[Dict[str, float]] = []
    for row in rows:
        ts = int(
            row.get("t")
            or row.get("time")
            or row.get("timestamp")
            or row.get("id")
            or 0
        )
        if ts < 1_000_000_000_000:  # detik -> ms
            ts *= 1000
        o = float(row.get("o") or row.get("open"))
        h = float(row.get("h") or row.get("high"))
        l = float(row.get("l") or row.get("low"))
        c = float(row.get("c") or row.get("close"))
        v = float(row.get("v") or row.get("vol") or 0)
        kl.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "vol": v})
    kl.sort(key=lambda x: x["ts"])
    return kl[-limit:]


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = request.Request(url, data=data)
    try:
        with request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"[WARN][Telegram] {e}")


def format_msg(symbol: str, price: float) -> str:
    return "\n".join(
        [
            "*M5 Cross-Down*",
            f"*Symbol:* `{symbol}`",
            f"*Price:* `{price:.6f}`",
            f"_Time: {now_iso()}_",
        ]
    )


def run(symbol: str, poll_seconds: int) -> None:
    env = read_env()
    tg_token, tg_chat = env["token"], env["chat"]
    last_notified: Optional[int] = None
    symbol = symbol.upper()
    while True:
        try:
            candles = get_klines_5m(symbol)
            closes = [c["close"] for c in candles]
            ma5 = sma(closes, 5)
            ma10 = sma(closes, 10)

            two = last_two_closed(candles)
            if len(two) < 2:
                time.sleep(poll_seconds)
                continue

            last_closed = two[-1]
            idx = candles.index(last_closed)

            if last_notified == last_closed["ts"]:
                time.sleep(poll_seconds)
                continue

            if detect_cross_down(ma5, ma10, idx):
                send_telegram(tg_token, tg_chat, format_msg(symbol, last_closed["close"]))
                last_notified = last_closed["ts"]

        except KeyboardInterrupt:
            print("Stop by user.")
            break
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr)

        time.sleep(poll_seconds)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Kirim notifikasi Telegram ketika MA5 cross-down MA10 (TF 5m)."
    )
    p.add_argument("--symbol", required=True, help="Contoh: BTC_USDT")
    p.add_argument(
        "--poll-seconds",
        type=int,
        default=POLL_SECONDS_DEFAULT,
        help="Interval polling dalam detik",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.symbol, args.poll_seconds)

