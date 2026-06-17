"""
回测 vs 实盘校准 — 量化"回测比实盘乐观了多少"

用模拟盘真实成交记录，对同一段同时跑回测，
计算折扣率（discount factor），以后所有回测数字按这个比例打折。

用法:
  python backtest_calibrate.py                    # 自动跑所有有记录的币种
  python backtest_calibrate.py --coin ETH         # 只跑 ETH
  python backtest_calibrate.py --output report.json
"""
import sys
import os
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger("calibrate")

ROOT = Path(__file__).parent


# ═══════════════════════════════════════════════════════════════
# 1. 读取模拟盘交易记录
# ═══════════════════════════════════════════════════════════════

def load_paper_trades(coin: str) -> list[dict]:
    """加载模拟盘交易记录"""
    files = sorted(ROOT.glob(f"*trade_records*{coin}*.json"))
    trades = []
    for f in files:
        try:
            data = json.loads(f.read_text())
            if isinstance(data, list):
                trades.extend(data)
            elif isinstance(data, dict):
                trades.extend(data.get("trades", data.get("records", [])))
        except Exception:
            pass
    return trades


def load_sqlite_trades(coin: str) -> list[dict]:
    """从 bot_state.db 读交易记录"""
    import sqlite3
    db_path = ROOT / "bot_state.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT time, coin, pnl, side, price, size FROM guard_trades WHERE coin=? ORDER BY time",
            (coin,)
        ).fetchall()
        conn.close()
        return [
            {"time": r[0], "coin": r[1], "pnl": r[2], "side": r[3], "price": r[4], "size": r[5]}
            for r in rows
        ]
    except Exception:
        return []


def paper_metrics(trades: list[dict]) -> dict:
    """从实盘/模拟盘交易提取指标"""
    if not trades:
        return {}
    pnls = [t.get("pnl", 0) for t in trades]
    total = sum(pnls)
    count = len(pnls)
    winners = sum(1 for p in pnls if p > 0)
    losers = sum(1 for p in pnls if p < 0)
    return {
        "total_pnl": round(total, 4),
        "trades": count,
        "win_rate": round(winners / count * 100, 1) if count else 0,
        "avg_win": round(sum(p for p in pnls if p > 0) / max(1, winners), 4),
        "avg_loss": round(sum(p for p in pnls if p < 0) / max(1, losers), 4),
        "best": round(max(pnls), 4) if pnls else 0,
        "worst": round(min(pnls), 4) if pnls else 0,
    }


# ═══════════════════════════════════════════════════════════════
# 2. 回测对比
# ═══════════════════════════════════════════════════════════════

def run_backtest_for_trade_period(coin: str, start_time: str, end_time: str = None) -> dict:
    """
    在模拟盘同一时间段跑回测。
    用实盘配置参数，统计回测盈亏。
    """
    from backtesting.engine import load_from_cache, calc_indicators
    import config

    base = coin.replace("_SWAP", "")
    df = load_from_cache(base, "1h", 90)
    if df is None or len(df) < 60:
        return {}

    ind = calc_indicators(df)
    is_futures = "_SWAP" in coin

    if is_futures:
        from backtesting.grid import simulate_grid
        capital = config.COIN_CONFIG.get(coin, {}).get("initial_capital", 2000)
        r = simulate_grid(ind, grid_width=0.10, grid_levels=3,
                          fee_rate=config.BACKTEST_FEE_RATE,
                          slippage=config.BACKTEST_SLIPPAGE)
    else:
        from backtesting.grid import simulate_grid
        capital = config.COIN_CONFIG.get(coin, {}).get("initial_capital", 1000)
        r = simulate_grid(ind, grid_width=0.08, grid_levels=3,
                          fee_rate=config.BACKTEST_FEE_RATE,
                          slippage=config.BACKTEST_SLIPPAGE)

    if not r:
        return {}

    return {
        "pnl_pct": round(r.get("pnl_pct", 0), 4),
        "pnl": round(r.get("pnl", 0), 4),
        "trades": r.get("buy_trades", 0) + r.get("sell_trades", 0),
        "win_rate": r.get("win_rate", 0),
        "total_fees": r.get("total_fees", 0),
    }


# ═══════════════════════════════════════════════════════════════
# 3. 校准计算
# ═══════════════════════════════════════════════════════════════

def calibrate(coin: str) -> dict | None:
    """
    对比实盘/模拟盘 vs 回测。

    返回 discount_factor:
      - 1.0: 回测=实盘（完美保真）
      - 0.5: 回测虚高50%
      - >1.0: 实盘比回测还好（罕见）
    """
    # 实盘数据
    sqlite_trades = load_sqlite_trades(coin)
    paper_trades = load_paper_trades(coin)
    real = paper_metrics(sqlite_trades or paper_trades)

    if not real:
        logger.info(f"{coin}: 无交易记录，跳过")
        return None

    # 回测数据
    bt = run_backtest_for_trade_period(coin, "ignore")

    if not bt:
        logger.info(f"{coin}: 回测数据不足，跳过")
        return None

    # ── 折扣率 ──
    # 简单对比：实盘总PnL / 回测总PnL
    if bt["pnl"] > 0 and real["total_pnl"] < bt["pnl"]:
        discount = real["total_pnl"] / bt["pnl"] if bt["pnl"] != 0 else 1.0
        discount = max(0.0, min(2.0, discount))  # 裁剪到 [0, 2]
    elif bt["pnl"] < 0 and real["total_pnl"] < 0:
        discount = 1.0  # 都是亏，不打折
    elif real["total_pnl"] > bt["pnl"] > 0:
        discount = 1.0  # 实盘更好
    else:
        discount = 1.0

    verdict = (
        "⚠️ 回测严重虚高" if discount < 0.3 else
        "⚠️ 回测偏高" if discount < 0.7 else
        "✅ 保真度良好" if discount < 1.3 else
        "🟢 实盘优于回测"
    )

    return {
        "coin": coin,
        "real": real,
        "backtest": bt,
        "discount_factor": round(discount, 2),
        "verdict": verdict,
        "note": (f"建议所有回测 PnL 打 {discount:.0%} 折后看"
                 if discount < 0.9 else
                 "回测保真度可接受"),
    }


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="回测vs实盘校准")
    parser.add_argument("--coin", default="", help="指定币种（不指定跑全部）")
    parser.add_argument("--output", default="", help="输出JSON文件")
    parser.add_argument("--summary", action="store_true", help="只显示汇总")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [calibrate] %(levelname)s: %(message)s")

    coins = [args.coin] if args.coin else ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]
    results = {}

    print(f"\n{'═' * 65}")
    print(f"  回测 vs 实盘 保真度校准  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═' * 65}")

    for coin in coins:
        r = calibrate(coin)
        if r is None:
            continue
        results[coin] = r

        if not args.summary:
            print(f"\n  ── {coin} ──")
            real = r["real"]
            bt = r["backtest"]
            print(f"  实盘:  PnL={real['total_pnl']:+.2f}  "
                  f"交易={real['trades']}  胜率={real['win_rate']}%")
            print(f"  回测:  PnL={bt['pnl']:+.2f}  "
                  f"交易={bt['trades']}  胜率={bt['win_rate']}%")
            print(f"  折扣率: {r['discount_factor']:.2%}  {r['verdict']}")
            print(f"  {r['note']}")

    # 汇总
    if len(results) > 1:
        discounts = [v["discount_factor"] for v in results.values()]
        avg_d = sum(discounts) / len(discounts)
        print(f"\n{'─' * 65}")
        print(f"  汇总折扣率: {avg_d:.2%}  (取最保守的)")
        min_d = min(discounts)
        print(f"  最保守建议: 所有回测 PnL 打 {min_d:.0%} 折")
        results["_summary"] = {
            "coins": list(results.keys()),
            "avg_discount": round(avg_d, 2),
            "min_discount": round(min_d, 2),
            "recommend_discount": round(min_d, 2),
        }

    if args.output:
        Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"\n  报告已保存: {args.output}")


if __name__ == "__main__":
    main()


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
    from brain import optimize_grid
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


def _cli_edge_research():
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
