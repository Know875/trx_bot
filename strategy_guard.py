"""
策略级熔断 — 在账户熔断和参数回滚之间。

不做"一刀切停币种"，而是精确到策略：
  strategy_score < 25   → 暂停72h，恢复后试运行12h (30%仓位)
  strategy_score < 40   → 暂停24h，恢复后试运行12h (30%仓位)
  连续2天评分 < 40      → 仓位减半
  连续3天评分 < 40      → 禁用，冷却72h后自动重新试运行
  试运行结束评分 < 40   → 禁用，冷却72h后自动重新试运行
  试运行结束评分 ≥ 50   → 恢复正常

自动禁用不再"永久死亡"：冷却 DISABLE_COOLDOWN_HOURS 后自动转入试运行(30%仓位)
再给一次机会，避免行情转好后好策略仍被一次坏周期判死。仅 manual_disable()
为真永久禁用（不参与冷却）。

用法:
  from strategy_guard import StrategyGuard
  sg = StrategyGuard()
  result = sg.check(coin, strategy)  # 每次策略决策前

状态存储: SQLite (bot_state.db) — WAL 模式，跨线程安全，自动从 JSON 迁移
"""

import json, os, time, logging, threading
from datetime import datetime, timezone, timedelta

from state_db import StrategyStateDB

logger = logging.getLogger("strategy_guard")

# 多个币种线程 + web 请求共用同一份 state，串行化读改写
_SG_LOCK = threading.RLock()


def _synchronized(func):
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _SG_LOCK:
            return func(*args, **kwargs)
    return wrapper

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

# 自动禁用冷却：冷却到期后自动转入试运行再给一次机会（manual_disable 不参与）
DISABLE_COOLDOWN_HOURS = 72

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
    """每策略熔断，比账户级更精准。底层 SQLite 存储。"""

    def __init__(self):
        self._db = StrategyStateDB()

    # ═══════════════════════════════════════════════════════

    def _key(self, coin: str, strategy: str) -> str:
        return f"{coin}:{strategy}"

    def _clear_probation_entry(self, key: str):
        """清除试运行评分基准（离开试运行时调用）"""
        pe = self._db.get_json("probation_entry", {})
        if key in pe:
            pe.pop(key, None)
            self._db.set_json("probation_entry", pe)

    @staticmethod
    def _disabled_record(reason: str, *, manual: bool, since: str) -> dict:
        """统一禁用记录结构：{reason, manual, since}。manual=True 不参与自动冷却。"""
        return {"reason": reason, "manual": manual, "since": since}

    @staticmethod
    def _norm_disabled(info):
        """兼容旧格式：历史上 disabled[key] 直接存字符串理由（视为可冷却的自动禁用）。"""
        if isinstance(info, dict):
            return dict(info)
        return {"reason": str(info), "manual": False, "since": None}

    @_synchronized
    def update_score(self, coin: str, strategy: str, score: int):
        """由 strategy_report 每日调用，更新评分"""
        today = str(datetime.now(timezone.utc).date())
        key = self._key(coin, strategy)

        daily_scores = self._db.get_json("daily_scores", {})
        daily_scores.setdefault(today, {})[key] = score
        self._db.set_json("daily_scores", daily_scores)

        last_score = self._db.get_json("last_score", {})
        last_score[key] = score
        self._db.set_json("last_score", last_score)

        cb = self._db.get_json("consecutive_bad", {})
        if score < SCORE_PAUSE_24H:
            cb[key] = cb.get(key, 0) + 1
        else:
            cb[key] = 0
        self._db.set_json("consecutive_bad", cb)

        history = self._db.get_json("history", [])
        history.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "action": "score_update",
            "coin": coin, "strategy": strategy, "score": score,
        })
        if len(history) > 30:
            history = history[-30:]
        self._db.set_json("history", history)

    # ═══════════════════════════════════════════════════════
    # 核心检查逻辑
    # ═══════════════════════════════════════════════════════

    @_synchronized
    def check(self, coin: str, strategy: str, peek: bool = False) -> dict:
        """
        每次策略决策前调用。
        返回 {'allowed': bool, 'mode': str, 'position_mult': float, 'reason': str}

        peek=True：只读评估当前生效状态，不推进状态机/不写库（P1-4）。
        用于 status()/has_active()/web 面板/外部 gate 检查，避免"看一眼面板"
        就把策略推进试运行甚至永久禁用。状态机推进只由主交易循环(peek=False)驱动。
        """
        key = self._key(coin, strategy)
        now = datetime.now(timezone.utc)

        result = {"allowed": True, "mode": "normal", "position_mult": 1.0, "reason": ""}

        # ── 1. 禁用（自动禁用可冷却后重试，manual_disable 为真永久） ──
        disabled = self._db.get_json("disabled", {})
        probation = self._db.get_json("probation_until", {})
        disabled_info = disabled.get(key)
        if disabled_info:
            info = self._norm_disabled(disabled_info)
            reason = info.get("reason", "")
            # 人工禁用：永久，不冷却
            if info.get("manual"):
                return {"allowed": False, "mode": "disabled", "position_mult": 0,
                        "reason": f"策略已禁用: {reason}"}
            since_str = info.get("since")
            if since_str is None:
                # 旧格式无时间戳：懒迁移，从现在起开始计冷却（本次仍禁用）
                if not peek:
                    info["since"] = now.isoformat()
                    disabled[key] = info
                    self._db.set_json("disabled", disabled)
                return {"allowed": False, "mode": "disabled", "position_mult": 0,
                        "reason": f"策略已禁用: {reason}（冷却{DISABLE_COOLDOWN_HOURS}h后重试）"}
            since = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
            cooldown_end = since + timedelta(hours=DISABLE_COOLDOWN_HOURS)
            if now < cooldown_end:
                remaining_h = (cooldown_end - now).total_seconds() / 3600
                return {"allowed": False, "mode": "disabled", "position_mult": 0,
                        "reason": f"策略已禁用: {reason}（冷却剩余{remaining_h:.1f}h）"}
            # 冷却到期 → 转入试运行再给一次机会
            if not peek:
                del disabled[key]
                self._db.set_json("disabled", disabled)
                probation[key] = (now + timedelta(hours=PROBATION_HOURS)).isoformat()
                self._db.set_json("probation_until", probation)
                pe = self._db.get_json("probation_entry", {})
                pe[key] = self._db.get_json("last_score", {}).get(key)
                self._db.set_json("probation_entry", pe)
                cb = self._db.get_json("consecutive_bad", {})
                cb[key] = 0
                self._db.set_json("consecutive_bad", cb)
                logger.info(f"🔄 {key} 禁用冷却结束，自动进入 {PROBATION_HOURS}h 试运行 ({PROBATION_POSITION:.0%}仓位)")
            return {"allowed": True, "mode": "probation", "position_mult": PROBATION_POSITION,
                    "reason": f"禁用冷却结束，自动试运行 ({PROBATION_POSITION:.0%}仓位)，{PROBATION_HOURS}h 后评估"}

        # ── 2. 试运行（复检期） ──
        probation_until_str = probation.get(key)
        if probation_until_str:
            probation_until = datetime.fromisoformat(probation_until_str.replace("Z", "+00:00"))
            if now < probation_until:
                remaining_h = (probation_until - now).total_seconds() / 3600
                return {"allowed": True, "mode": "probation",
                        "position_mult": PROBATION_POSITION,
                        "reason": f"试运行中 ({PROBATION_POSITION:.0%}仓位)，剩余 {remaining_h:.1f}h"}
            else:
                # 试运行到期 → 评估。先确认有「新评分」，否则不能用过期评分裁决(P2-2)。
                last_score = self._db.get_json("last_score", {})
                last = last_score.get(key)
                entry = self._db.get_json("probation_entry", {}).get(key)
                if last is None or last == entry:
                    # 进入试运行以来评分未更新 → 顺延，避免拿旧低分误判禁用
                    if not peek:
                        probation[key] = (now + timedelta(hours=PROBATION_HOURS)).isoformat()
                        self._db.set_json("probation_until", probation)
                        logger.info(f"⏳ {key} 试运行期内无新评分，顺延 {PROBATION_HOURS}h 再评估")
                    return {"allowed": True, "mode": "probation", "position_mult": PROBATION_POSITION,
                            "reason": f"试运行顺延（待新评分），{PROBATION_HOURS}h 后再评估"}
                if last < SCORE_PAUSE_24H:
                    if not peek:
                        del probation[key]
                        self._db.set_json("probation_until", probation)
                        self._clear_probation_entry(key)
                        disabled[key] = self._disabled_record(
                            f"试运行结束评分{last}<{SCORE_PAUSE_24H}",
                            manual=False, since=now.isoformat())
                        self._db.set_json("disabled", disabled)
                    return {"allowed": False, "mode": "disabled", "position_mult": 0,
                            "reason": f"试运行失败: 评分{last}<{SCORE_PAUSE_24H}，禁用(冷却{DISABLE_COOLDOWN_HOURS}h后重试)"}
                # 通过/观察 → 恢复正常（cb 归零后即落入 normal，等价于原 fall-through）
                if not peek:
                    del probation[key]
                    self._db.set_json("probation_until", probation)
                    self._clear_probation_entry(key)
                    cb = self._db.get_json("consecutive_bad", {})
                    cb[key] = 0
                    self._db.set_json("consecutive_bad", cb)
                    if last >= PROBATION_PASS_SCORE:
                        logger.info(f"✅ {key} 试运行通过 (评分{last})，恢复正常")
                    else:
                        logger.info(f"👀 {key} 试运行结束 (评分{last})，恢复但仍需观察")
                return result

        # ── 3. 暂停期 ──
        paused = self._db.get_json("paused_until", {})
        paused_until_str = paused.get(key)
        if paused_until_str:
            paused_until = datetime.fromisoformat(paused_until_str.replace("Z", "+00:00"))
            if now < paused_until:
                remaining_h = (paused_until - now).total_seconds() / 3600
                return {"allowed": False, "mode": "paused", "position_mult": 0,
                        "reason": f"策略暂停中，剩余 {remaining_h:.1f}h"}
            else:
                # 暂停到期 → 进入试运行
                if not peek:
                    del paused[key]
                    self._db.set_json("paused_until", paused)
                    probation_until = now + timedelta(hours=PROBATION_HOURS)
                    probation[key] = probation_until.isoformat()
                    self._db.set_json("probation_until", probation)
                    # 记录进入试运行时的评分基准，到期据此判断是否有新评分(P2-2)
                    pe = self._db.get_json("probation_entry", {})
                    pe[key] = self._db.get_json("last_score", {}).get(key)
                    self._db.set_json("probation_entry", pe)
                    logger.info(f"🔄 {key} 暂停到期，进入 {PROBATION_HOURS}h 试运行 ({PROBATION_POSITION:.0%}仓位)")
                return {"allowed": True, "mode": "probation", "position_mult": PROBATION_POSITION,
                        "reason": f"恢复试运行 ({PROBATION_POSITION:.0%}仓位)，{PROBATION_HOURS}h 后评估"}

        # ── 4. 连续低评分 ──
        cb = self._db.get_json("consecutive_bad", {})
        cb_val = cb.get(key, 0)

        if cb_val >= CONSECUTIVE_DISABLE:
            if not peek:
                disabled[key] = self._disabled_record(
                    f"连续{cb_val}天评分<{SCORE_PAUSE_24H}",
                    manual=False, since=now.isoformat())
                self._db.set_json("disabled", disabled)
                cb[key] = 0
                self._db.set_json("consecutive_bad", cb)
            return {"allowed": False, "mode": "disabled", "position_mult": 0,
                    "reason": f"连续{cb_val}天低评分，禁用(冷却{DISABLE_COOLDOWN_HOURS}h后重试)"}

        if cb_val >= CONSECUTIVE_REDUCE:
            return {"allowed": True, "mode": "reduced", "position_mult": POSITION_REDUCTION,
                    "reason": f"连续{cb_val}天评分<{SCORE_PAUSE_24H}，仓位减半"}

        # ── 5. 评分阈值检查 ──
        last_score = self._db.get_json("last_score", {})
        last = last_score.get(key)

        if last is not None and last < SCORE_PAUSE_72H:
            if not peek:
                paused[key] = (now + timedelta(hours=72)).isoformat()
                self._db.set_json("paused_until", paused)
            return {"allowed": False, "mode": "paused", "position_mult": 0,
                    "reason": f"评分{last}<{SCORE_PAUSE_72H}，暂停72h"}

        if last is not None and last < SCORE_PAUSE_24H:
            if not peek:
                paused[key] = (now + timedelta(hours=24)).isoformat()
                self._db.set_json("paused_until", paused)
            return {"allowed": False, "mode": "paused", "position_mult": 0,
                    "reason": f"评分{last}<{SCORE_PAUSE_24H}，暂停24h"}

        return result

    # ═══════════════════════════════════════════════════════
    # 管理方法
    # ═══════════════════════════════════════════════════════

    @_synchronized
    def manual_enable(self, coin: str, strategy: str):
        """人工恢复禁用策略"""
        key = self._key(coin, strategy)
        disabled = self._db.get_json("disabled", {})
        disabled.pop(key, None)
        self._db.set_json("disabled", disabled)
        paused = self._db.get_json("paused_until", {})
        paused.pop(key, None)
        self._db.set_json("paused_until", paused)
        probation = self._db.get_json("probation_until", {})
        probation.pop(key, None)
        self._db.set_json("probation_until", probation)
        self._clear_probation_entry(key)
        cb = self._db.get_json("consecutive_bad", {})
        cb[key] = 0
        self._db.set_json("consecutive_bad", cb)
        history = self._db.get_json("history", [])
        history.append({"time": datetime.now(timezone.utc).isoformat(),
                        "action": "manual_enable", "coin": coin, "strategy": strategy})
        self._db.set_json("history", history[-30:])
        logger.info(f"✅ 人工恢复 {key}")

    @_synchronized
    def manual_disable(self, coin: str, strategy: str, reason: str = "人工禁用"):
        """强制禁用策略"""
        key = self._key(coin, strategy)
        now = datetime.now(timezone.utc)
        disabled = self._db.get_json("disabled", {})
        disabled[key] = self._disabled_record(reason, manual=True, since=now.isoformat())
        self._db.set_json("disabled", disabled)
        paused = self._db.get_json("paused_until", {})
        paused.pop(key, None)
        self._db.set_json("paused_until", paused)
        probation = self._db.get_json("probation_until", {})
        probation.pop(key, None)
        self._db.set_json("probation_until", probation)
        logger.info(f"🚫 人工禁用 {key} (永久): {reason}")

    @staticmethod
    def _get_coin_strategies(coin: str) -> list:
        return ALL_STRATEGIES.get(coin, [])

    def has_active(self, coin: str) -> bool:
        for strat in ALL_STRATEGIES.get(coin, []):
            if self.check(coin, strat, peek=True)["allowed"]:
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
                res = self.check(coin, strat, peek=True)  # 只读：看状态不推进状态机
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

            # 模拟暂停到期 (直接改 SQLite 偷步)
            paused = sg._db.get_json("paused_until", {})
            paused["SOL:trend"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            sg._db.set_json("paused_until", paused)
            r2 = sg.check("SOL", "trend")
            print(f"  2. 暂停到期: {r2['mode']} → {r2['reason']} (仓位×{r2['position_mult']:.0%})")

            # 模拟试运行结束+评分仍低
            sg.update_score("SOL", "trend", 30)
            probation = sg._db.get_json("probation_until", {})
            probation["SOL:trend"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            sg._db.set_json("probation_until", probation)
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
