#!/usr/bin/env python3
"""历史回测 v2 —— Binance 1h K线, 180天数据

回答：
1. 每个币种最优网格宽度/档位（按历史PnL）
2. RSI限制对收益的影响
3. 各币种相关性
"""

import json, os, sys
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "backtest_data")
INTERVAL = "1h"
DAYS = 180
SYMBOLS_MAP = {"TRX": "TRXUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}

# 测试参数
GRID_WIDTHS  = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
GRID_LEVELS  = [3, 5, 7, 9]
RSI_LIMITS   = [None, 70, 75, 80]

# ═══════════════════════════════════════════════════════

def load_binance_data(name):
    """加载Binance 1h数据并去重"""
    sym = SYMBOLS_MAP[name]
    path = os.path.join(DATA_DIR, f"{sym}_{INTERVAL}_{DAYS}d.json")
    if not os.path.exists(path):
        print(f"  ⚠️ {name}: 无数据文件")
        return None

    with open(path) as f:
        raw = json.load(f)

    # Binance kline: [open_time, open, high, low, close, volume, close_time, ...]
    rows = []
    seen = set()
    for r in raw:
        ts = r[0]
        if ts in seen:
            continue
        seen.add(ts)
        rows.append({
            "ts": ts,
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "vol": float(r[5]),
        })

    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms")

    # 只取最近180天
    cutoff = df["ts"].max() - DAYS * 86400000
    df = df[df["ts"] >= cutoff].reset_index(drop=True)

    return df


def calc_rsi(close, period=14):
    """计算RSI序列"""
    n = len(close)
    delta = np.diff(close, prepend=close[0])
    gain = np.maximum(delta, 0)
    loss = np.maximum(-delta, 0)
    avg_gain = np.convolve(gain, np.ones(period)/period, mode="same")
    avg_loss = np.convolve(loss, np.ones(period)/period, mode="same")
    avg_loss = np.where(avg_loss < 1e-10, 1e-10, avg_loss)
    return 100 - 100 / (1 + avg_gain / avg_loss)


def simulate_grid(df, grid_width, grid_levels, rsi_limit=None, capital=1000):
    """1h网格模拟"""
    n = len(df)
    if n < 60:
        return None

    close = df["close"].values
    high  = df["high"].values
    low   = df["low"].values
    rsi   = calc_rsi(close)

    trades = []
    buy_orders  = []
    sell_orders = []
    usdt = capital
    coin = 0.0
    warmup = 30

    for i in range(warmup, n):
        p_high, p_low, p_close = high[i], low[i], close[i]
        rsi_now = rsi[i]

        # ── 成交检查 ──
        new_buys = []
        for bp, sz in buy_orders:
            if p_low <= bp and sz <= usdt:
                amt = sz / bp
                usdt -= sz
                coin += amt
                sell_orders.append((bp * (1 + grid_width), amt))
                trades.append({"side": "buy", "price": bp, "amt": amt})
            else:
                new_buys.append((bp, sz))
        buy_orders = new_buys

        new_sells = []
        for sp, sz in sell_orders:
            if p_high >= sp and sz <= coin:
                coin -= sz
                usdt += sz * sp
                if rsi_limit is None or rsi_now <= rsi_limit:
                    buy_orders.append((sp * (1 - grid_width), sz * sp))
                trades.append({"side": "sell", "price": sp, "amt": sz})
            else:
                new_sells.append((sp, sz))
        sell_orders = new_sells

        # ── 重建网格 ──
        if not buy_orders and not sell_orders and coin < 1e-8:
            _build_grid(buy_orders, sell_orders, p_close, grid_width, grid_levels, usdt, rsi_now, rsi_limit)
        elif not buy_orders and usdt > capital * 0.1:
            if rsi_limit is None or rsi_now <= rsi_limit:
                _build_grid(buy_orders, sell_orders, p_close, grid_width, grid_levels, usdt, rsi_now, rsi_limit)

    final = usdt + coin * close[-1]
    pnl = final - capital
    pnl_pct = pnl / capital * 100

    sells = [t for t in trades if t["side"] == "sell"]
    buys  = [t for t in trades if t["side"] == "buy"]
    wins  = sum(1 for i, s in enumerate(sells) if i > 0 and s["price"] > sells[i-1]["price"])
    # better: count profitable round trips
    profits = 0
    for i in range(len(sells)):
        p_sell = sells[i]["price"]
        # find corresponding buy
        for j in range(len(buys) - 1, -1, -1):
            if buys[j]["amt"] >= sells[i]["amt"] * 0.99 and buys[j]["price"] < p_sell:
                profits += 1
                break

    return {
        "grid_width": grid_width,
        "grid_levels": grid_levels,
        "rsi_limit": rsi_limit or 999,
        "pnl": round(pnl, 4),
        "pnl_pct": round(pnl_pct, 4),
        "total_trades": len(trades),
        "buy_trades": len(buys),
        "sell_trades": len(sells),
        "win_rate": round(profits / len(sells) * 100, 1) if sells else 0,
        "bars": n,
    }


def _build_grid(buys, sells, price, width, levels, usdt, rsi, rsi_limit):
    if rsi_limit and rsi > rsi_limit:
        return
    per = usdt / levels
    for i in range(levels):
        buys.append((price * (1 - width * (i + 1) / levels), per))
        sells.append((price * (1 + width * (i + 1) / levels), 0))


# ═══════════════════════════════════════════════════════

def run_one(name):
    df = load_binance_data(name)
    if df is None or len(df) < 100:
        return None, None

    d1 = df["dt"].iloc[0].strftime("%m-%d")
    d2 = df["dt"].iloc[-1].strftime("%m-%d")
    print(f"\n{'='*60}")
    print(f"回测 {name} | 1h K线 | {d1} → {d2} | {len(df)}根")
    print(f"{'='*60}")

    results = []
    for gw in GRID_WIDTHS:
        for gl in GRID_LEVELS:
            for rl in RSI_LIMITS:
                r = simulate_grid(df, gw, gl, rl)
                if r:
                    r["symbol"] = name
                    results.append(r)

    if not results:
        return None, df

    rr = sorted(results, key=lambda x: x["pnl_pct"], reverse=True)

    print(f"  Top10:")
    print(f"  {'宽度':>6} {'档位':>4} {'RSI':>6} {'PnL%':>8} {'交易':>6} {'胜率':>6}")
    print(f"  {'-'*44}")
    for r in rr[:10]:
        rsi_s = f"<{r['rsi_limit']}" if r['rsi_limit'] != 999 else "不限"
        print(f"  {r['grid_width']:.0%}  {r['grid_levels']:>4}  {rsi_s:>6}  {r['pnl_pct']:+7.2f}%  {r['total_trades']:>5}  {r['win_rate']:5.1f}%")

    # 宽度均值
    print(f"\n  各宽度均值(所有RSI+档位平均)：")
    for gw in GRID_WIDTHS:
        sub = [r["pnl_pct"] for r in results if r["grid_width"] == gw]
        if sub:
            print(f"    ±{gw:.0%}: 均值{np.mean(sub):+.2f}%  最高{max(sub):+.2f}%  最低{min(sub):+.2f}%")

    # RSI影响
    print(f"  RSI限制均值：")
    for rl in [None, 70, 75, 80]:
        rl_key = rl or 999
        sub = [r["pnl_pct"] for r in results if r["rsi_limit"] == rl_key]
        if sub:
            sub_wr = [r["win_rate"] for r in results if r["rsi_limit"] == rl_key]
            rsi_s = f"<{rl}" if rl else "不限"
            avg_wr = sum(sub_wr) / len(sub_wr)
            print(f"    RSI{rsi_s}: 均值{np.mean(sub):+.2f}%  胜率均值{avg_wr:.1f}%")

    return rr[0], df


def correlation(dfs):
    """日收益率相关性"""
    print(f"\n{'='*60}")
    print(f"日收益率相关性")
    print(f"{'='*60}")

    returns = {}
    for name, df in dfs.items():
        if df is None or len(df) < 200:
            continue
        d = df.set_index("dt").resample("D").agg({"close": ["first", "last"]}).dropna()
        d.columns = ["open_d", "close_d"]
        returns[name] = (d["close_d"] - d["open_d"]) / d["open_d"]

    if len(returns) < 2:
        return
    corr_df = pd.DataFrame(returns).dropna().corr()
    print(corr_df.round(3).to_string())
    return corr_df


# ═══════════════════════════════════════════════════════

def main():
    best = {}
    dfs = {}

    for name in ["TRX", "ETH", "SOL"]:
        try:
            top, df = run_one(name)
            if top:
                best[name] = top
            if df is not None:
                dfs[name] = df
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            import traceback
            traceback.print_exc()

    correlation(dfs)

    # 总结
    print(f"\n{'='*60}")
    print(f"最优参数总结 (1h K线, {DAYS}天)")
    print(f"{'='*60}")
    for name, b in sorted(best.items(), key=lambda x: x[1]["pnl_pct"], reverse=True):
        rsi_s = f"RSI<{b['rsi_limit']}" if b['rsi_limit'] != 999 else "RSI不限"
        width_s = f"±{b['grid_width']:.0%}"
        pnl_annual = b["pnl_pct"] / DAYS * 365
        print(f"  {name:6s}: {width_s:>5} {b['grid_levels']}档 {rsi_s:>8}  {b['pnl_pct']:+.2f}%({pnl_annual:+.0f}%年)  {b['total_trades']}笔 胜率{b['win_rate']:.0f}%")


if __name__ == "__main__":
    main()
