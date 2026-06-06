"""
策略贡献度报表 — 看清楚谁在赚钱谁在亏钱

每24小时自动生成:
  - 策略贡献排名（PnL排序）
  - 费用效率排名（手续费/利润）
  - 风险效率排名（夏普/回撤）
  - 建议暂停/加权策略

用法:
  from strategy_report import StrategyReporter
  sr = StrategyReporter()
  sr.record(coin, strategy, pnl, fee, ...)  # 每笔交易
  sr.daily_summary()  # 每日报表
"""

import json, os, time, logging, threading
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
REPORT_FILE = str(ROOT / "strategy_report.json")

logger = logging.getLogger("strategy_report")

# 多个币种线程共用同一实例（main._reporter），串行化写盘
_REPORT_LOCK = threading.RLock()


def _synchronized(func):
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _REPORT_LOCK:
            return func(*args, **kwargs)
    return wrapper

# 各币种配比权重（按总本金百分比分配）
WEIGHTS = {
    "TRX":      0.10,
    "ETH":      0.30,
    "SOL":      0.20,
    "TRX_SWAP": 0.12,
    "ETH_SWAP": 0.20,
    "SOL_SWAP": 0.08,
}


class StrategyReporter:
    """追踪每个策略的贡献度"""

    def __init__(self):
        self._load()

    def _load(self):
        if os.path.exists(REPORT_FILE):
            try:
                with open(REPORT_FILE) as f:
                    self.state = json.load(f)
                return
            except Exception as e:
                logger.warning(f"strategy_report.json 损坏，备份后重建: {e}")
                try:
                    os.rename(REPORT_FILE, f"{REPORT_FILE}.corrupted.{int(time.time())}")
                except Exception:
                    pass
        self.state = self._new_day()

    def _save(self):
        # 原子写入，避免 web 并发读到半截 JSON
        tmp = f"{REPORT_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, REPORT_FILE)

    def _new_day(self) -> dict:
        return {}

    def _ensure_today(self) -> str:
        today = str(datetime.now(timezone.utc).date())
        if today not in self.state:
            self.state[today] = {}
        return today

    def _ensure_strategy(self, coin: str, strategy: str) -> str:
        today = self._ensure_today()
        key = f"{coin}:{strategy}"
        if key not in self.state[today]:
            self.state[today][key] = {
                "coin": coin,
                "strategy": strategy,
                "net_pnl": 0,
                "gross_pnl": 0,
                "total_fees": 0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "max_drawdown": 0,
                "peak_pnl": 0,
                "open_time": datetime.now(timezone.utc).isoformat(),
            }
        return key

    # ═══════════════════════════════════════════════════════

    @_synchronized
    def record(self, coin: str, strategy: str, pnl: float, fee: float = 0, gross_pnl: float = None):
        """记录一笔交易的贡献"""
        key = self._ensure_strategy(coin, strategy)
        s = self.state[self._ensure_today()][key]

        if gross_pnl is None:
            gross_pnl = pnl + fee

        s["net_pnl"] = round(s["net_pnl"] + pnl, 4)
        s["gross_pnl"] = round(s["gross_pnl"] + gross_pnl, 4)
        s["total_fees"] = round(s["total_fees"] + fee, 4)
        s["trades"] += 1

        if pnl > 0:
            s["wins"] += 1
        elif pnl < 0:
            s["losses"] += 1

        # 最大回撤追踪
        if s["net_pnl"] > s["peak_pnl"]:
            s["peak_pnl"] = s["net_pnl"]
        current_dd = (s["peak_pnl"] - s["net_pnl"]) / abs(s["peak_pnl"]) * 100 if s["peak_pnl"] > 0 else 0
        if current_dd > s["max_drawdown"]:
            s["max_drawdown"] = round(current_dd, 1)

        self._save()

    # ═══════════════════════════════════════════════════════
    # 报表
    # ═══════════════════════════════════════════════════════

    def daily_summary(self, date: str = None) -> dict:
        """生成当日策略贡献报表"""
        if date is None:
            date = str(datetime.now(timezone.utc).date())

        if date not in self.state:
            return {"date": date, "strategies": [], "summary": "今日无数据"}

        strategies = []
        for key, s in self.state[date].items():
            total = s["trades"]
            wins = s["wins"]
            wr = wins / total * 100 if total > 0 else 0
            fee_ratio = s["total_fees"] / s["gross_pnl"] * 100 if s["gross_pnl"] > 0 else 0

            # 加权贡献 = PnL × 币种权重
            weight = WEIGHTS.get(s["coin"], 0.1)
            weighted_pnl = s["net_pnl"] * weight

            # 综合评分 (0-100)
            score = self._calc_score(s, wr, fee_ratio, weight)

            strategies.append({
                "coin": s["coin"],
                "strategy": s["strategy"],
                "net_pnl": s["net_pnl"],
                "gross_pnl": s["gross_pnl"],
                "total_fees": s["total_fees"],
                "trades": total,
                "win_rate": round(wr, 1),
                "max_drawdown": s["max_drawdown"],
                "fee_ratio": round(fee_ratio, 1),
                "weight": weight,
                "weighted_pnl": round(weighted_pnl, 3),
                "score": score,
            })

        strategies.sort(key=lambda x: x["net_pnl"], reverse=True)

        # ── 生成建议 ──
        recommendations = self._make_recommendations(strategies, date)

        return {
            "date": date,
            "strategies": strategies,
            "recommendations": recommendations,
            "summary": self._format_summary(strategies, recommendations),
        }

    def _calc_score(self, s: dict, win_rate: float, fee_ratio: float, weight: float) -> int:
        """综合评分 0-100"""
        score = 50

        # PnL
        if s["net_pnl"] > 0:
            score += min(25, s["net_pnl"] * 5)
        else:
            score += max(-25, s["net_pnl"] * 5)

        # 胜率
        if win_rate > 60:
            score += 10
        elif win_rate < 40:
            score -= 10

        # 手续费
        if fee_ratio > 50:
            score -= 15
        elif fee_ratio > 30:
            score -= 8

        # 回撤
        if s["max_drawdown"] > 10:
            score -= 10
        elif s["max_drawdown"] > 5:
            score -= 5

        # 权重
        score += int(weight * 10)

        return max(0, min(100, score))

    def _make_recommendations(self, strategies: list, date: str) -> list:
        recs = []

        if not strategies:
            return recs

        # 最赚钱
        best = max(strategies, key=lambda x: x["net_pnl"])
        if best["net_pnl"] > 0:
            recs.append({
                "type": "最高收益",
                "item": f"{best['coin']}.{best['strategy']}",
                "detail": f"${best['net_pnl']:+.2f} (胜率 {best['win_rate']}%)",
                "action": "继续保持",
            })

        # 最亏
        worst = min(strategies, key=lambda x: x["net_pnl"])
        if worst["net_pnl"] < -0.5:
            recs.append({
                "type": "最大亏损",
                "item": f"{worst['coin']}.{worst['strategy']}",
                "detail": f"${worst['net_pnl']:+.2f} (胜率 {worst['win_rate']}%)",
                "action": "建议降级或暂停",
            })

        # 手续费最高
        fee_hog = max(strategies, key=lambda x: x["fee_ratio"])
        if fee_hog["fee_ratio"] > 40:
            recs.append({
                "type": "手续费吞噬",
                "item": f"{fee_hog['coin']}.{fee_hog['strategy']}",
                "detail": f"手续费占比 {fee_hog['fee_ratio']:.0f}%",
                "action": "建议调整网格档位减少交易频率",
            })

        # 回撤最大
        dd_max = max(strategies, key=lambda x: x["max_drawdown"])
        if dd_max["max_drawdown"] > 8:
            recs.append({
                "type": "最大回撤",
                "item": f"{dd_max['coin']}.{dd_max['strategy']}",
                "detail": f"回撤 {dd_max['max_drawdown']:.1f}%",
                "action": "建议降低杠杆或仓位",
            })

        # 评分最低
        worst_score = min(strategies, key=lambda x: x["score"])
        if worst_score["score"] < 40:
            recs.append({
                "type": "综合最差",
                "item": f"{worst_score['coin']}.{worst_score['strategy']}",
                "detail": f"评分 {worst_score['score']}/100",
                "action": "建议暂停并回测验证",
            })

        return recs

    def _format_summary(self, strategies: list, recs: list) -> str:
        lines = []
        total_pnl = sum(s["net_pnl"] for s in strategies)
        total_trades = sum(s["trades"] for s in strategies)
        total_fees = sum(s["total_fees"] for s in strategies)

        lines.append(f"总PnL: ${total_pnl:+.2f} | 总交易: {total_trades} | 总手续费: ${total_fees:.3f}")

        if recs:
            lines.append(f"\n建议 ({len(recs)} 条):")
            for r in recs:
                lines.append(f"  [{r['type']}] {r['item']}: {r['detail']} → {r['action']}")

        return "\n".join(lines)

    def print_report(self, date: str = None):
        report = self.daily_summary(date)

        print(f"\n📊 策略贡献报表 ({report['date']})")
        print("=" * 60)

        strategies = report["strategies"]
        if not strategies:
            print("  今日无交易数据")
            return

        print(f"\n{'币种':10s} {'策略':12s} {'PnL':>8s}  {'胜率':>6s}  {'回撤':>6s}  {'手续费':>7s}  {'评分':>5s}")
        print("-" * 60)
        for s in strategies:
            pnl_str = f"${s['net_pnl']:+.2f}"
            wr_str = f"{s['win_rate']:.0f}%"
            dd_str = f"{s['max_drawdown']:.1f}%"
            fee_str = f"{s['fee_ratio']:.0f}%"
            score_str = f"{s['score']}"
            print(f"{s['coin']:10s} {s['strategy']:12s} {pnl_str:>8s}  {wr_str:>6s}  {dd_str:>6s}  {fee_str:>7s}  {score_str:>5s}")

        print(f"\n📋 {report['summary']}")

        recs = report.get("recommendations", [])
        if recs:
            print(f"\n💡 今日建议 ({len(recs)} 条):")
            for r in recs:
                print(f"  [{r['type']}] {r['item']}")
                print(f"    {r['detail']}")
                print(f"    → {r['action']}")


# ═══════════════════════════════════════════════════════════
# 历史对比
# ═══════════════════════════════════════════════════════════

def compare_days(date1: str, date2: str) -> dict:
    """比较两天的策略表现，找趋势"""
    sr = StrategyReporter()
    r1 = sr.daily_summary(date1)
    r2 = sr.daily_summary(date2)

    s1 = {f"{s['coin']}.{s['strategy']}": s for s in r1["strategies"]}
    s2 = {f"{s['coin']}.{s['strategy']}": s for s in r2["strategies"]}

    changes = []
    for key in set(s1) | set(s2):
        pnl1 = s1.get(key, {}).get("net_pnl", 0)
        pnl2 = s2.get(key, {}).get("net_pnl", 0)
        trend = "📈" if pnl2 > pnl1 else ("📉" if pnl2 < pnl1 else "➡️")
        changes.append({
            "strategy": key,
            "pnl_change": round(pnl2 - pnl1, 2),
            "trend": trend,
        })

    changes.sort(key=lambda x: x["pnl_change"], reverse=True)
    print(f"\n📈 策略趋势对比 ({date1} → {date2}):")
    for c in changes:
        print(f"  {c['trend']} {c['strategy']}: ${c['pnl_change']:+.2f}")

    return changes


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    sr = StrategyReporter()

    if len(sys.argv) > 1 and sys.argv[1] == "compare":
        d1 = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        d2 = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        compare_days(d1, d2)
    else:
        sr.print_report()

        # demo
        if not sr.state:
            print("\n\n📝 Demo — 模拟数据:")
            demo_data = [
                ("ETH", "grid", 8.4, 1.1, 12, 7, 2.5),
                ("TRX", "grid", 1.2, 0.3, 8, 5, 1.2),
                ("SOL", "trend", -2.1, 0.6, 5, 3, 2.8),
                ("TRX_SWAP", "futures_grid", 0.8, 0.4, 6, 4, 1.2),
                ("ETH_SWAP", "futures_grid", 3.5, 0.9, 10, 6, 3.0),
                ("SOL_SWAP", "futures_trend", -1.5, 0.5, 4, 2, 3.5),
            ]
            for coin, strat, pnl, fee, trades, wins, dd in demo_data:
                for _ in range(trades):
                    sr.record(coin, strat, pnl / trades, fee / trades)
            sr.print_report()
