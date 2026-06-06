"""
回测引擎 — 数据加载 + 指标计算 + 模拟引擎 + 报告
统一 backtest.py / backtest2.py / backtest3_regime.py / backtest4_trend.py
"""
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "backtest_data"
DATA_DIR.mkdir(exist_ok=True)

# ── 多币种映射 ──────────────────────────────────────────────
SYMBOLS = {
    "TRX":      "TRX-USDT",
    "ETH":      "ETH-USDT",
    "SOL":      "SOL-USDT",
    "TRX_SWAP": "TRX-USDT-SWAP",
    "ETH_SWAP": "ETH-USDT-SWAP",
    "SOL_SWAP": "SOL-USDT-SWAP",
}

BINANCE_MAP = {"TRX": "TRXUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}


# ═══════════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════════

def load_from_cache(name: str, interval: str = "1h", days: int = 180) -> Optional[pd.DataFrame]:
    """从本地 JSON 缓存加载（Binance 格式）。"""
    sym = BINANCE_MAP.get(name.split("_")[0])
    if not sym:
        return None
    path = DATA_DIR / f"{sym}_{interval}_{days}d.json"
    if not path.exists():
        return None
    with open(path) as f:
        raw = json.load(f)
    rows, seen = [], set()
    for r in raw:
        ts = r[0]
        if ts in seen:
            continue
        seen.add(ts)
        rows.append({
            "ts": ts, "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "vol": float(r[5]),
        })
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms")
    cutoff = df["ts"].max() - days * 86_400_000
    return df[df["ts"] >= cutoff].reset_index(drop=True)


def download_okx_candles(symbol: str, bar: str = "15m", days: int = 90,
                         bars_per_batch: int = 300) -> Optional[pd.DataFrame]:
    """从 OKX REST API 分批下载历史K线。"""
    cache_path = DATA_DIR / f"{symbol.replace('-','_')}_{days}d.csv"
    if cache_path.exists():
        df = pd.read_csv(cache_path)
        if len(df) > 500:
            df["dt"] = pd.to_datetime(df["dt"])
            return df

    print(f"  下载 {symbol} 历史K线 (OKX)...")
    import requests
    all_data = []
    after = ""
    max_batches = (days * 24 * 4) // bars_per_batch + 5

    for _ in range(max_batches):
        url = "https://www.okx.com/api/v5/market/candles"
        params = {"instId": symbol, "bar": bar, "limit": str(bars_per_batch)}
        if after:
            params["after"] = after
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            body = resp.json()
            if body.get("code") != "0":
                print(f"    API错误: {body.get('msg')}")
                break
            raw = body["data"]
            if not raw:
                break
            all_data.extend(raw)
            after = raw[-1][0]
            time.sleep(0.25)
            if len(raw) < bars_per_batch:
                break
        except Exception as e:
            print(f"    异常: {e}")
            break

    if not all_data:
        return None
    rows = [{"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
             "low": float(r[3]), "close": float(r[4]), "vol": float(r[5])} for r in all_data]
    df = pd.DataFrame(rows).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms")
    df.to_csv(cache_path, index=False)
    print(f"    ✓ {len(df)} 根K线")
    return df


# ═══════════════════════════════════════════════════════════
# 向量化指标（一次计算，所有回测复用）
# ═══════════════════════════════════════════════════════════

@dataclass
class IndicatorPack:
    """一次计算全部技术指标，各模拟引擎复用。"""
    close: np.ndarray
    high: np.ndarray
    low: np.ndarray
    vol: np.ndarray
    ema20: np.ndarray
    ema60: np.ndarray
    bb_width: np.ndarray
    atr: np.ndarray
    atr_pct: np.ndarray
    rsi: np.ndarray
    ema12: np.ndarray = field(default_factory=lambda: np.array([]))
    ema26: np.ndarray = field(default_factory=lambda: np.array([]))
    adx: np.ndarray = field(default_factory=lambda: np.array([]))
    vwap: np.ndarray = field(default_factory=lambda: np.array([]))
    regimes: list = field(default_factory=list)

    def __post_init__(self):
        n = len(self.close)
        if len(self.ema12) == 0:
            self.ema12 = pd.Series(self.close).ewm(span=12, adjust=False).mean().values
        if len(self.ema26) == 0:
            self.ema26 = pd.Series(self.close).ewm(span=26, adjust=False).mean().values


def calc_indicators(df: pd.DataFrame, min_warmup: int = 60) -> IndicatorPack:
    """向量化计算全部指标。"""
    close = df["close"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    vol   = df["vol"].values.astype(float)
    n = len(close)

    # EMA
    ema20 = pd.Series(close).ewm(span=20, adjust=False).mean().values
    ema60 = pd.Series(close).ewm(span=60, adjust=False).mean().values
    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean().values

    # BB(20, 2)
    roll_ma  = pd.Series(close).rolling(20).mean().values
    roll_std = pd.Series(close).rolling(20).std().values
    bb_width = np.divide(roll_std * 4, roll_ma, out=np.zeros_like(roll_ma), where=roll_ma != 0)

    # ATR(14)
    tr = np.maximum(high - low, np.abs(high - np.roll(close, 1)))
    tr = np.maximum(tr, np.abs(low - np.roll(close, 1)))
    tr[0] = high[0] - low[0]
    atr = pd.Series(tr).ewm(span=14, adjust=False).mean().values
    atr_pct = np.divide(atr, close, out=np.zeros_like(atr), where=close != 0)

    # RSI(14)
    delta = np.diff(close, prepend=close[0])
    gain = np.maximum(delta, 0)
    loss = np.maximum(-delta, 0)
    avg_gain = pd.Series(gain).ewm(span=14, adjust=False).mean().values
    avg_loss = pd.Series(loss).ewm(span=14, adjust=False).mean().values
    safe_loss = np.where(avg_loss < 1e-10, 1e-10, avg_loss)
    rsi = 100 - 100 / (1 + avg_gain / safe_loss)

    # ADX(14)
    up_move   = np.maximum(high - np.roll(high, 1), 0)
    down_move = np.maximum(np.roll(low, 1) - low, 0)
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    tr14 = atr * 14  # approx: ewm X 14 ≈ sum
    plus_di  = np.divide(pd.Series(plus_dm).ewm(span=14, adjust=False).mean().values,
                         np.maximum(tr14, 1e-10)) * 100
    minus_di = np.divide(pd.Series(minus_dm).ewm(span=14, adjust=False).mean().values,
                         np.maximum(tr14, 1e-10)) * 100
    dx = np.divide(np.abs(plus_di - minus_di),
                   np.maximum(plus_di + minus_di, 1e-10)) * 100
    adx = pd.Series(dx).ewm(span=14, adjust=False).mean().values

    # VWAP(20)
    vwap = np.full(n, close[-1])
    for i in range(20, n):
        sl = slice(i - 20, i)
        s = vol[sl].sum()
        if s > 0:
            vwap[i] = (close[sl] * vol[sl]).sum() / s

    # Regime 判定
    regimes = []
    for i in range(n):
        if i < min_warmup:
            regimes.append("warmup")
            continue
        ed = abs(ema20[i] - ema60[i]) / max(ema60[i], 1e-10)
        bw = bb_width[i]
        ap = atr_pct[i]

        dead = bw < 0.012 and ap < 0.005 and ed < 0.005
        rng  = bw < 0.030 and ed < 0.015 and ap < 0.015
        trd  = ed > 0.007 and bw > 0.012 and ap > 0.002 and adx[i] > 25

        if dead or rng:
            regimes.append("ranging")
        elif trd:
            regimes.append("trending_up" if ema20[i] > ema60[i] else "trending_down")
        else:
            regimes.append("wait")

    return IndicatorPack(
        close=close, high=high, low=low, vol=vol,
        ema20=ema20, ema60=ema60, ema12=ema12, ema26=ema26,
        bb_width=bb_width, atr=atr, atr_pct=atr_pct,
        rsi=rsi, adx=adx, vwap=vwap, regimes=regimes,
    )
