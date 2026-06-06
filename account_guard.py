"""
全局账户熔断 — 参数回滚解决"参数错了"，这个解决"市场错了"

三级保护:
  L1 预警 (日亏>2%):      暂停新增仓位，只允许平仓/止损
  L2 保护模式 (日亏>3.5%):  所有策略收紧，开仓需确认
  L3 强停 (日亏>5%):      停止所有开仓，Telegram 强提醒

币种级保护:
  连续亏损5笔: 冷却2小时
  单币种日亏超预算40%: 暂停该币种

用法:
  from account_guard import Guard
  guard = Guard()
  guard.check()  # 每次交易前调用
  guard.add_trade(coin, pnl)  # 记录每笔交易
"""

import json, os, logging, time, threading
from datetime import datetime, timezone
from pathlib import Path

import config

ROOT = Path(__file__).parent
GUARD_FILE = str(ROOT / "guard_state.json")

logger = logging.getLogger("guard")

# 进程内全局锁：多个币种线程 + web 请求会共用同一份 guard_state.json，
# 保护「读-改-写」临界区，避免并发下日亏统计丢更新。
_GUARD_LOCK = threading.RLock()


def _synchronized(func):
    """用全局锁串行化对 guard 状态的读改写"""
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _GUARD_LOCK:
            return func(*args, **kwargs)
    return wrapper

def _safe_parse_dt(ts_str: str):
    """安全解析 ISO 时间戳，始终返回 aware UTC datetime"""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except:
        return None

def _is_cooled(cooled: str | None) -> bool:
    """检查币种是否在冷却期"""
    if not cooled:
        return False
    dt = _safe_parse_dt(cooled)
    if dt is None:
        return False
    return dt > datetime.now(timezone.utc)

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

DAILY_LOSS_WARN   = 0.02   # 日亏 2% → 预警
DAILY_LOSS_PROTECT = 0.035  # 日亏 3.5% → 保护模式
DAILY_LOSS_HALT   = 0.05   # 日亏 5% → 强停

CONSECUTIVE_LOSS_MAX = 5    # 连续亏损笔数
CONSECUTIVE_COOLDOWN_H = 2  # 冷却小时

PER_COIN_BUDGET_RATIO = 0.40  # 单币种日亏超总预算 40% → 暂停

# 各币种日预算（USDT）
COIN_DAILY_BUDGET = {
    "TRX":      5,
    "ETH":      15,
    "SOL":      10,
    "TRX_SWAP": 6,
    "ETH_SWAP": 12,
    "SOL_SWAP": 8,
}


class Guard:
    """账户守护者 — 每次交易前 call guard.check()"""

    def __init__(self, total_capital: float = None):
        # 统一引用 config.TOTAL_CAPITAL，避免与 COIN_CONFIG / 回撤口径不一致
        self.total_capital = total_capital or getattr(config, "TOTAL_CAPITAL", 500)
        self._load()

    def _load(self):
        if os.path.exists(GUARD_FILE):
            try:
                with open(GUARD_FILE) as f:
                    self.state = json.load(f)
            except Exception as e:
                # 文件损坏 → 备份并重建
                import logging as _logging
                _logging.getLogger("account_guard").warning(
                    f"guard_state.json 损坏，备份后重建: {e}")
                try:
                    backup = GUARD_FILE + ".corrupted." + str(int(time.time()))
                    os.rename(GUARD_FILE, backup)
                except:
                    pass
                self.state = self._new_day()
        else:
            self.state = self._new_day()

    def _save(self):
        # 原子写入：先写临时文件再 rename，防止中途 kill 导致文件损坏
        tmp = GUARD_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, GUARD_FILE)

    def _new_day(self) -> dict:
        return {
            "date": str(datetime.now(timezone.utc).date()),
            "total_capital": self.total_capital,
            "daily_pnl": 0.0,
            "trades": [],
            "per_coin": {},
            "coin_cooled_until": {},
            "alerts": [],
            "status": "normal",  # normal | warn | protect | halt
        }

    # ═══════════════════════════════════════════════════════
    # 核心方法
    # ═══════════════════════════════════════════════════════

    @_synchronized
    def check(self) -> dict:
        """
        每次策略循环前调用。
        返回 {'allowed': bool, 'mode': str, 'restrictions': list, 'reason': str}
        """
        today = str(datetime.now(timezone.utc).date())
        if self.state.get("date") != today:
            self.state = self._new_day()

        s = self.state
        daily_pnl = s.get("daily_pnl", 0)
        daily_pnl_pct = daily_pnl / self.total_capital

        result = {"allowed": True, "mode": "normal", "restrictions": [], "reason": "", "status": "normal"}

        # ── L3 强停 ──
        if daily_pnl_pct < -DAILY_LOSS_HALT:
            result["allowed"] = False
            result["mode"] = "halt"
            result["status"] = "halt"
            result["reason"] = f"日亏损 {abs(daily_pnl_pct):.1%} > {DAILY_LOSS_HALT:.0%}，强制停止所有开仓"
            result["restrictions"] = ["no_open", "close_only", "alert_critical"]
            s["status"] = "halt"
            s.setdefault("alerts", []).append({
                "time": datetime.now(timezone.utc).isoformat(),
                "level": "critical",
                "msg": result["reason"],
            })
            self._save()
            return result

        # ── L2 保护模式 ──
        if daily_pnl_pct < -DAILY_LOSS_PROTECT:
            result["mode"] = "protect"
            result["status"] = "protect"
            result["reason"] = f"日亏损 {abs(daily_pnl_pct):.1%} > {DAILY_LOSS_PROTECT:.0%}，进入保护模式"
            result["restrictions"] = ["reduce_position", "widen_grid", "confirm_open"]
            s["status"] = "protect"
            s.setdefault("alerts", []).append({
                "time": datetime.now(timezone.utc).isoformat(),
                "level": "warning",
                "msg": result["reason"],
            })
            self._save()
            return result

        # ── L1 预警 ──
        if daily_pnl_pct < -DAILY_LOSS_WARN:
            result["mode"] = "warn"
            result["status"] = "warn"
            result["reason"] = f"日亏损 {abs(daily_pnl_pct):.1%} > {DAILY_LOSS_WARN:.0%}，暂停新增仓位"
            result["restrictions"] = ["no_new_positions"]
            s["status"] = "warn"
            s.setdefault("alerts", []).append({
                "time": datetime.now(timezone.utc).isoformat(),
                "level": "info",
                "msg": result["reason"],
            })
            self._save()
            return result

        s["status"] = "normal"
        self._save()
        return result

    @_synchronized
    def add_trade(self, coin: str, pnl: float):
        """记录一笔交易"""
        today = str(datetime.now(timezone.utc).date())
        if self.state.get("date") != today:
            self.state = self._new_day()

        s = self.state
        s["daily_pnl"] = s.get("daily_pnl", 0) + pnl
        s.setdefault("trades", []).append({
            "coin": coin,
            "pnl": round(pnl, 4),
            "time": datetime.now(timezone.utc).isoformat(),
        })

        # 更新币种统计
        pc = s.setdefault("per_coin", {}).setdefault(coin, {
            "total_pnl": 0, "total_trades": 0, "wins": 0, "losses": 0,
            "consecutive_losses": 0, "cooled": False,
        })
        pc["total_pnl"] = round(pc["total_pnl"] + pnl, 4)
        pc["total_trades"] += 1
        if pnl > 0:
            pc["wins"] += 1
            pc["consecutive_losses"] = 0
        else:
            pc["losses"] += 1
            pc["consecutive_losses"] += 1

        self._save()

    @_synchronized
    def coin_check(self, coin: str) -> dict:
        """
        每币种交易前检查。
        返回 {'allowed': bool, 'reason': str}
        """
        today = str(datetime.now(timezone.utc).date())
        if self.state.get("date") != today:
            self.state = self._new_day()

        s = self.state
        result = {"allowed": True, "reason": ""}
        now = datetime.now(timezone.utc)

        # 检查币种冷却
        cooled_until_str = s.get("coin_cooled_until", {}).get(coin)
        if cooled_until_str:
            cooled_until = _safe_parse_dt(cooled_until_str)
            if cooled_until and now < cooled_until:
                remaining = (cooled_until - now).total_seconds() / 3600
                result["allowed"] = False
                result["reason"] = f"{coin} 冷却中，剩余 {remaining:.1f}h"
                return result

        # 检查连续亏损
        pc = s.get("per_coin", {}).get(coin, {})
        cons = pc.get("consecutive_losses", 0)
        if cons >= CONSECUTIVE_LOSS_MAX:
            cooled_until = now + __import__('datetime').timedelta(hours=CONSECUTIVE_COOLDOWN_H)
            s.setdefault("coin_cooled_until", {})[coin] = cooled_until.isoformat()
            result["allowed"] = False
            result["reason"] = f"{coin} 连续亏损 {cons} 笔，冷却 {CONSECUTIVE_COOLDOWN_H}h"
            pc["cooled"] = True
            self._save()
            return result

        # 检查币种预算
        budget = COIN_DAILY_BUDGET.get(coin, 10)
        coin_loss = abs(pc.get("total_pnl", 0)) if pc.get("total_pnl", 0) < 0 else 0
        if coin_loss > budget * PER_COIN_BUDGET_RATIO:
            result["allowed"] = False
            result["reason"] = f"{coin} 日亏 ${coin_loss:.1f} > 预算 {PER_COIN_BUDGET_RATIO:.0%}，暂停"
            return result

        return result

    @_synchronized
    def status_report(self) -> dict:
        """完整的守护者状态报告"""
        today = str(datetime.now(timezone.utc).date())
        if self.state.get("date") != today:
            self.state = self._new_day()

        s = self.state
        daily_pnl = s.get("daily_pnl", 0)
        daily_pnl_pct = daily_pnl / self.total_capital

        per_coin_status = {}
        for coin, pc in s.get("per_coin", {}).items():
            total = pc.get("total_trades", 0)
            wins = pc.get("wins", 0)
            wr = wins / total * 100 if total > 0 else 0
            cooled = s.get("coin_cooled_until", {}).get(coin)
            per_coin_status[coin] = {
                "pnl": pc.get("total_pnl", 0),
                "trades": total,
                "win_rate": round(wr, 1),
                "consecutive_losses": pc.get("consecutive_losses", 0),
                "cooled": _is_cooled(cooled),
            }

        return {
            "date": today,
            "total_capital": self.total_capital,
            "daily_pnl": round(daily_pnl, 2),
            "daily_pnl_pct": round(daily_pnl_pct * 100, 2),
            "status": s.get("status", "normal"),
            "total_trades": len(s.get("trades", [])),
            "alerts": s.get("alerts", [])[-5:],
            "per_coin": per_coin_status,
        }


# ═══════════════════════════════════════════════════════════
# CLI 测试 & 状态查看
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    guard = Guard(total_capital=500)
    report = guard.status_report()

    print("🛡️ 账户守护者状态")
    print("=" * 50)
    print(f"  日期: {report['date']}")
    print(f"  总本金: ${report['total_capital']}")
    print(f"  日PnL: ${report['daily_pnl']:.2f} ({report['daily_pnl_pct']:+.1f}%)")
    print(f"  状态: {report['status']}")
    print(f"  交易数: {report['total_trades']}")
    print(f"  告警: {len(report['alerts'])} 条")

    print(f"\n📊 币种明细:")
    for coin, stats in report["per_coin"].items():
        status = "🧊 冷却" if stats["cooled"] else "✅ 正常"
        print(f"  {coin:12s} PnL: ${stats['pnl']:+.2f}  胜率: {stats['win_rate']:.0f}%  "
              f"连亏: {stats['consecutive_losses']}  {status}")

    # 测试熔断
    print(f"\n⚡ 熔断检测:")
    result = guard.check()
    print(f"  当前: {result['mode']} {'✅' if result['allowed'] else '❌'}")
    if result["restrictions"]:
        print(f"  限制: {result['restrictions']}")
