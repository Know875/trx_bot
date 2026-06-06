#!/usr/bin/env python3
"""历史回测 —— 网格参数优化

回答三个问题：
1. 每个币种最优网格宽度/档位是多少？（按历史PnL）
2. RSI限制对收益的影响？（有RSI上限 vs 无限制）
3. 各币种相关性？（资金冗余分析）
"""

import json, time, sys, os
from datetime import datetime, timedelta
from collections import defaultdict
import pandas as pd
import numpy as np

from okx_client import OKXClient
from market_analyzer import parse_candles

# ── 回测参数 ──────────────────────────────────────────
LOOKBACK_DAYS = 90          # 回看90天
BAR = "15m"                 # 15分钟K线
BARS_PER_BATCH = 300        # OKX单次最大
SYMBOLS = {
    "TRX":      "TRX-USDT",
    "ETH":      "ETH-USDT",
    "SOL":      "SOL-USDT",
    "TRX_SWAP": "TRX-USDT-SWAP",
    "ETH_SWAP": "ETH-USDT-SWAP",
    "SOL_SWAP": "SOL-USDT-SWAP",
}

# 待测试参数组合
GRID_WIDTHS  = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]   # 网格宽度（价格比例）
GRID_LEVELS  = [3, 5, 7, 9]                              # 网格档位数
RSI_LIMITS   = [None, 70, 75, 80]                        # RSI上限(None=不限制)

DATA_DIR = os.path.join(os.path.dirname(__file__), "backtest_data")
os.makedirs(DATA_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════
# 1. 下载历史K线
# ═══════════════════════════════════════════════════════

def download_history(symbol, days=LOOKBACK_DAYS):
    """分批下载历史K线，直接调OKX REST API"""
    path = os.path.join(DATA_DIR, f"{symbol.replace('-','_')}_{days}d.csv")
    if os.path.exists(path):
        df = pd.read_csv(path)
        if len(df) > 500:
            print(f"  {symbol}: ✓ 缓存 {len(df)} 根")
            df["dt"] = pd.to_datetime(df["dt"])
            return df

    print(f"  下载 {symbol} 历史K线...")
    import requests
    all_data = []
    after = ""
    batches = 0
    max_batches = (days * 24 * 4) // BARS_PER_BATCH + 5

    while batches < max_batches:
        url = f"https://www.okx.com/api/v5/market/candles"
        params = {"instId": symbol, "bar": BAR, "limit": str(BARS_PER_BATCH)}
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
            after = raw[-1][0]  # 最后一条K线的时间戳
            batches += 1
            time.sleep(0.25)
            if len(raw) < BARS_PER_BATCH:
                break
        except Exception as e:
            print(f"    异常: {e}")
            break

    if not all_data:
        print("    ⚠️ 无数据")
        return None

    # OKX返回：[ts, open, high, low, close, vol, volCcy, ...]
    rows = []
    for r in all_data:
        rows.append({
            "ts":    int(r[0]),
            "open":  float(r[1]),
            "high":  float(r[2]),
            "low":   float(r[3]),
            "close": float(r[4]),
            "vol":   float(r[5]),
        })

    df = pd.DataFrame(rows)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms")
    df.to_csv(path, index=False)
    print(f"    ✓ {len(df)} 根K线, {df['dt'].iloc[0]} → {df['dt'].iloc[-1]}")
    return df


# ═══════════════════════════════════════════════════════
# 2. 网格策略模拟
# ═══════════════════════════════════════════════════════

def simulate_grid(df, grid_width, grid_levels, rsi_limit=None, capital=1000):
    """
    简化版网格模拟：
    - 每根K线检查是否有挂单成交（穿透挂单价即视为成交）
    - 成交后在反方向补挂单
    - 不做趋势/死盘等行情判断，纯网格
    - 忽略手续费（费率因行情变化，回测无法精确模拟）
    """
    n = len(df)
    if n < 60:
        return {}

    # 用滚动窗口计算RSI（14周期）
    close = df["close"].values
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = np.convolve(gain, np.ones(14)/14, mode="same")
    avg_loss = np.convolve(loss, np.ones(14)/14, mode="same")
    avg_loss = np.where(avg_loss == 0, 1e-10, avg_loss)
    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)

    trades = []
    # 追踪当前挂单
    buy_orders  = []   # [(price, size_usdt)]
    sell_orders = []
    balance_usdt = capital
    balance_coin = 0.0

    warmup = 60  # 等指标稳定

    for i in range(warmup, n):
        row = df.iloc[i]
        high, low, price = row["high"], row["low"], row["close"]

        # RSI 过滤（仅限制做多）
        if rsi_limit is not None and rsi[i] > rsi_limit:
            # 不挂新买单，但已有卖单照常执行
            pass

        # 检查买单成交：价格低于挂单价
        new_buys = []
        for bp, sz in buy_orders:
            if low <= bp:
                # 成交
                amt = sz / bp  # USDT → 币
                if sz <= balance_usdt:
                    balance_usdt -= sz
                else:
                    amt = balance_usdt / bp
                    balance_usdt = 0
                balance_coin += amt
                trades.append({"side": "buy", "price": bp, "amt": amt, "usdt": amt * bp})
                # 在上方挂卖单
                sell_orders.append((bp * (1 + grid_width), amt))
            else:
                new_buys.append((bp, sz))
        buy_orders = new_buys

        # 检查卖单成交：价格高于挂单价
        new_sells = []
        for sp, sz in sell_orders:
            if high >= sp:
                if sz <= balance_coin:
                    balance_coin -= sz
                else:
                    sz = balance_coin
                    balance_coin = 0
                revenue = sz * sp
                balance_usdt += revenue
                trades.append({"side": "sell", "price": sp, "amt": sz, "usdt": revenue})
                # 在下方补买单
                if rsi_limit is None or rsi[i] <= rsi_limit:
                    buy_orders.append((sp * (1 - grid_width), revenue))
            else:
                new_sells.append((sp, sz))
        sell_orders = new_sells

        # 首次/重设网格
        if not buy_orders and not sell_orders and balance_coin < 1e-8:
            _init_grid(buy_orders, sell_orders, price, grid_width, grid_levels, balance_usdt, rsi[i], rsi_limit)
        elif not buy_orders and balance_coin > 0:
            # 只有币，补卖单
            _init_sells(sell_orders, price, grid_width, grid_levels, balance_coin)
        elif not sell_orders and balance_usdt > capital * 0.1:
            # 只有U，补买单
            _init_grid(buy_orders, sell_orders, price, grid_width, grid_levels, balance_usdt, rsi[i], rsi_limit)

    # 计算最终净值
    final = balance_usdt + balance_coin * df.iloc[-1]["close"]
    pnl = final - capital
    pnl_pct = pnl / capital * 100

    # 统计
    buy_trades  = [t for t in trades if t["side"] == "buy"]
    sell_trades = [t for t in trades if t["side"] == "sell"]
    wins = sum(1 for t in sell_trades if t["usdt"] > t.get("cost_usdt", 0))

    return {
        "total_trades": len(trades),
        "buy_trades": len(buy_trades),
        "sell_trades": len(sell_trades),
        "wins": wins,
        "win_rate": wins / len(sell_trades) * 100 if sell_trades else 0,
        "pnl": round(pnl, 4),
        "pnl_pct": round(pnl_pct, 4),
        "final_balance": round(balance_usdt, 2),
        "final_coin": round(balance_coin, 4),
    }


def _init_grid(buy_orders, sell_orders, price, width, levels, balance, rsi_val, rsi_limit):
    """初始化双边网格"""
    size_per = balance / levels
    if rsi_limit and rsi_val > rsi_limit:
        return  # 不建仓
    for i in range(levels):
        bp = price * (1 - width * (i + 1) / levels)
        buy_orders.append((bp, size_per))
        sp = price * (1 + width * (i + 1) / levels)
        sell_orders.append((sp, 0))  # 初始无币可卖，占位


def _init_sells(sell_orders, price, width, levels, balance):
    """只有币时初始化卖单"""
    size_per = balance / levels
    for i in range(levels):
        sp = price * (1 + width * (i + 1) / levels)
        sell_orders.append((sp, size_per))


# ═══════════════════════════════════════════════════════
# 3. 主回测循环
# ═══════════════════════════════════════════════════════

def run_backtest(name, symbol):
    """对一个币种跑所有参数组合"""
    df = download_history(symbol)
    if df is None:
        return None

    start = df["dt"].iloc[0].strftime("%m-%d")
    end   = df["dt"].iloc[-1].strftime("%m-%d")
    print(f"\n{'='*60}")
    print(f"回测 {name} | {symbol} | {start} → {end} | {len(df)} 根K线")
    print(f"{'='*60}")

    results = []
    total_combos = len(GRID_WIDTHS) * len(GRID_LEVELS) * len(RSI_LIMITS)

    for gw in GRID_WIDTHS:
        for gl in GRID_LEVELS:
            for rl in RSI_LIMITS:
                r = simulate_grid(df, gw, gl, rl)
                if r:
                    r["grid_width"]  = gw
                    r["grid_levels"]  = gl
                    r["rsi_limit"]   = rl or 999
                    r["symbol"]      = name
                    r["bars"]        = len(df)
                    results.append(r)

    return results, df


def analyze(results, name):
    """输出分析"""
    if not results:
        print(f"  ⚠️ {name}: 无结果")
        return

    rr = sorted(results, key=lambda x: x["pnl_pct"], reverse=True)

    print(f"\n  Top10 参数组合（按PnL%）：")
    print(f"  {'宽度':>6} {'档位':>4} {'RSI上限':>7} {'PnL%':>8} {'交易数':>6} {'胜率':>6}")
    print(f"  {'-'*45}")
    for r in rr[:10]:
        rsi_str = f"r{r['rsi_limit']}" if r['rsi_limit'] != 999 else "不限"
        print(f"  {r['grid_width']:.0%}  {r['grid_levels']:>4}  {rsi_str:>7}  {r['pnl_pct']:+7.2f}%  {r['total_trades']:>5}  {r['win_rate']:5.1f}%")

    # 分组统计：不同grid_width的平均PnL
    print(f"\n  各宽度均值：")
    for gw in GRID_WIDTHS:
        subset = [r for r in results if r["grid_width"] == gw]
        if subset:
            avg = np.mean([r["pnl_pct"] for r in subset])
            max_p = max([r["pnl_pct"] for r in subset])
            print(f"    ±{gw:.0%}: 均值{avg:+.2f}%  最高{max_p:+.2f}%")

    # RSI限制的影响
    print(f"\n  RSI限制影响：")
    no_limit = [r for r in results if r["rsi_limit"] == 999]
    with_limit = [r for r in results if r["rsi_limit"] != 999]
    if no_limit:
        avg_no = np.mean([r["pnl_pct"] for r in no_limit])
        print(f"    不限RSI: 均值{avg_no:+.2f}%")
    if with_limit:
        for rl_val in [70, 75, 80]:
            sub = [r for r in with_limit if r["rsi_limit"] == rl_val]
            if sub:
                avg_sub = np.mean([r["pnl_pct"] for r in sub])
                print(f"    RSI<{rl_val}: 均值{avg_sub:+.2f}%")

    return rr[0]  # 返回最佳组合


# ═══════════════════════════════════════════════════════
# 4. 相关性分析
# ═══════════════════════════════════════════════════════

def analyze_correlation(dataframes):
    """计算各币种日收益率相关性"""
    daily_returns = {}
    for name, df in dataframes.items():
        if df is None or len(df) < 200:
            continue
        df = df.copy()
        df["dt"] = pd.to_datetime(df["dt"])
        daily = df.resample("D", on="dt").agg({"close": ["first", "last"]}).dropna()
        daily["ret"] = (daily[("close", "last")] - daily[("close", "first")]) / daily[("close", "first")]
        daily_returns[name] = daily["ret"]

    if len(daily_returns) < 2:
        return

    df_ret = pd.DataFrame(daily_returns).dropna()
    corr = df_ret.corr()

    print(f"\n{'='*60}")
    print(f"日收益率相关性矩阵：")
    print(f"{'='*60}")
    print(corr.round(3).to_string())
    return corr


# ═══════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════

def main():
    all_best = {}
    all_data = {}

    for name, symbol in SYMBOLS.items():
        try:
            data = run_backtest(name, symbol)
            if data is None:
                continue
            results, df = data
            best = analyze(results, name)
            if best:
                all_best[name] = best
            all_data[name] = df
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            import traceback
            traceback.print_exc()

    # 相关性分析
    try:
        analyze_correlation(all_data)
    except Exception as e:
        print(f"  相关性分析异常: {e}")

    # ── 总结 ──
    print(f"\n{'='*60}")
    print(f"最优参数总结")
    print(f"{'='*60}")
    for name, best in sorted(all_best.items(), key=lambda x: x[1]["pnl_pct"], reverse=True):
        rsi_str = f"RSI<{best['rsi_limit']}" if best['rsi_limit'] != 999 else "不限"
        print(f"  {name:12s}: 宽±{best['grid_width']:.0%} 档{best['grid_levels']} {rsi_str}  PnL{best['pnl_pct']:+.2f}%  {best['total_trades']}笔")


if __name__ == "__main__":
    main()
