#!/usr/bin/env python3
"""
看门狗 — 外部死人开关

关键设计：bot自己的 TG 告警在进程死掉时发不出来。
所以这里用外部 cron 每分钟检查，独立于 bot 进程。

检查链:
  1. .bot_heartbeat 文件年龄 → 过期 = bot 假死/崩了
  2. PID 文件是否存在 + 进程是否存活
  3. bot_state.db 是否在增长（>30min 没新交易 = 可能卡住了但不一定崩）

告警:
  - Telegram 推送（从 notify.py 复用的 send_tg）
  - 可选: 自动重启 systemd 服务

用法:
  python watchdog.py [--restart] [--no-tg] [--max-age 120]
  建议: crontab 每分钟一次
"""

import os
import sys
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [watchdog] %(levelname)s: %(message)s")
logger = logging.getLogger("watchdog")

ROOT = Path(__file__).parent
HEARTBEAT_FILE = ROOT / ".bot_heartbeat"
PID_FILE = ROOT / ".bot.pid"  # PID 文件（bot 启动时写入）
DB_FILE = ROOT / "bot_state.db"
LOCK_FILE = ROOT / ".bot.lock"  # 单实例锁
# ── Healthchecks.io ──
HC_UUID = os.environ.get("HEALTHCHECKS_IO_UUID", "")

# 限频：同类型告警 30 分钟内只发一次（文件级去重）
ALERT_COOLDOWN = 1800
_ALERT_FILE = ROOT / ".watchdog_alerts.json"


def _load_alert_state():
    import json
    if _ALERT_FILE.exists():
        try:
            return json.loads(_ALERT_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_alert_state(state):
    import json
    _ALERT_FILE.write_text(json.dumps(state))


def can_alert(kind: str) -> bool:
    s = _load_alert_state()
    last = s.get(kind, 0)
    if time.time() - last < ALERT_COOLDOWN:
        return False
    s[kind] = time.time()
    _save_alert_state(s)
    return True


def send_tg(msg: str):
    """复用 notify.py 的 Telegram 通知"""
    import json
    try:
        import httpx
        import config
        token = getattr(config, "TG_BOT_TOKEN", "") or os.environ.get("TG_BOT_TOKEN", "")
        chat_id = getattr(config, "TG_CHAT_ID", "") or os.environ.get("TG_CHAT_ID", "")
        if not token or not chat_id:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        httpx.post(url, json={"chat_id": chat_id, "text": msg}, timeout=10)
    except Exception as e:
        logger.warning(f"TG 通知失败: {e}")


def ping_healthchecks():
    """向 healthchecks.io 发心跳。不成功也不影响主逻辑。"""
    if not HC_UUID:
        return
    try:
        import httpx
        httpx.get(f"https://hc-ping.com/{HC_UUID}", timeout=5)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="TRX Bot 看门狗")
    parser.add_argument("--restart", action="store_true", help="心跳过期时自动重启 systemd 服务")
    parser.add_argument("--no-tg", action="store_true", help="不发 Telegram")
    parser.add_argument("--max-age", type=int, default=120, help="心跳文件最大年龄（秒），默认120")
    args = parser.parse_args()

    issues = []

    # ── 1. 心跳检查 ──
    if HEARTBEAT_FILE.exists():
        age = time.time() - HEARTBEAT_FILE.stat().st_mtime
        if age > args.max_age:
            issues.append(f"心跳过期 {age:.0f}s")
            if can_alert("heartbeat"):
                send_tg(f"🔴 看门狗: bot 心跳过期 {age:.0f}s — 可能已停止")
        elif age > 60:
            if can_alert("heartbeat_slow"):
                send_tg(f"🟡 看门狗: bot 心跳延迟 {age:.0f}s — 可能卡顿")
    else:
        issues.append("无心跳文件")
        if can_alert("no_heartbeat"):
            send_tg("🔴 看门狗: .bot_heartbeat 不存在 — bot 可能从未启动")

    # ── 2. 进程检查 ──
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            try:
                os.kill(pid, 0)  # 发信号 0 检查进程是否存在
            except OSError:
                issues.append(f"PID {pid} 已死")
                if can_alert("pid_dead"):
                    send_tg(f"🔴 看门狗: bot PID {pid} 进程已死")
        except (ValueError, OSError):
            pass
    elif LOCK_FILE.exists():
        pass  # 有锁文件说明有进程在运行（w/o PID file）
    else:
        if not HEARTBEAT_FILE.exists():
            issues.append("无锁文件 — bot 从未启动")

    # ── 3. 数据库活跃度 ──
    if DB_FILE.exists():
        db_age = time.time() - DB_FILE.stat().st_mtime
        if db_age > 1800:  # 30 分钟
            issues.append(f"DB 无新写入 {db_age/60:.0f}min")
            if can_alert("db_stale"):
                send_tg(f"🟡 看门狗: bot_state.db {db_age/60:.0f}min 无新写入")
    else:
        issues.append("无 bot_state.db")

    # ── 报告 ──
    if issues:
        msg = "⚠️ 看门狗检测到问题: " + "; ".join(issues)
        logger.warning(msg)
        if issues and args.restart:
            logger.info("尝试重启 trx-bot-dashboard 服务...")
            os.system("systemctl restart trx-bot-dashboard 2>/dev/null")
            if can_alert("restart"):
                send_tg(f"🔄 看门狗: 自动重启 | 原因: {'; '.join(issues)}")
    else:
        logger.info("一切正常 ✅")

    # ── 4. 外部死人开关: ping healthchecks.io ──
    # 如果这条 ping 停了，healthchecks.io 会通知你（外部服务，bot死了也能通知）
    ping_healthchecks()


if __name__ == "__main__":
    main()
