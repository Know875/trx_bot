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
        from param_score import is_extreme_market
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
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    results = []

    for l in s["lines"]:
        key = l["key"]
        coin = key.split(":")[0]
        pc = per_coin.get(coin, {})
        cons = pc.get("consecutive_losses", 0) or 0

        cooled_until = safe_iso_to_dt(guard_state.get("coin_cooled_until", {}).get(coin))
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
