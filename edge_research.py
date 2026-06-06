"""
边际研究 — 诚实地检验"这套策略到底有没有边际"，而不是假设有。

它不编造 alpha，只测量历史数据里是否存在**可利用的统计模式**：
  ① 收益自相关：负 → 均值回归（网格有理）；正 → 动量（趋势有理）；~0 → 随机游走（无边际）
  ② 时段效应：各 UTC 小时平均收益，验证"亚洲高峰"假设是否真成立
  ③ 网格扣成本后是否还赚：用含手续费+滑点的回测跑最优网格，对比"买入持有"
  ④ 趋势策略 vs 买入持有

结论会直说"没有显著边际"，因为那才是对真金白银负责。

用法: python edge_research.py [coin|all]
"""
import sys
import logging
from collections import defaultdict

import numpy as np

logger = logging.getLogger("edge_research")

COINS = ["TRX", "ETH", "SOL"]


def _load(coin):
    from backtesting.engine import load_from_cache
    return load_from_cache(coin, "1h", 180)


def _autocorr(returns: np.ndarray, lag: int = 1) -> float:
    r = returns[~np.isnan(returns)]
    if len(r) < lag + 30:
        return 0.0
    a, b = r[:-lag], r[lag:]
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def analyze(coin: str) -> dict:
    df = _load(coin)
    if df is None or len(df) < 200:
        print(f"\n【{coin}】无足够数据")
        return {}

    close = df["close"].values.astype(float)
    rets = np.diff(close) / close[:-1]      # 1h 简单收益
    n = len(rets)

    # ① 自相关（均值回归 vs 动量）
    ac1 = _autocorr(rets, 1)
    ac4 = _autocorr(rets, 4)
    sig = 1.96 / np.sqrt(n)                  # 5% 显著阈值
    if ac1 < -sig:
        regime_edge = f"均值回归（网格有理，lag1={ac1:+.3f}）"
    elif ac1 > sig:
        regime_edge = f"动量（趋势有理，lag1={ac1:+.3f}）"
    else:
        regime_edge = f"接近随机游走（无显著边际，lag1={ac1:+.3f}）"

    # ② 时段效应（UTC 小时平均收益）
    hours = df["dt"].dt.hour.values[1:]      # 对齐 rets
    by_hour = defaultdict(list)
    for h, r in zip(hours, rets):
        by_hour[int(h)].append(r)
    hour_mean = {h: float(np.mean(v)) for h, v in by_hour.items() if v}
    asia = [r for h, r in zip(hours, rets) if 7 <= h < 15]
    non_asia = [r for h, r in zip(hours, rets) if not (7 <= h < 15)]
    asia_mean = float(np.mean(asia)) if asia else 0.0
    non_asia_mean = float(np.mean(non_asia)) if non_asia else 0.0
    best_h = max(hour_mean, key=hour_mean.get) if hour_mean else None
    worst_h = min(hour_mean, key=hour_mean.get) if hour_mean else None

    # ③ 网格扣成本后 vs 买入持有
    from backtesting.engine import calc_indicators
    from optimize import optimize_grid
    ind = calc_indicators(df)
    opt = optimize_grid(coin, ind=ind)
    grid_net = opt["best_value"] if opt else None
    buy_hold = (close[-1] / close[0] - 1) * 100

    # ④ 趋势策略 vs 买入持有
    try:
        from backtesting.trend import simulate_trend
        trend_res = simulate_trend(ind)
        trend_net = trend_res.get("pnl_pct")
    except Exception:
        trend_net = None

    # ── 输出 ──
    print(f"\n{'='*60}\n【{coin}】180天 1h | 样本 {n}")
    print(f"  ① 收益结构: {regime_edge}  (lag4={ac4:+.3f}, 显著阈值±{sig:.3f})")
    print(f"  ② 时段: 亚洲(7-15UTC)均收益={asia_mean*100:+.4f}% vs 其它={non_asia_mean*100:+.4f}%"
          f"  → {'亚洲更优' if asia_mean>non_asia_mean else '亚洲并不更优'}")
    if best_h is not None:
        print(f"     最佳时段 {best_h:02d}h({hour_mean[best_h]*100:+.4f}%) / 最差 {worst_h:02d}h({hour_mean[worst_h]*100:+.4f}%)")
    print(f"  ③ 最优网格(扣成本)={grid_net:+.2f}% vs 买入持有={buy_hold:+.2f}%"
          f"  → 网格{'跑赢' if (grid_net or -9)>buy_hold else '跑输'}持有")
    if trend_net is not None:
        print(f"  ④ 趋势策略(扣成本)={trend_net:+.2f}% vs 买入持有={buy_hold:+.2f}%"
              f"  → 趋势{'跑赢' if trend_net>buy_hold else '跑输'}持有")

    # ── 诚实裁决 ──
    verdict = []
    if abs(ac1) < sig:
        verdict.append("收益接近随机，网格/趋势都难有结构性边际")
    if grid_net is not None and grid_net <= 0:
        verdict.append("最优网格扣成本后不赚钱")
    if grid_net is not None and grid_net <= buy_hold and (trend_net is None or trend_net <= buy_hold):
        verdict.append("两种策略都跑不赢买入持有")
    if not verdict:
        verdict.append("存在可利用的统计结构，值得继续验证")
    print(f"  📌 裁决: {'；'.join(verdict)}")

    return {
        "coin": coin, "n": n, "autocorr_lag1": ac1, "autocorr_lag4": ac4,
        "asia_mean": asia_mean, "non_asia_mean": non_asia_mean,
        "grid_net_pct": grid_net, "trend_net_pct": trend_net, "buy_hold_pct": buy_hold,
        "verdict": verdict,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    targets = sys.argv[1:] or COINS
    print("🔬 边际研究 — 诚实检验是否存在可利用模式")
    for c in targets:
        try:
            analyze(c)
        except Exception as e:
            print(f"\n【{c}】分析失败: {e}")
    print(f"\n{'='*60}")
    print("提醒: 历史有边际 ≠ 未来有边际；这只是排除『连历史都没边际』的情况。")
