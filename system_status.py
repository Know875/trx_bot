"""
统一状态面板 — 一眼看懂所有模块

用法:
  python system_status.py          # 一次性输出
  python system_status.py watch    # 每5分钟刷新
"""

import json, os, time, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent

def load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def safe_iso_to_dt(s: str):
    """安全解析 ISO 时间戳, 统一返回 naive UTC datetime"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None

# ═══════════════════════════════════════════════════════════

def read_guard() -> dict:
    from account_guard import Guard
    try:
        g = Guard()
        r = g.status_report()
        icon_map = {"normal": "🟢", "warn": "🟡", "protect": "🔴", "halt": "💀"}
        return {
            "icon": icon_map.get(r.get("status", "normal"), "⚪"),
            "status": r.get("status", "normal"),
            "daily_pnl": r.get("daily_pnl", 0),
            "daily_pnl_pct": r.get("daily_pnl_pct", 0),
            "alerts": len(r.get("alerts", [])),
        }
    except Exception:
        return {"icon": "⚪", "status": "no_data", "daily_pnl": 0, "daily_pnl_pct": 0, "alerts": 0}

def read_extreme() -> dict:
    try:
        from brain import is_extreme_market
        return is_extreme_market()
    except Exception:
        return {"is_extreme": False, "reasons": []}

def read_macro() -> dict:
    try:
        from macro import MacroIntelligence
        mi = MacroIntelligence()
        a = mi.check_signals()
        return {"risk_score": a.get("risk_score", 0), "position_mult": a.get("position_multiplier", 1.0)}
    except Exception:
        return {"risk_score": 0, "position_mult": 1.0}

def read_strategies() -> list:
    from strategy_guard import StrategyGuard
    from account_guard import Guard
    sg = StrategyGuard()
    s = sg.status()
    g = Guard()
    guard_rpt = g.status_report()
    per_coin = guard_rpt.get("per_coin", {})
    # 用 Guard SQLite 后端获取冷却状态，不再读旧的 JSON 文件
    cooled_until_map = guard_rpt.get("coin_cooled_until", {})
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    results = []

    for l in s["lines"]:
        key = l["key"]
        coin = key.split(":")[0]
        pc = per_coin.get(coin, {})
        cons = pc.get("consecutive_losses", 0) or 0

        cooled_until = safe_iso_to_dt(cooled_until_map.get(coin))
        coin_cooled = cooled_until is not None and now_naive < cooled_until

        results.append({
            "key": key, "icon": l["icon"], "mode": l["mode"],
            "position_mult": l["position_mult"], "reason": l["reason"],
            "cons_losses": cons, "coin_cooled": coin_cooled,
        })
    return results

def read_brain() -> dict:
    state = load_json(str(ROOT / "brain_state.json"))
    queue = state.get("rollback_queue", [])
    pending = [r for r in queue if not r.get("evaluated")]
    return {
        "explored": state.get("total_explored", len(state.get("exploration", {}))),
        "rollback_pending": len(pending),
        "auto_tune_runs": len(state.get("auto_trials", [])),
    }

def read_cooldowns() -> list:
    """从 param_scores.json 读取冷却黑名单"""
    cd = load_json(str(ROOT / "param_scores.json"))
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    results = []
    for key, info in cd.get("cooldown", {}).items():
        rolled = safe_iso_to_dt(info.get("rolled_at", ""))
        if rolled:
            days_left = 7 - (now_naive - rolled).total_seconds() / 86400
            if days_left > 0:
                results.append(f"{key} ({days_left:.1f}d left)")
    return results

# ═══════════════════════════════════════════════════════════

def build_panel():
    lines = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines.append(f"📊 系统状态 — {now_str}")
    lines.append("═" * 56)

    # 账户
    g = read_guard()
    pnl_str = f"${g['daily_pnl']:+.2f} ({g['daily_pnl_pct']:+.1f}%)"
    alert_str = f" | ⚠️ {g['alerts']} 告警" if g["alerts"] else ""
    lines.append(f"🏦 账户:    {g['icon']} {g['status']:8s}  日PnL {pnl_str}{alert_str}")

    # 极端行情
    ex = read_extreme()
    ex_icon = "🔴" if ex.get("is_extreme") else "🟢"
    ex_info = " / ".join(ex.get("reasons", [])) if ex.get("is_extreme") else "无"
    lines.append(f"🌋 极端:    {ex_icon} {'⚠️ 是' if ex.get('is_extreme') else '否'}\t{ex_info}")

    # 宏观
    m = read_macro()
    risk_icon = "🟢" if m["risk_score"] < 40 else ("🟡" if m["risk_score"] < 60 else "🔴")
    lines.append(f"🌐 宏观:    {risk_icon} 风险 {m['risk_score']}%  仓位系数 ×{m['position_mult']:.2f}")

    # 策略
    lines.append(f"\n📋 策略线程:")
    strategies = read_strategies()
    by_coin = {}
    for s in strategies:
        coin = s["key"].split(":")[0]
        by_coin.setdefault(coin, []).append(s)

    for coin, items in by_coin.items():
        for s in items:
            strategy = s["key"].split(":")[1] if ":" in s["key"] else s["key"]
            icon = s["icon"].replace("⚠️", "🟡")
            extras = []
            if s["cons_losses"] >= 3:
                extras.append(f"连亏{s['cons_losses']}")
            if s["coin_cooled"]:
                extras.append("币冷却")
            if s["mode"] == "probation":
                extras.append(f"试运行 ×{s['position_mult']:.0%}")
            elif s["mode"] == "reduced":
                extras.append(f"减半 ×{s['position_mult']:.0%}")
            elif s["reason"] and s["mode"] != "normal":
                extras.append(s["reason"])
            extra_str = f" — {', '.join(extras)}" if extras else ""
            lines.append(f"  {icon} {coin:8s}.{strategy:18s} {s['mode']}{extra_str}")

    # 自进化
    b = read_brain()
    lines.append(f"\n🧬 自进化: 探索{b['explored']}组 | 待回滚{b['rollback_pending']} | 自动调参{b['auto_tune_runs']}次")

    # 冷却黑名单
    cooldowns = read_cooldowns()
    if cooldowns:
        lines.append(f"🧊 参数冷却: {len(cooldowns)} 项 — {', '.join(cooldowns)}")
    else:
        lines.append(f"🧊 参数冷却: 0")

    # 汇总
    modes = [s["mode"] for s in strategies]
    normal = modes.count("normal") if modes else 0
    issues = len(modes) - normal if modes else 0
    prob = modes.count("probation") if modes else 0
    lines.append(f"\n{'─' * 56}")
    lines.append(f"📊 总计: {len(strategies)} 策略 | {issues} 异常 | {normal} 正常 | {prob} 试运行")

    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        try:
            while True:
                os.system("clear")
                print(build_panel())
                print(f"\n⏱️  5分钟刷新 | Ctrl-C 退出")
                time.sleep(300)
        except KeyboardInterrupt:
            print("\n👋 退出")
    else:
        print(build_panel())


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
    import backtest_calibrate
    return backtest_calibrate.analyze(coin)


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


def _cli_measure():
    main()
