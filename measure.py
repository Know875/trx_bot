"""
测量流程 — 一键汇总裁决"这套系统到底能不能上实盘"。

把三个诚实工具的结论合成一个 per-coin 裁决：
  ① paper_eval     —— 模拟盘真实成交是否正期望（硬闸，权重最高）
  ② edge_research  —— 历史上是否存在可利用边际（佐证）
  ③ backtest_calibrate —— 回测比实盘乐观了多少（折扣率，佐证）

裁决逻辑（保守）：
  - 无成交/样本不足           → 📭 数据不足（继续在模拟盘跑）
  - paper 未达标               → ❌ NO-GO（实盘没跑出正期望）
  - paper 达标但佐证有疑       → 🟡 WATCH（可能是运气，再观察）
  - paper 达标且佐证一致       → ✅ GO候选（可考虑小资金实盘）

设计原则：每个子工具独立 try/except，缺依赖/缺数据只标 N/A，绝不让一个工具拖垮整份报告。
纯只读分析，不下单、不改任何状态。

用法:
  python measure.py            # 全币种汇总裁决
  python measure.py --json     # JSON 输出
  python measure.py --tg       # 把汇总推到 Telegram
"""
import sys
import os
import io
import json
import math
import argparse
import logging
import contextlib
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger("measure")


def _coins() -> list:
    try:
        import config
        return list(config.COINS)
    except Exception:
        return ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]


def _quiet(fn, *args, **kwargs):
    """调用子工具并吞掉它的 stdout（我们只要返回值）。失败返回 (None, errmsg)。"""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs), None
    except Exception as e:
        return None, str(e)


# ═══════════════════════════════════════════════════════════════
# 单币种测量
# ═══════════════════════════════════════════════════════════════

def measure_coin(coin: str) -> dict:
    out = {"coin": coin}

    # ① paper_eval —— 硬闸
    paper, perr = None, None
    pok, preasons = False, []
    try:
        import paper_eval
        paper = paper_eval._stats(coin)
        pok, preasons = paper_eval._gate(paper)
    except Exception as e:
        perr = str(e)
    out["paper"] = paper
    out["paper_ok"] = pok
    out["paper_reasons"] = preasons
    out["paper_err"] = perr

    # ② edge_research —— 佐证（需 pandas/numpy）
    edge, eerr = _quiet(_edge_analyze, coin)
    out["edge"] = edge
    out["edge_err"] = eerr

    # ③ backtest_calibrate —— 佐证（需 pandas/numpy）
    calib, cerr = _quiet(_calibrate, coin)
    out["calib"] = calib
    out["calib_err"] = cerr

    # ── 合成裁决 ──
    out.update(_verdict(paper, pok, preasons, edge, calib, perr))
    return out


def _edge_analyze(coin):
    import edge_research
    return edge_research.analyze(coin)


def _calibrate(coin):
    import backtest_calibrate
    return backtest_calibrate.calibrate(coin)


def _verdict(paper, pok, preasons, edge, calib, perr) -> dict:
    # 数据不足
    if perr:
        return {"verdict": "⚠️ 工具错误", "note": f"paper_eval 异常: {perr}"}
    if not paper or paper.get("error"):
        msg = paper.get("error", "无记录") if paper else "无记录"
        return {"verdict": "📭 数据不足", "note": f"{msg} —— 继续在模拟盘跑满样本"}

    # paper 硬闸未过
    if not pok:
        return {"verdict": "❌ NO-GO", "note": "实盘未达标: " + "; ".join(preasons)}

    # paper 达标 → 看佐证有没有疑点
    concerns = []
    if edge:
        gnet = edge.get("grid_net_pct")
        bh = edge.get("buy_hold_pct")
        if gnet is not None and bh is not None and gnet < bh:
            concerns.append("回测网格跑不赢买入持有")
        if gnet is not None and gnet <= 0:
            concerns.append("回测网格扣成本后≤0")
        ac = edge.get("autocorr_lag1")
        n = edge.get("n", 0)
        if ac is not None and n:
            sig = 1.96 / math.sqrt(n)
            if abs(ac) < sig:
                concerns.append("收益接近随机（无结构性边际）")
    if calib:
        d = calib.get("discount_factor", 1.0)
        if d < 0.7:
            concerns.append(f"回测虚高(折扣{d:.0%})")

    if concerns:
        return {"verdict": "🟡 WATCH", "note": "实盘正期望但佐证有疑: " + "; ".join(concerns)}
    # 佐证全缺失（无 backtest_data / 缺依赖）→ 不能把"没疑点"当"通过"
    if edge is None and calib is None:
        return {"verdict": "🟡 WATCH",
                "note": "实盘正期望，但回测佐证缺失（无 backtest_data 或缺依赖）；补数据复测，或自行决定是否凭实盘单独放行"}
    return {"verdict": "✅ GO候选", "note": "实盘正期望且回测佐证一致 → 可考虑小资金实盘"}


# ═══════════════════════════════════════════════════════════════
# 汇总输出
# ═══════════════════════════════════════════════════════════════

def _fmt_line(r: dict) -> str:
    coin = r["coin"]
    p = r.get("paper") or {}
    bits = []
    if p and not p.get("error"):
        bits.append(f"{p.get('trades',0)}笔/{p.get('days',0)}天")
        bits.append(f"净{p.get('net_pnl',0):+.2f}")
        bits.append(f"PF{p.get('profit_factor',0)}")
        bits.append(f"期望{p.get('expectancy',0):+.4f}")
    edge = r.get("edge")
    if edge:
        bits.append(f"自相关{edge.get('autocorr_lag1',0):+.3f}")
        if edge.get("grid_net_pct") is not None:
            bits.append(f"网格{edge['grid_net_pct']:+.1f}%vs持有{edge.get('buy_hold_pct',0):+.1f}%")
    calib = r.get("calib")
    if calib:
        bits.append(f"折扣{calib.get('discount_factor',1):.0%}")
    detail = " | ".join(str(b) for b in bits) if bits else "无数据"
    return f"  {coin:10s} {r['verdict']:12s} {detail}\n             ↳ {r['note']}"


def run(coins=None) -> dict:
    coins = coins or _coins()
    results = {c: measure_coin(c) for c in coins}
    # 统计
    counts = {}
    for r in results.values():
        v = r["verdict"].split()[-1] if " " in r["verdict"] else r["verdict"]
        counts[v] = counts.get(v, 0) + 1
    results["_summary"] = {"counts": counts, "time": datetime.now().isoformat()}
    return results


def _overall_reco(results: dict) -> str:
    go = [c for c, r in results.items() if c != "_summary" and "GO候选" in r["verdict"]]
    watch = [c for c, r in results.items() if c != "_summary" and "WATCH" in r["verdict"]]
    nogo = [c for c, r in results.items() if c != "_summary" and "NO-GO" in r["verdict"]]
    if go:
        return f"✅ 可考虑对 {', '.join(go)} 上小资金实盘；{', '.join(watch) or '其余'} 继续观察"
    if watch:
        return f"🟡 暂无完全达标币种；{', '.join(watch)} 接近，再观察。不建议加大资金"
    if nogo:
        return "❌ 没有币种达标 —— 不建议上实盘/加钱，先排查或转向 carry 研究"
    return "📭 数据不足 —— 让 bot 在模拟盘继续跑满样本再来测"


def main():
    parser = argparse.ArgumentParser(description="测量流程：汇总裁决能否上实盘")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--tg", action="store_true", help="推送汇总到 Telegram")
    parser.add_argument("--coin", default="", help="只测指定币种")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [measure] %(levelname)s: %(message)s")

    coins = [args.coin] if args.coin else _coins()
    results = run(coins)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return

    print(f"\n{'═'*72}")
    print(f"  📊 测量流程汇总  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  (paper_eval=硬闸 | edge_research/backtest_calibrate=佐证)")
    print(f"{'═'*72}")
    for c in coins:
        print(_fmt_line(results[c]))
    print(f"{'─'*72}")
    reco = _overall_reco(results)
    print(f"  结论: {reco}")
    print(f"  分布: {results['_summary']['counts']}")
    print(f"\n  提醒: 这是『测量』不是『建造』。数字说不行就别上钱，别再加功能。")

    if args.tg:
        _push_tg(coins, results, reco)


def _push_tg(coins, results, reco):
    try:
        import config, urllib.request, urllib.parse
        token = getattr(config, "TG_BOT_TOKEN", "")
        chat_id = getattr(config, "TG_CHAT_ID", "")
        if not token or not chat_id:
            logger.warning("未配置 TG，跳过推送")
            return
        lines = [f"📊 测量汇总 {datetime.now().strftime('%m-%d %H:%M')}"]
        for c in coins:
            r = results[c]
            lines.append(f"{c}: {r['verdict']}")
        lines.append(f"\n{reco}")
        text = "\n".join(lines)
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15):
            pass
        print("  已推送 TG")
    except Exception as e:
        logger.warning(f"TG 推送失败: {e}")


if __name__ == "__main__":
    main()
