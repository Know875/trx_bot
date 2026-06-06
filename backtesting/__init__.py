"""
统一回测入口 — 替代 backtest.py / backtest2.py / backtest3_regime.py / backtest4_trend.py

用法:
    python -m backtesting grid      # 网格参数扫描 (OKX数据)
    python -m backtesting grid -c   # 用本地缓存数据
    python -m backtesting regime    # 行情检测准确率验证
    python -m backtesting trend     # 趋势策略回测
    python -m backtesting all       # 全部运行
"""
import argparse
import sys
import traceback
from collections import defaultdict
from pathlib import Path

import numpy as np

# 添加父目录到 sys.path（直接运行时需要）
_parent = Path(__file__).parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

from backtesting.engine import (
    SYMBOLS, download_okx_candles, load_from_cache, calc_indicators,
)
from backtesting.grid import simulate_grid, sweep_grid_params
from backtesting.trend import simulate_trend
from backtesting.report import (
    print_grid_results, print_regime_test, print_trend_results,
    correlation_matrix, print_summary,
)

GRID_WIDTHS  = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
GRID_LEVELS  = [3, 5, 7, 9]
RSI_LIMITS   = [None, 70, 75, 80]
BEST_PARAMS   = {"TRX": (0.01, 9), "ETH": (0.05, 9), "SOL": (0.02, 3)}


def run_grid_sweep(use_cache: bool = False):
    """网格参数扫描（原 backtest.py + backtest2.py）"""
    # 只用现货币种
    spot_coins = {k: v for k, v in SYMBOLS.items() if "SWAP" not in k}
    all_best = {}
    all_data = {}

    for name, symbol in spot_coins.items():
        try:
            if use_cache:
                df = load_from_cache(name)
                days = 180
            else:
                df = download_okx_candles(symbol)
                days = 90
            if df is None or len(df) < 100:
                print(f"  ⚠️ {name}: 无数据")
                continue

            d1 = df["dt"].iloc[0].strftime("%m-%d")
            d2 = df["dt"].iloc[-1].strftime("%m-%d")
            print(f"\n{'='*60}")
            print(f"回测 {name} | {symbol} | {d1} → {d2} | {len(df)}根K线")
            print(f"{'='*60}")

            ind = calc_indicators(df)
            results = sweep_grid_params(ind, name, GRID_WIDTHS, GRID_LEVELS, RSI_LIMITS)
            print_grid_results(name, results, GRID_WIDTHS, {*RSI_LIMITS, None})
            if results:
                all_best[name] = results[0]
            all_data[name] = df
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            traceback.print_exc()

    try:
        correlation_matrix(all_data)
    except Exception as e:
        print(f"  相关性分析异常: {e}")

    print_summary(all_best, f"最优参数总结 ({'缓存' if use_cache else 'OKX'}数据)")


def run_regime_test():
    """行情检测准确率验证（原 backtest3_regime.py）"""
    print("╔══════════════════════════════════════════════╗")
    print("║  行情检测准确率验证 (1h K线, 180天)           ║")
    print("╚══════════════════════════════════════════════╝")

    for name in ["TRX", "ETH", "SOL"]:
        df = load_from_cache(name)
        if df is None:
            continue

        # 降采样到4h减少计算
        df = df.set_index("dt").resample("4h").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "vol": "sum"
        }).dropna().reset_index()

        ind = calc_indicators(df)

        w, n = BEST_PARAMS[name]
        print(f"\n{'='*55}")
        print(f"  {name} | 网格±{w:.0%} {n}档")

        results = {}
        for label, rf in [("全天网格", None), ("只在ranging", "ranging"), ("只在trending", "trending")]:
            r = simulate_grid(ind, w, n, regime_filter=rf)
            results[label] = (r["pnl_pct"], r["total_trades"])

        print_regime_test(df, ind.regimes, name, results)


def run_trend_test():
    """趋势策略回测（原 backtest4_trend.py）"""
    print("╔══════════════════════════════════════════════╗")
    print("║  趋势策略回测 (1h K线, 180天)                 ║")
    print("╚══════════════════════════════════════════════╝")

    for name in ["ETH", "SOL"]:
        df = load_from_cache(name)
        if df is None or len(df) < 200:
            continue

        d1 = df["dt"].iloc[0].strftime("%m-%d")
        d2 = df["dt"].iloc[-1].strftime("%m-%d")
        print(f"\n  {name} | 1h K线 | {d1} → {d2} | {len(df)}根")

        ind = calc_indicators(df)
        result = simulate_trend(ind)

        buyhold = (ind.close[-1] - ind.close[59]) / ind.close[59] * 100
        print_trend_results(name, result, buyhold)


def main():
    parser = argparse.ArgumentParser(description="统一回测工具")
    parser.add_argument("mode", nargs="?", default="grid",
                        choices=["grid", "regime", "trend", "all"])
    parser.add_argument("-c", "--cache", action="store_true",
                        help="使用本地缓存数据（Binance 1h）而非 OKX 实时下载")
    args = parser.parse_args()

    if args.mode == "grid":
        run_grid_sweep(use_cache=args.cache)
    elif args.mode == "regime":
        run_regime_test()
    elif args.mode == "trend":
        run_trend_test()
    elif args.mode == "all":
        run_grid_sweep(use_cache=args.cache)
        print("\n\n")
        run_regime_test()
        print("\n\n")
        run_trend_test()


if __name__ == "__main__":
    main()
