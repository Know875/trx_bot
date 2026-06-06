"""
策略级熔断 — 在账户熔断和参数回滚之间。

不做"一刀切停币种"，而是精确到策略：
  strategy_score < 25   → 暂停72h，恢复后试运行12h (30%仓位)
  strategy_score < 40   → 暂停24h，恢复后试运行12h (30%仓位)
  连续2天评分 < 40      → 仓位减半
  连续3天评分 < 40      → 禁用，需人工确认
  试运行结束评分 < 40   → 永久禁用
  试运行结束评分 ≥ 50   → 恢复正常

用法:
  from strategy_guard import StrategyGuard
  sg = StrategyGuard()
  result = sg.check(coin, strategy)  # 每次策略决策前
"""

import json, os, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = str(ROOT / "strategy_guard_state.json")

logger = logging.getLogger("strategy_guard")

# 评分阈值
SCORE_PAUSE_24H     = 40   # <40 → 暂停24h
SCORE_PAUSE_72H     = 25   # <25 → 暂停72h
CONSECUTIVE_REDUCE  = 2    # 连续N天 <40 → 仓位减半
CONSECUTIVE_DISABLE = 3    # 连续N天 <40 → 禁用
POSITION_REDUCTION  = 0.50  # 仓位减半

# 恢复复检（防止刚恢复又亏）
PROBATION_HOURS      = 12    # 恢复后试运行时长
PROBATION_POSITION   = 0.30  # 试运行仓位 30%
PROBATION_PASS_SCORE = 50    # 试运行结束评分 ≥50 → 恢复正常

# 所有策略白名单
ALL_STRATEGIES = {
    "TRX":      ["trx_adaptive"],
    "ETH":      ["grid", "trend"],
    "SOL":      ["grid", "trend"],
    "TRX_SWAP": ["trx_adaptive_futures"],
    "ETH_SWAP": ["futures_grid", "futures_trend"],
    "SOL_SWAP": ["futures_grid", "futures_trend"],
}


class StrategyGuard:
    """每策略熔断，比账户级更精准"""

    def __init__(self):
        self._load()

    def _load(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                self.state = json.load(f)
        else:
            self.state = self._new_state()

    def _save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False, default=str)

    def _new_state(self):
        return {
            "daily_scores": {},
            "paused_until": {},
            "probation_until": {},
            "disabled": {},
            "consecutive_bad": {},
            "last_score": {},
            "history": [],
        }

    # ═══════════════════════════════════════════════════════

    def _key(self, coin: str, strategy: str) -> str:
        return f"{coin}:{strategy}"

    def update_score(self, coin: str, strategy: str, score: int):
        """由 strategy_report 每日调用，更新评分"""
        today = str(datetime.now(timezone.utc).date())
        key = self._key(coin, strategy)

        self.state.setdefault("daily_scores", {}).setdefault(today, {})[key] = score
        self.state.setdefault("last_score", {})[key] = score

        cb = self.state.setdefault("consecutive_bad", {})
        if score < SCORE_PAUSE_24H:
            cb[key] = cb.get(key, 0) + 1
        else:
            cb[key] = 0

        self.state.setdefault("history", []).append({
            "time": datetime.now(timezone.utc).isoformat(),
            "action": "score_update",
            "coin": coin, "strategy": strategy, "score": score,
        })
        if len(self.state["history"]) > 30:
            self.state["history"] = self.state["history"][-30:]
        self._save()

    # ═══════════════════════════════════════════════════════
    # 核心检查逻辑
    # ═══════════════════════════════════════════════════════

    def check(self, coin: str, strategy: str) -> dict:
        """
        每次策略决策前调用。
        返回 {'allowed': bool, 'mode': str, 'position_mult': float, 'reason': str}
        """
        key = self._key(coin, strategy)
        now = datetime.now(timezone.utc)

        result = {"allowed": True, "mode": "normal", "position_mult": 1.0, "reason": ""}

        # ── 1. 永久禁用 ──
        disabled_info = self.state.get("disabled", {}).get(key)
        if disabled_info:
            return {"allowed": False, "mode": "disabled", "position_mult": 0, "reason": f"策略已禁用: {disabled_info}"}

        # ── 2. 试运行（复检期） ──
        probation_until_str = self.state.get("probation_until", {}).get(key)
        if probation_until_str:
            probation_until = datetime.fromisoformat(probation_until_str.replace("Z", "+00:00"))
            if now < probation_until:
                remaining_h = (probation_until - now).total_seconds() / 3600
                result["allowed"] = True
                result["mode"] = "probation"
                result["position_mult"] = PROBATION_POSITION
                result["reason"] = f"试运行中 ({PROBATION_POSITION:.0%}仓位)，剩余 {remaining_h:.1f}h"
                return result
            else:
                # 试运行到期 → 评估
                last = self.state.get("last_score", {}).get(key)
                del self.state["probation_until"][key]
                if last is not None and last < SCORE_PAUSE_24H:
                    # 试运行结束仍不合格 → 永久禁用
                    self.state.setdefault("disabled", {})[key] = f"试运行结束评分{last}<{SCORE_PAUSE_24H}，永久禁用"
                    self._save()
                    return {"allowed": False, "mode": "disabled", "position_mult": 0,
                            "reason": f"试运行失败: 评分{last}<{SCORE_PAUSE_24H}，已永久禁用"}
                elif last is not None and last >= PROBATION_PASS_SCORE:
                    # 合格 → 恢复
                    self.state.get("consecutive_bad", {})[key] = 0
                    self._save()
                    logger.info(f"✅ {key} 试运行通过 (评分{last})，恢复正常")
                else:
                    # 中间状态（40-49）→ 延长观察，但先恢复正常仓位
                    self.state.get("consecutive_bad", {})[key] = 0
                    self._save()
                    logger.info(f"👀 {key} 试运行结束 (评分{last})，恢复但仍需观察")
                # fall through to normal

        # ── 3. 暂停期 ──
        paused_until_str = self.state.get("paused_until", {}).get(key)
        if paused_until_str:
            paused_until = datetime.fromisoformat(paused_until_str.replace("Z", "+00:00"))
            if now < paused_until:
                remaining_h = (paused_until - now).total_seconds() / 3600
                return {"allowed": False, "mode": "paused", "position_mult": 0,
                        "reason": f"策略暂停中，剩余 {remaining_h:.1f}h"}
            else:
                # 暂停到期 → 进入试运行
                del self.state["paused_until"][key]
                probation_until = now + timedelta(hours=PROBATION_HOURS)
                self.state.setdefault("probation_until", {})[key] = probation_until.isoformat()
                self._save()
                logger.info(f"🔄 {key} 暂停到期，进入 {PROBATION_HOURS}h 试运行 ({PROBATION_POSITION:.0%}仓位)")
                return {"allowed": True, "mode": "probation", "position_mult": PROBATION_POSITION,
                        "reason": f"恢复试运行 ({PROBATION_POSITION:.0%}仓位)，{PROBATION_HOURS}h 后评估"}

        # ── 4. 连续低评分 ──
        cb = self.state.get("consecutive_bad", {}).get(key, 0)

        if cb >= CONSECUTIVE_DISABLE:
            self.state.setdefault("disabled", {})[key] = f"连续{cb}天评分<{SCORE_PAUSE_24H}，自动禁用"
            self.state["consecutive_bad"][key] = 0
            self._save()
            return {"allowed": False, "mode": "disabled", "position_mult": 0,
                    "reason": f"连续{cb}天低评分，已禁用"}

        if cb >= CONSECUTIVE_REDUCE:
            return {"allowed": True, "mode": "reduced", "position_mult": POSITION_REDUCTION,
                    "reason": f"连续{cb}天评分<{SCORE_PAUSE_24H}，仓位减半"}

        # ── 5. 评分阈值检查 ──
        last_score = self.state.get("last_score", {}).get(key)

        if last_score is not None and last_score < SCORE_PAUSE_72H:
            paused_until = now + timedelta(hours=72)
            self.state.setdefault("paused_until", {})[key] = paused_until.isoformat()
            self._save()
            return {"allowed": False, "mode": "paused", "position_mult": 0,
                    "reason": f"评分{last_score}<{SCORE_PAUSE_72H}，暂停72h"}

        if last_score is not None and last_score < SCORE_PAUSE_24H:
            paused_until = now + timedelta(hours=24)
            self.state.setdefault("paused_until", {})[key] = paused_until.isoformat()
            self._save()
            return {"allowed": False, "mode": "paused", "position_mult": 0,
                    "reason": f"评分{last_score}<{SCORE_PAUSE_24H}，暂停24h"}

        return result

    # ═══════════════════════════════════════════════════════
    # 管理方法
    # ═══════════════════════════════════════════════════════

    def manual_enable(self, coin: str, strategy: str):
        """人工恢复禁用策略"""
        key = self._key(coin, strategy)
        self.state.get("disabled", {}).pop(key, None)
        self.state.get("paused_until", {}).pop(key, None)
        self.state.get("probation_until", {}).pop(key, None)
        self.state.get("consecutive_bad", {})[key] = 0
        self.state.setdefault("history", []).append({
            "time": datetime.now(timezone.utc).isoformat(),
            "action": "manual_enable",
            "coin": coin, "strategy": strategy,
        })
        self._save()
        logger.info(f"✅ 人工恢复 {key}")

    def manual_disable(self, coin: str, strategy: str, reason: str = "人工禁用"):
        """强制禁用策略"""
        key = self._key(coin, strategy)
        self.state.setdefault("disabled", {})[key] = reason
        self.state.get("paused_until", {}).pop(key, None)
        self.state.get("probation_until", {}).pop(key, None)
        self._save()
        logger.info(f"🚫 人工禁用 {key}: {reason}")

    @staticmethod
    def _get_coin_strategies(coin: str) -> list:
        return ALL_STRATEGIES.get(coin, [])

    def has_active(self, coin: str) -> bool:
        for strat in ALL_STRATEGIES.get(coin, []):
            if self.check(coin, strat)["allowed"]:
                return True
        return False

    # ═══════════════════════════════════════════════════════
    # 状态面板
    # ═══════════════════════════════════════════════════════

    def status(self) -> dict:
        now = datetime.now(timezone.utc)
        counts = {"active": 0, "probation": 0, "paused": 0, "reduced": 0, "disabled": 0}
        lines = []

        for coin, strats in ALL_STRATEGIES.items():
            for strat in strats:
                key = self._key(coin, strat)
                res = self.check(coin, strat)
                mode = res["mode"]
                counts[mode] = counts.get(mode, 0) + 1

                icon_map = {
                    "normal": "🟢", "probation": "🟡", "reduced": "⚠️",
                    "paused": "🔴", "disabled": "💀",
                }
                icon = icon_map.get(mode, "❓")
                lines.append({
                    "icon": icon, "key": key, "mode": mode,
                    "position_mult": res["position_mult"],
                    "reason": res["reason"],
                })

        return {"counts": counts, "lines": lines}

    def print_status(self):
        s = self.status()
        c = s["counts"]
        total = sum(c.values())
        ok = c.get("active", 0)
        issues = []
        if c.get("probation"): issues.append(f"{c['probation']} 试运行")
        if c.get("paused"): issues.append(f"{c['paused']} 暂停")
        if c.get("reduced"): issues.append(f"{c['reduced']} 减半")
        if c.get("disabled"): issues.append(f"{c['disabled']} 禁用")

        print(f"\n🛡️ 策略熔断 ({ok}/{total} 正常)")
        print("═" * 50)
        for l in s["lines"]:
            tag = f"(×{l['position_mult']:.0%})" if l["position_mult"] < 1 else ""
            info = f"  {l['reason']}" if l["reason"] else ""
            print(f"  {l['icon']} {l['key']:28s} {tag}{info}")
        if issues:
            print(f"\n⚠️ 需关注: {', '.join(issues)}")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    sg = StrategyGuard()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "enable" and len(sys.argv) >= 4:
            sg.manual_enable(sys.argv[2], sys.argv[3])
        elif cmd == "disable" and len(sys.argv) >= 4:
            reason = sys.argv[4] if len(sys.argv) > 4 else "人工禁用"
            sg.manual_disable(sys.argv[2], sys.argv[3], reason)

        elif cmd == "test-probation":
            print("📝 试运行流程测试:")
            # 模拟低分触发暂停
            sg.update_score("SOL", "trend", 35)
            r1 = sg.check("SOL", "trend")
            print(f"  1. 评分35: {r1['mode']} → {r1['reason']}")

            # 模拟暂停到期 (需要改文件偷步)
            import json
            with open(STATE_FILE) as f:
                state = json.load(f)
            # 把 paused_until 设成1分钟前
            state["paused_until"]["SOL:trend"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            sg._load()
            r2 = sg.check("SOL", "trend")
            print(f"  2. 暂停到期: {r2['mode']} → {r2['reason']} (仓位×{r2['position_mult']:.0%})")

            # 模拟试运行结束+评分仍低
            sg.update_score("SOL", "trend", 30)
            state.setdefault("probation_until", {})["SOL:trend"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            sg._load()
            r3 = sg.check("SOL", "trend")
            print(f"  3. 试运行失败(评分30): {r3['mode']} → {r3['reason']}")

            # 恢复后重新测试正常流程
            sg.manual_enable("SOL", "trend")
            sg.update_score("SOL", "trend", 55)
            r4 = sg.check("SOL", "trend")
            print(f"  4. 人工恢复+评分55: {r4['mode']} ✅")

        elif cmd == "test":
            sg.update_score("SOL", "trend", 35)
            sg.update_score("ETH", "grid", 88)
            sg.update_score("TRX_SWAP", "trx_adaptive_futures", 72)
            print("📝 策略熔断:")
            for c, s in [("SOL","trend"),("SOL","grid"),("ETH","grid"),("TRX_SWAP","trx_adaptive_futures")]:
                r = sg.check(c, s)
                print(f"  {c}.{s}: {r['mode']} {'✅' if r['allowed'] else '❌'} {r['reason']}")

        else:
            sg.print_status()
    else:
        sg.print_status()
