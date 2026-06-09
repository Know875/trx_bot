#!/usr/bin/env python3
"""
每日评分 → StrategyGuard 联动

每天凌晨自动运行，将 strategy_report 的日评分喂入 StrategyGuard，
触发：低分暂停 / 连低降仓 / 连低禁用 / 试运行复检 等保护机制。

用法:
  python daily_scorer.py              # 手动跑一次
  python daily_scorer.py --date 2026-06-08  # 指定日期

Cron (建议每天凌晨1:00):
  0 1 * * * cd /root/projects/trx_bot && python3 daily_scorer.py >> daily_scorer.log 2>&1
"""

import sys, os, logging, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [daily_score] %(message)s")
logger = logging.getLogger("daily_scorer")

from strategy_report import StrategyReporter
from strategy_guard import StrategyGuard


def run(date: str = None):
    sr = StrategyReporter()
    sg = StrategyGuard()
    summary = sr.daily_summary(date)

    strategies = summary.get('strategies', [])
    if not strategies:
        logger.warning(f"{summary.get('date', date)} 无策略数据，跳过评分")
        return

    scored = 0
    for s in strategies:
        coin = s['coin']
        strategy = s['strategy']

        # cleanup 条目不算真实策略
        if strategy == 'cleanup':
            continue

        score = int(s.get('score', 0))
        net_pnl = s.get('net_pnl', 0)
        sg.update_score(coin, strategy, score)
        scored += 1
        logger.info(f"  {coin}:{strategy} score={score}/100 PnL={net_pnl:+.2f}")

    logger.info(f"已完成 {scored} 个策略评分 ({summary.get('date', date)})")
    print()
    sg.print_status()

    # 输出建议
    recs = summary.get('recommendations', [])
    if recs:
        print(f"\n📋 日报建议 ({len(recs)} 条):")
        for r in recs:
            print(f"  [{r.get('type', '')}] {r.get('item', '')}: {r.get('detail', '')} → {r.get('action', '')}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--date", type=str, default=None, help="指定日期 (YYYY-MM-DD)")
    args = p.parse_args()
    run(args.date)
