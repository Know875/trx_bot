#!/usr/bin/env python3
"""趋势策略回测 —— 验证 trending_up/down 期间做趋势是否赚钱"""
import json, os
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "backtest_data")
SYMBOLS_MAP = {"ETH": "ETHUSDT", "SOL": "SOLUSDT"}

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
    return df[df["ts"] >= cutoff].reset_index(drop=True)


def precompute(df):
    """向量化计算EMA/ATR/RSI/ADX + 行情判定"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(close)

    ema20 = pd.Series(close).ewm(span=20, adjust=False).mean().values
    ema60 = pd.Series(close).ewm(span=60, adjust=False).mean().values

    # BB
    roll_std = pd.Series(close).rolling(20).std().values
    roll_ma  = pd.Series(close).rolling(20).mean().values
    bb_width = (roll_std * 4) / roll_ma

    # ATR
    tr = np.maximum(high - low, np.abs(high - np.roll(close, 1)))
    tr = np.maximum(tr, np.abs(low - np.roll(close, 1)))
    tr[0] = high[0] - low[0]
    atr = pd.Series(tr).rolling(14).mean().values
    atr_pct = atr / close

    # RSI
    delta = np.diff(close, prepend=close[0])
    gain = np.maximum(delta, 0)
    loss = np.maximum(-delta, 0)
    avg_gain = pd.Series(gain).ewm(span=14, adjust=False).mean().values
    avg_loss = pd.Series(loss).ewm(span=14, adjust=False).mean().values
    avg_loss = np.where(avg_loss < 1e-10, 1e-10, avg_loss)
    rsi = 100 - 100 / (1 + avg_gain / avg_loss)

    # ADX
    adx = np.full(n, 0.0)
    tr14 = pd.Series(tr).ewm(span=14, adjust=False).mean().values
    up_move = np.maximum(high - np.roll(high, 1), 0)
    down_move = np.maximum(np.roll(low, 1) - low, 0)
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    plus_di = pd.Series(plus_dm).ewm(span=14, adjust=False).mean().values / np.maximum(tr14, 1e-10) * 100
    minus_di = pd.Series(minus_dm).ewm(span=14, adjust=False).mean().values / np.maximum(tr14, 1e-10) * 100
    dx = np.abs(plus_di - minus_di) / np.maximum(plus_di + minus_di, 1e-10) * 100
    adx = pd.Series(dx).ewm(span=14, adjust=False).mean().values

    # Regime
    regimes = []
    for i in range(n):
        if i < 60:
            regimes.append("warmup"); continue
        ed = abs(ema20[i] - ema60[i]) / ema60[i]; bw = bb_width[i]; ap = atr_pct[i]
        dead = bw < 0.012 and ap < 0.005 and ed < 0.005
        rng = bw < 0.030 and ed < 0.015 and ap < 0.015
        trd = ed > 0.007 and bw > 0.012 and ap > 0.002 and adx[i] > 25
        if dead or rng: regimes.append("ranging")
        elif trd: regimes.append("trending_up" if ema20[i] > ema60[i] else "trending_down")
        else: regimes.append("wait")

    return regimes, rsi, atr_pct, adx, close, high, low


# ═══════════════════════════════════════════════════════

TP_PCT   = 0.020   # 2% take profit
SL_PCT   = 0.010   # 1% stop loss
TRAIL_PCT = 0.008  # 0.8% trailing
RSI_OB   = 70
RSI_OS   = 30
COOLDOWN = 5
POSITION_PCT = 0.45  # 45% of capital per trade

def simulate_trend(df, regimes, rsi, atr_pct, adx, close_arr, high_arr, low_arr, capital=1000):
    """趋势策略模拟：只在trending期间开仓，跟现有逻辑对齐"""
    n = len(df)
    usdt = capital
    pos = 0.0       # 持仓USDT价值
    pos_side = None  # "long" or "short"
    entry_price = 0.0
    tp_price = 0.0
    stop_price = 0.0
    best_price = 0.0
    cooldown = 0
    trades = []
    regime_cycle_active = False  # 当前regime周期内是否允许入场

    for i in range(60, n):
        p_close = close_arr[i]
        p_high = high_arr[i]
        p_low = low_arr[i]
        regime = regimes[i]

        if cooldown > 0:
            cooldown -= 1

        # ── 只在trending期间交易 ──
        if regime not in ("trending_up", "trending_down"):
            regime_cycle_active = False
            # 有持仓就平掉（行情切换时平仓）
            if pos > 0:
                pnl = _close(pos, pos_side, entry_price, p_close, usdt)
                trades.append({"side": pos_side, "entry": entry_price, "exit": p_close, "pnl": pnl})
                pos = 0; pos_side = None
            continue

        # 新的trending周期开始
        if not regime_cycle_active:
            regime_cycle_active = True

        # ── 无持仓：尝试开仓 ──
        if pos == 0 and cooldown == 0:
            r = rsi[i]
            if regime == "trending_up" and r < RSI_OB:
                pos_size = usdt * POSITION_PCT
                pos = pos_size
                pos_side = "long"
                entry_price = p_close
                best_price = p_close
                tp_price = p_close * (1 + TP_PCT)
                stop_price = p_close * (1 - SL_PCT)
                continue
            elif regime == "trending_down" and r > RSI_OS:
                pos_size = usdt * POSITION_PCT
                pos = pos_size
                pos_side = "short"
                entry_price = p_close
                best_price = p_close
                tp_price = p_close * (1 - TP_PCT)
                stop_price = p_close * (1 + SL_PCT)
                continue

        if pos == 0:
            continue

        # ── 有持仓：检查出场条件 ──
        # 更新最优价
        if pos_side == "long":
            if p_high > best_price:
                best_price = p_high
            # 移动止损
            dd = (best_price - p_low) / best_price if best_price > 0 else 0
            if dd >= TRAIL_PCT:
                exit_p = best_price * (1 - TRAIL_PCT)
                pnl = _close(pos, pos_side, entry_price, exit_p, usdt)
                trades.append({"side": pos_side, "entry": entry_price, "exit": exit_p, "pnl": pnl, "reason": "trailing"})
                usdt += pnl
                pos = 0; pos_side = None; cooldown = COOLDOWN
                continue
            # 止盈
            if p_high >= tp_price:
                pnl = _close(pos, pos_side, entry_price, tp_price, usdt)
                trades.append({"side": pos_side, "entry": entry_price, "exit": tp_price, "pnl": pnl, "reason": "tp"})
                usdt += pnl
                pos = 0; pos_side = None; cooldown = COOLDOWN
                continue
            # 止损
            if p_low <= stop_price:
                pnl = _close(pos, pos_side, entry_price, stop_price, usdt)
                trades.append({"side": pos_side, "entry": entry_price, "exit": stop_price, "pnl": pnl, "reason": "sl"})
                usdt += pnl
                pos = 0; pos_side = None; cooldown = COOLDOWN
                continue
        else:  # short
            if p_low < best_price:
                best_price = p_low
            dd = (p_high - best_price) / best_price if best_price > 0 else 0
            if dd >= TRAIL_PCT:
                exit_p = best_price * (1 + TRAIL_PCT)
                pnl = _close(pos, pos_side, entry_price, exit_p, usdt)
                trades.append({"side": pos_side, "entry": entry_price, "exit": exit_p, "pnl": pnl, "reason": "trailing"})
                usdt += pnl
                pos = 0; pos_side = None; cooldown = COOLDOWN
                continue
            if p_low <= tp_price:
                pnl = _close(pos, pos_side, entry_price, tp_price, usdt)
                trades.append({"side": pos_side, "entry": entry_price, "exit": tp_price, "pnl": pnl, "reason": "tp"})
                usdt += pnl
                pos = 0; pos_side = None; cooldown = COOLDOWN
                continue
            if p_high >= stop_price:
                pnl = _close(pos, pos_side, entry_price, stop_price, usdt)
                trades.append({"side": pos_side, "entry": entry_price, "exit": stop_price, "pnl": pnl, "reason": "sl"})
                usdt += pnl
                pos = 0; pos_side = None; cooldown = COOLDOWN
                continue

    # 强制平仓
    if pos > 0:
        pnl = _close(pos, pos_side, entry_price, close_arr[-1], usdt)
        trades.append({"side": pos_side, "entry": entry_price, "exit": close_arr[-1], "pnl": pnl})
        usdt += pnl

    pnl_total = usdt - capital
    pnl_pct = pnl_total / capital * 100

    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    avg_win = np.mean([t["pnl"] for t in trades if t["pnl"] > 0]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in trades if t["pnl"] <= 0]) if losses else 0

    reasons = {}
    for t in trades:
        r = t.get("reason", "forced")
        reasons[r] = reasons.get(r, 0) + 1

    return {
        "pnl_pct": round(pnl_pct, 2),
        "pnl": round(pnl_total, 2),
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(trades) * 100, 1) if trades else 0,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "reasons": reasons,
    }


def _close(pos, side, entry, exit, usdt):
    """计算平仓盈亏（USDT）"""
    if side == "long":
        return pos * (exit - entry) / entry
    else:
        return pos * (entry - exit) / entry


# ═══════════════════════════════════════════════════════

print("╔════════════════════════════════════════════════════╗")
print("║  趋势策略回测 (1h K线, 180天)                      ║")
print("║  参数: TP+2% SL-1% 移动-0.8% RSI<70/>30           ║")
print("╚════════════════════════════════════════════════════╝")

for name in ["ETH", "SOL"]:
    print(f"\n{'='*55}")
    print(f"  {name}")
    df = load_df(name)
    regimes, rsi, atr, adx, close, high, low = precompute(df)

    # 统计
    from collections import Counter
    cnt = Counter(r for r in regimes if r != "warmup")
    total = sum(cnt.values())

    # 只在trending期间的趋势策略
    result = simulate_trend(df, regimes, rsi, atr, adx, close, high, low)

    # 买入持有基准
    buyhold = (close[-1] - close[59]) / close[59] * 100

    print(f"  行情分布: ranging={cnt.get('ranging',0)/total*100:.0f}% "
          f"trending_up={cnt.get('trending_up',0)/total*100:.0f}% "
          f"trending_down={cnt.get('trending_down',0)/total*100:.0f}% "
          f"wait={cnt.get('wait',0)/total*100:.0f}%")
    print(f"  基准: 买入持有 {buyhold:+.2f}%")
    print(f"")
    print(f"  趋势策略:")
    print(f"    总PnL: {result['pnl_pct']:+.2f}% (${result['pnl']:+.2f})")
    print(f"    年化:   {result['pnl_pct']/180*365:+.1f}%")
    print(f"    交易数: {result['trades']}笔  胜率: {result['win_rate']}%")
    print(f"    均盈:  ${result['avg_win']:+.2f}  均亏: ${result['avg_loss']:+.2f}")
    print(f"    出场分布: {result['reasons']}")
