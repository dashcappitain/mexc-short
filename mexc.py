#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MEXC Futures Short-Only MA Cross Bot (pymexc)
- TF 5 menit
- Cross leverage 20x (one-way mode)
- Open SHORT saat MA5 cross-down MA10
- Layering 5x: [20, 30, 40, 50, 60] USDT
- Tambah layer hanya bila harga cross berikutnya > harga layer terakhir
- API key & secret via .env

Jalankan:
  python bot.py --symbol BTC_USDT
  python bot.py --symbol ETH_USDT --dry-run
"""
import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv

try:
    from pymexc import futures
except Exception as e:
    print("ERROR: gagal import 'pymexc'. Jalankan: pip install pymexc", file=sys.stderr)
    raise

# ------------------ Konfigurasi Tetap ------------------
INTERVAL = "Min5"  # K-Line 5 menit
LAYER_USDT = [20, 30, 40, 50, 60]
MAX_LAYERS = len(LAYER_USDT)
POLL_SECONDS_DEFAULT = 10

# ------------------ Util & MA ------------------
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

def ts_ms() -> int:
    return int(time.time() * 1000)

def sma(values: List[float], period: int) -> List[Optional[float]]:
    """Simple moving average; None untuk indeks < period-1"""
    out: List[Optional[float]] = [None] * len(values)
    run_sum = 0.0
    for i, v in enumerate(values):
        run_sum += v
        if i >= period:
            run_sum -= values[i - period]
        if i >= period - 1:
            out[i] = run_sum / period
    return out

def norm_symbol(sym: str) -> str:
    s = sym.upper().replace("-", "_")
    if s.endswith("USDT") and "_" not in s:
        return s.replace("USDT", "_USDT")
    return s

def read_env():
    load_dotenv()
    key = os.getenv("MEXC_API_KEY")
    sec = os.getenv("MEXC_API_SECRET")
    if not key or not sec:
        print("ENV ERROR: set MEXC_API_KEY dan MEXC_API_SECRET di .env", file=sys.stderr)
        sys.exit(2)
    return key, sec

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

# ------------------ State ------------------
@dataclass
class BotState:
    symbol: str
    last_cross_candle_time: Optional[int] = None  # ms of cross candle open time
    layers_opened: int = 0
    last_layer_price: Optional[float] = None
    layer_prices: List[float] = field(default_factory=list)

    @classmethod
    def load(cls, path: str, symbol: str) -> "BotState":
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
            return cls(**data)
        return cls(symbol=symbol)

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump(self.__dict__, f, ensure_ascii=False, indent=2)

# ------------------ MEXC Adapter ------------------
class MexcFuturesClient:
    """
    Pembungkus fungsi-fungsi pymexc Futures yang dipakai bot.
    Metode penting:
      - kline(symbol, interval)
      - ticker(symbol)
      - get_position_mode(), change_position_mode(position_mode=2)  # 2=one-way
      - change_leverage(open_type=2, symbol=..., position_type=2, leverage=20)  # cross 20x (short)
      - order(symbol, vol, side=3, type=5, open_type=2)  # market short
    """
    def __init__(self, api_key: str, api_secret: str):
        self.http = futures.HTTP(api_key=api_key, api_secret=api_secret)

    def get_klines_5m(self, symbol: str, limit: int = 210) -> List[Dict[str, Any]]:
        resp = self.http.kline(symbol=symbol, interval=INTERVAL)
        kl = []
        for row in resp:
            if isinstance(row, dict):
                ts = int(row.get("t") or row.get("time") or row.get("timestamp") or 0)
                o = float(row.get("o") or row.get("open"))
                h = float(row.get("h") or row.get("high"))
                l = float(row.get("l") or row.get("low"))
                c = float(row.get("c") or row.get("close"))
                v = float(row.get("v") or row.get("vol") or 0)
            else:
                # asumsi list [ts, o, h, l, c, v]
                ts = int(row[0])
                o, h, l, c, v = map(float, row[1:6])
            kl.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "vol": v})
        kl.sort(key=lambda x: x["ts"])
        return kl[-limit:]

    def get_last_price(self, symbol: str) -> float:
        t = self.http.ticker(symbol=symbol)
        if isinstance(t, list):
            t = t[0]
        for key in ("lastPrice", "last_price", "last"):
            if key in t:
                return float(t[key])
        return float(t.get("price") or t.get("p") or 0.0)

    def ensure_one_way_mode(self):
        try:
            mode = self.http.get_position_mode()
            if isinstance(mode, dict) and mode.get("positionMode") == 2:
                return
            self.http.change_position_mode(position_mode=2)  # 2 = one-way
        except Exception as e:
            print(f"[WARN] gagal set one-way mode: {e}")

    def ensure_cross_20x(self, symbol: str):
        try:
            # set leverage cross 20x untuk posisi short (position_type=2)
            self.http.change_leverage(open_type=2, symbol=symbol, position_type=2, leverage=20)
        except Exception as e:
            print(f"[WARN] gagal set leverage cross 20x: {e}")

    def market_short(self, symbol: str, qty_base: float, client_oid: Optional[str] = None) -> Dict[str, Any]:
        params = {
            "symbol": symbol,
            "vol": float(qty_base),
            "side": 3,        # 3 = open short
            "type": 5,        # 5 = market
            "open_type": 2,   # 2 = cross
        }
        if client_oid:
            params["external_oid"] = client_oid
        return self.http.order(**params)

# ------------------ Strategi ------------------
def last_two_closed(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ambil dua candle terakhir yang SUDAH CLOSE (abaikan candle berjalan)."""
    if not candles:
        return []
    now_ms = ts_ms()
    five_min_ms = 5 * 60 * 1000
    closed = [c for c in candles if now_ms >= (c["ts"] + five_min_ms)]
    if len(closed) < 2:
        return []
    return closed[-2:]

def detect_cross_down(ma5: List[Optional[float]], ma10: List[Optional[float]], last_idx: int) -> bool:
    """True jika terjadi cross-down pada indeks last_idx (candle terbaru yang sudah close)."""
    if last_idx < 1 or ma5[last_idx] is None or ma10[last_idx] is None:
        return False
    prev = last_idx - 1
    d_prev = (ma5[prev] or 0) - (ma10[prev] or 0)
    d_curr = (ma5[last_idx] or 0) - (ma10[last_idx] or 0)
    return d_prev >= 0 and d_curr < 0

def qty_from_usdt(notional_usdt: float, price: float, precision: int = 6) -> float:
    """Konversi nominal USDT -> qty base (pembulatan turun)."""
    if price <= 0:
        return 0.0
    qty = notional_usdt / price
    factor = 10 ** precision
    return math.floor(qty * factor) / factor

# ------------------ Main Loop ------------------
def run(symbol: str, poll_seconds: int, dry_run: bool, state_dir: str):
    api_key, api_secret = read_env()
    symbol = norm_symbol(symbol)
    ensure_dir(state_dir)
    state_path = os.path.join(state_dir, f"state_{symbol}.json")
    st = BotState.load(state_path, symbol)

    mx = MexcFuturesClient(api_key, api_secret)
    mx.ensure_one_way_mode()
    mx.ensure_cross_20x(symbol)

    print(f"[{now_iso()}] Bot start for {symbol} | dry_run={dry_run}")
    while True:
        try:
            candles = mx.get_klines_5m(symbol, limit=210)
            closes = [c["close"] for c in candles]
            ma5 = sma(closes, 5)
            ma10 = sma(closes, 10)

            two = last_two_closed(candles)
            if len(two) < 2:
                time.sleep(poll_seconds)
                continue

            last_closed = two[-1]
            last_idx = candles.index(last_closed)
            signal_price = last_closed["close"]
            signal_ts = last_closed["ts"]

            crossed = detect_cross_down(ma5, ma10, last_idx)
            if not crossed:
                time.sleep(poll_seconds)
                continue

            # Hindari double-trigger pada candle yang sama
            if st.last_cross_candle_time == signal_ts:
                time.sleep(poll_seconds)
                continue

            # Tentukan layer berikutnya
            next_layer = st.layers_opened + 1
            if next_layer > MAX_LAYERS:
                print(f"[{now_iso()}] {symbol} CROSS-DOWN @ {signal_price:.6f} | Layers full ({st.layers_opened}/{MAX_LAYERS}) -> SKIP")
                st.last_cross_candle_time = signal_ts
                st.save(state_path)
                time.sleep(poll_seconds)
                continue

            # Rule layering: hanya tambah jika harga cross baru > harga layer terakhir
            if st.last_layer_price is not None and signal_price <= st.last_layer_price:
                print(f"[{now_iso()}] {symbol} CROSS-DOWN @ {signal_price:.6f} <= last_layer_price {st.last_layer_price:.6f} -> SKIP add-layer")
                st.last_cross_candle_time = signal_ts
                st.save(state_path)
                time.sleep(poll_seconds)
                continue

            # Hitung qty base dari notional USDT
            notional = LAYER_USDT[next_layer - 1]
            price_for_qty = signal_price or mx.get_last_price(symbol)
            qty = qty_from_usdt(notional, price_for_qty, precision=6)

            print(f"[{now_iso()}] {symbol} CROSS-DOWN @ {signal_price:.6f} -> OPEN SHORT LAYER-{next_layer} notional={notional}USDT qty≈{qty}")
            if qty <= 0:
                print(f"[WARN] Qty=0, batal order.")
            else:
                if not dry_run:
                    oid = f"ma5x10_{symbol}_{signal_ts}_{next_layer}"
                    resp = mx.market_short(symbol=symbol, qty_base=qty, client_oid=oid)
                    print(f"[ORDER] Response: {resp}")
                else:
                    print("[DRY-RUN] (order tidak dikirim)")

                # Update state
                st.layers_opened = next_layer
                st.last_layer_price = signal_price
                st.layer_prices.append(signal_price)

            st.last_cross_candle_time = signal_ts
            st.save(state_path)

        except KeyboardInterrupt:
            print("\nStop by user.")
            break
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr)
        time.sleep(poll_seconds)

def parse_args():
    p = argparse.ArgumentParser(description="MEXC Futures Short-Only Bot (MA5 cross-down MA10; 5m)")
    p.add_argument("--symbol", required=True, help="Contoh: BTC_USDT, ETH_USDT")
    p.add_argument("--poll-seconds", type=int, default=POLL_SECONDS_DEFAULT, help="Interval polling (detik)")
    p.add_argument("--dry-run", action="store_true", help="Hanya log, tidak kirim order")
    p.add_argument("--state-dir", default="./state", help="Folder simpan state")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    run(symbol=args.symbol, poll_seconds=args.poll_seconds, dry_run=args.dry_run, state_dir=args.state_dir)
