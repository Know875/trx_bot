"""
回测报告生成 — 参数排名、分组统计、相关性分析
"""
import numpy as np
import pandas as pd


def print_grid_results(name: str, results: list, widths: list,
                       rsi_vals: set, top_n: int = 10):
    """打印网格参数扫描结果。"""
    if not results:
        print(f"  ⚠️ {name}: 无结果")
        return results[0]["symbol"] if results else None

    rr = results[:top_n]
    print(f"\n  Top{top_n} 参数组合（按PnL%）：")
    print(f"  {'宽度':>6} {'档位':>4} {'RSI上限':>7} {'行情':>6} {'PnL%':>8} {'交易':>6} {'胜率':>6}")
    print(f"  {'-'*51}")
    for r in rr:
        rsi_str = f"r{r['rsi_limit']}" if r['rsi_limit'] != 999 else "不限"
        regime_str = r.get("regime_filter", "all")
        print(f"  {r['grid_width']:.0%}  {r['grid_levels']:>4}  {rsi_str:>7}  {regime_str:>6}  "
              f"{r['pnl_pct']:+7.2f}%  {r['total_trades']:>5}  {r['win_rate']:5.1f}%")

    # 各宽度均值
    print(f"\n  各宽度均值：")
    for gw in widths:
        sub = [r["pnl_pct"] for r in results if r["grid_width"] == gw]
        if sub:
            print(f"    ±{gw:.0%}: 均值{np.mean(sub):+.2f}%  最高{max(sub):+.2f}%  最低{min(sub):+.2f}%")

    # RSI 影响
    print(f"\n  RSI限制影响：")
    for rl in sorted(rsi_vals, key=lambda x: 999 if x is None else x):
        sub = [r["pnl_pct"] for r in results if r["rsi_limit"] == (rl or 999)]
        if sub:
            lbl = "不限" if rl is None else f"<{rl}"
            print(f"    RSI{lbl}: 均值{np.mean(sub):+.2f}%  最高{max(sub):+.2f}%  样本{len(sub)}")
    return True


def print_regime_test(df, regimes, name: str, results: dict):
    """打印行情检测准确率验证结果。"""
    from collections import Counter
    cnt = Counter(r for r in regimes if r != "warmup")
    total = sum(cnt.values())
    print(f"\n{'='*55}")
    print(f"  {name} | 行情分布验证")
    if total > 0:
        trending_up   = cnt.get("trending_up", 0)
        trending_down = cnt.get("trending_down", 0)
        ranging       = cnt.get("ranging", 0)
        wait          = cnt.get("wait", 0)
        print(f"  ranging={ranging/total*100:.0f}%  trending_up={trending_up/total*100:.0f}%  "
              f"trending_down={trending_down/total*100:.0f}%  wait={wait/total*100:.0f}%")

    print(f"  {'策略':<18} {'PnL%':>8} {'交易':>5}")
    print(f"  {'─'*33}")
    for label, (p, t) in results.items():
        print(f"  {label:<18} {p:+7.2f}%  {t:>4}")

    if len(results) >= 2:
        p_all, _ = results.get("全天网格", (0, 0))
        p_rng, _ = results.get("只在ranging", (0, 0))
        p_trd, _ = results.get("只在trending", (0, 0))
        if p_rng > p_all:
            print(f"  ✅ ranging过滤有效：比全天多赚{p_rng - p_all:+.2f}%")
        if p_trd > p_all:
            print(f"  ✅ trending过滤有效")


def print_trend_results(name: str, result: dict, buyhold_pct: float, days: int = 180):
    """打印趋势策略回测结果。"""
    print(f"\n{'='*55}")
    print(f"  {name} 趋势策略回测")
    print(f"  {'─'*27}")
    print(f"  基准: 买入持有 {buyhold_pct:+.2f}%")
    print(f"  趋势策略:")
    print(f"    总PnL: {result['pnl_pct']:+.2f}% (${result['pnl']:+.2f})")
    print(f"    年化:   {result['pnl_pct'] / days * 365:+.1f}%")
    print(f"    交易数: {result['trades']}笔  胜率: {result['win_rate']}%")
    print(f"    均盈:  ${result['avg_win']:+.2f}  均亏: ${result['avg_loss']:+.2f}")
    print(f"    出场分布: {result['reasons']}")


def correlation_matrix(dfs: dict) -> pd.DataFrame:
    """计算各币种日收益率相关性矩阵。"""
    returns = {}
    for name, df in dfs.items():
        if df is None or len(df) < 200:
            continue
        d = df.set_index("dt").resample("D").agg({"close": ["first", "last"]}).dropna()
        d.columns = ["open_d", "close_d"]
        returns[name] = (d["close_d"] - d["open_d"]) / d["open_d"]

    if len(returns) < 2:
        return pd.DataFrame()

    corr = pd.DataFrame(returns).dropna().corr()
    print(f"\n{'='*55}")
    print(f"日收益率相关性矩阵：")
    print(corr.round(3).to_string())
    return corr


def print_summary(best: dict, label: str = "最优参数总结"):
    """打印汇总。"""
    print(f"\n{'='*55}")
    print(f"{label}")
    print(f"{'='*55}")
    for name, b in sorted(best.items(), key=lambda x: x[1].get("pnl_pct", 0), reverse=True):
        gw = b.get("grid_width", 0)
        gl = b.get("grid_levels", 0)
        rl = b.get("rsi_limit", 999)
        rsi_s = f"RSI<{rl}" if rl != 999 else "RSI不限"
        pnl = b.get("pnl_pct", 0)
        print(f"  {name:12s}: ±{gw:.0%}  {gl}档  {rsi_s:>8}  PnL{pnl:+.2f}%  "
              f"{b.get('total_trades', 0)}笔  胜率{b.get('win_rate', 0):.0f}%")
