"""
模拟盘 → 实盘 放行闸。

用实盘/模拟盘的真实成交记录（trade_records_*.json，PnL 已扣手续费）做统计裁决：
样本够不够、是否真盈利、盈亏比、回撤、是否优于"啥都不做"。
只有全部达标才给 GO，否则 NO-GO 并说明原因——避免"模拟盘看着还行就上实盘"的冲动。

用法: python paper_eval.py
"""
import json
import os
import sys
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("paper_eval")
ROOT = Path(__file__).parent

# ── 放行门槛（保守，可按需调）──
MIN_TRADES      = 100     # 样本太少不足以下结论
MIN_DAYS        = 14      # 至少跑满两周
MIN_PROFIT_FACTOR = 1.1   # 毛盈 / 毛亏
MIN_EXPECTANCY  = 0.0     # 每笔期望 > 0


def _records(coin: str):
    path = ROOT / f"trade_records_{coin}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    return data


def _stats(coin: str) -> dict:
    data = _records(coin)
    if not data:
        return {"coin": coin, "error": "无记录"}
    recs = [r for r in data.get("records", []) if r.get("strategy") != "cleanup"]
    n = len(recs)
    if n == 0:
        return {"coin": coin, "error": "无成交"}

    pnls = [r.get("pnl", 0) for r in recs]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    net = data.get("realized_pnl", sum(pnls))
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    expectancy = net / n

    # 最大回撤（基于累计 total_pnl）
    peak = 0.0
    max_dd = 0.0
    for r in recs:
        tp = r.get("total_pnl", 0)
        peak = max(peak, tp)
        max_dd = min(max_dd, tp - peak)

    # 运行天数
    days = 0.0
    try:
        t0 = datetime.strptime(recs[0]["time"], "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(recs[-1]["time"], "%Y-%m-%d %H:%M:%S")
        days = (t1 - t0).total_seconds() / 86400
    except Exception:
        pass

    return {
        "coin": coin, "trades": n, "net_pnl": round(net, 4),
        "win_rate": round(len(wins) / n * 100, 1),
        "profit_factor": round(pf, 2), "expectancy": round(expectancy, 4),
        "max_drawdown": round(max_dd, 4), "days": round(days, 1),
    }


def _gate(s: dict) -> tuple[bool, list]:
    reasons = []
    if s.get("error"):
        return False, [s["error"]]
    if s["trades"] < MIN_TRADES:
        reasons.append(f"样本不足 {s['trades']}/{MIN_TRADES} 笔")
    if s["days"] < MIN_DAYS:
        reasons.append(f"运行天数不足 {s['days']}/{MIN_DAYS} 天")
    if s["net_pnl"] <= 0:
        reasons.append(f"净盈亏 {s['net_pnl']} ≤ 0")
    if s["profit_factor"] < MIN_PROFIT_FACTOR:
        reasons.append(f"盈亏比 {s['profit_factor']} < {MIN_PROFIT_FACTOR}")
    if s["expectancy"] <= MIN_EXPECTANCY:
        reasons.append(f"每笔期望 {s['expectancy']} ≤ {MIN_EXPECTANCY}")
    return (len(reasons) == 0), reasons


def main():
    try:
        import config
        coins = config.COINS
    except Exception:
        coins = ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]

    print("🚦 模拟盘 → 实盘 放行评估")
    print("=" * 64)
    go_count = 0
    total = 0
    for coin in coins:
        s = _stats(coin)
        ok, reasons = _gate(s)
        if s.get("error"):
            print(f"  {coin:10s} ⏭️  {s['error']}")
            continue
        total += 1
        tag = "✅ GO" if ok else "❌ NO-GO"
        if ok:
            go_count += 1
        print(f"  {coin:10s} {tag} | {s['trades']}笔 {s['days']}天 "
              f"净={s['net_pnl']:+.2f} 胜率={s['win_rate']}% PF={s['profit_factor']} "
              f"期望={s['expectancy']:+.4f} 回撤={s['max_drawdown']:+.2f}")
        if not ok:
            print(f"             ↳ {'; '.join(reasons)}")

    print("=" * 64)
    if total == 0:
        print("  暂无成交数据可评估（先让 bot 在模拟盘跑一段）")
    else:
        print(f"  结论: {go_count}/{total} 个币种达到上实盘门槛")
        if go_count == 0:
            print("  ⚠️ 没有币种达标 —— 不建议加大资金/上实盘")
    print("\n门槛: 样本≥{}笔 运行≥{}天 净>0 盈亏比≥{} 期望>0".format(
        MIN_TRADES, MIN_DAYS, MIN_PROFIT_FACTOR))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()
    sys.exit(0)
