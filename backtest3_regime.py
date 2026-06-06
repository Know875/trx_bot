#!/usr/bin/env python3
"""行情检测准确率验证 v2 —— 向量化计算指标"""
import json, os, sys
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "backtest_data")
SYMBOLS_MAP = {"TRX": "TRXUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}

def load_df(name):
    sym = SYMBOLS_MAP[name]
    path = os.path.join(DATA_DIR, f"{sym}_{'1h'}_180d.json")
    with open(path) as f:
        raw = json.load(f)
    rows, seen = [], set()
    for r in raw:
        ts = r[0]
        if ts in seen: continue
        seen.add(ts)
        rows.append({"ts": ts, "open": float(r[1]), "high": float(r[2]),
                      "low": float(r[3]), "close": float(r[4]), "vol": float(r[5])})
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms")
    cutoff = df["ts"].max() - 180 * 86400000
    df = df[df["ts"] >= cutoff].reset_index(drop=True)
    # 降采样到4h减少计算量
    df = df.set_index("dt").resample("4h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "vol": "sum"
    }).dropna().reset_index()
    return df

# ── 向量化指标 ──
def precompute_indicators(df):
    """一次性计算全部指标"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    vol = df["vol"].values
    n = len(close)
    
    # EMA
    ema20 = pd.Series(close).ewm(span=20, adjust=False).mean().values
    ema60 = pd.Series(close).ewm(span=60, adjust=False).mean().values
    
    # BB(20,2)
    roll_std = pd.Series(close).rolling(20).std().values
    roll_ma = pd.Series(close).rolling(20).mean().values
    bb_width = (roll_std * 4) / roll_ma  # 2σ * 2 = 4σ
    
    # ATR(14)
    tr = np.maximum(high - low, np.abs(high - np.roll(close, 1)))
    tr = np.maximum(tr, np.abs(low - np.roll(close, 1)))
    tr[0] = high[0] - low[0]
    atr = pd.Series(tr).rolling(14).mean().values
    atr_pct = atr / close
    
    # VWAP
    vwap = np.full(n, close[-1])
    for i in range(20, n):
        sl = slice(i-20, i)
        s = vol[sl].sum()
        vwap[i] = (close[sl] * vol[sl]).sum() / s if s > 0 else close[i]
    
    regimes = []
    for i in range(n):
        if i < 60:
            regimes.append("warmup")
            continue
        ed = abs(ema20[i] - ema60[i]) / ema60[i]
        bw = bb_width[i]
        ap = atr_pct[i]
        
        dead = bw < 0.012 and ap < 0.005 and ed < 0.005
        rng = bw < 0.030 and ed < 0.015 and ap < 0.015
        trd = ed > 0.007 and bw > 0.012 and ap > 0.002
        
        if dead or rng: regimes.append("ranging")
        elif trd: regimes.append("trending")
        else: regimes.append("wait")
    
    return regimes

# ── 网格模拟（只在filter_regime期间建仓）──
def simulate(df, regimes, grid_w, grid_n, filter_regime=None, capital=1000):
    usdt, coin = capital, 0.0
    buys, sells = [], []
    trades = 0
    
    for i in range(60, len(df)):
        p_high, p_low, p_close = df.iloc[i]["high"], df.iloc[i]["low"], df.iloc[i]["close"]
        regime = regimes[i]
        
        # ── 成交检查 ──
        new_buys = []
        for bp, sz in buys:
            if p_low <= bp and sz <= usdt:
                usdt -= sz; coin += sz/bp
                sells.append((bp*(1+grid_w), sz/bp))
                trades += 1
            else:
                new_buys.append((bp, sz))
        buys = new_buys
        
        new_sells = []
        for sp, sz in sells:
            if p_high >= sp and sz <= coin:
                coin -= sz; usdt += sz*sp
                buys.append((sp*(1-grid_w), sz*sp))
                trades += 1
            else:
                new_sells.append((sp, sz))
        sells = new_sells
        
        # ── 建仓 ──
        if filter_regime is not None and regime != filter_regime:
            continue
        if not buys and not sells and coin < 1e-8 and usdt > 0:
            per = usdt / grid_n
            for j in range(grid_n):
                buys.append((p_close*(1-grid_w*(j+1)/grid_n), per))
                sells.append((p_close*(1+grid_w*(j+1)/grid_n), 0))
        elif not buys and usdt > capital * 0.1:
            per = usdt / grid_n
            for j in range(grid_n):
                buys.append((p_close*(1-grid_w*(j+1)/grid_n), per))
    
    final = usdt + coin * df.iloc[-1]["close"]
    pnl = final - capital
    return pnl / capital * 100, trades

# ── Main ──
BEST = {"TRX": (0.01, 9), "ETH": (0.05, 9), "SOL": (0.02, 3)}

print("╔══════════════════════════════════════════════════════╗")
print("║  行情检测准确率验证 (4h K线, 180天)                  ║")
print("╚══════════════════════════════════════════════════════╝")

for name in ["TRX", "ETH", "SOL"]:
    w, n = BEST[name]
    print(f"\n{'='*55}")
    print(f"  {name} | 网格±{w:.0%} {n}档")
    
    df = load_df(name)
    regimes = precompute_indicators(df)
    
    # 行情分布
    from collections import Counter
    cnt = Counter(r for r in regimes if r != "warmup")
    total = sum(cnt.values())
    print(f"  行情分布: ranging={cnt['ranging']/total*100:.0f}%  trending={cnt['trending']/total*100:.0f}%  wait={cnt['wait']/total*100:.0f}%")
    
    # 三种策略
    p_all, t_all = simulate(df, regimes, w, n, filter_regime=None)
    p_rng, t_rng = simulate(df, regimes, w, n, filter_regime="ranging")
    p_trd, t_trd = simulate(df, regimes, w, n, filter_regime="trending")
    
    print(f"  {'策略':<18} {'PnL%':>8} {'交易':>5}")
    print(f"  {'─'*33}")
    print(f"  {'全天网格':<18} {p_all:+7.2f}%  {t_all:>4}")
    print(f"  {'只在ranging':<18} {p_rng:+7.2f}%  {t_rng:>4}")
    print(f"  {'只在trending':<18} {p_trd:+7.2f}%  {t_trd:>4}")
    
    if p_rng > p_all:
        print(f"  ✅ ranging过滤有效：比全天多赚{p_rng-p_all:+.2f}%")
    elif p_rng < p_all:
        print(f"  ➖ ranging过滤减益{p_rng-p_all:+.2f}%（全天网格本身就赚钱时过滤反而少赚）")
    else:
        print(f"  ≈ 无影响")
    
    if p_trd > p_all:
        print(f"  ✅ trending过滤有效")

print(f"\n{'='*55}")
print("  结论：行情检测值不值得加？")
print(f"  {'='*55}")
