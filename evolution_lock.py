"""
进化锁 — 唯一入口，所有参数修改必须过它。

三道门:
  1. can_evolve()     — 决策前判断能不能动
  2. safe_write_config() — 写入前第二道拦截
  3. 统一日志格式      — 每次拦截/通过都留痕

极端行情下:
  ✅ 允许: rollback, stop_loss, reduce_position, cooldown, pause, guard, alert
  ❌ 禁止: explore, backtest_promote, write_config, increase_position,
           remove_blacklist, enable_paused, full_recovery, expand_grid

用法:
  from evolution_lock import can_evolve, safe_write_config

  ok, reason = can_evolve("brain.py auto-tune")
  if not ok:
      log(f"Evolution locked: {reason}")
      check_rollback()
      return

  safe_write_config(coin, param, value, source="brain.py")  # 不调用这个就写不进去
"""

import json, os, logging
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
LOCK_FILE = str(ROOT / "evolution_lock.json")
MANUAL_LOCK_FILE = str(ROOT / ".evolution_manual_lock")

logger = logging.getLogger("evolution_lock")

# ═══════════════════════════════════════════════════════════
# 手动锁
# ═══════════════════════════════════════════════════════════

def set_manual_lock(reason: str = "人工锁定"):
    """手动锁定进化 — 创建锁文件"""
    with open(MANUAL_LOCK_FILE, "w") as f:
        f.write(f"locked_at={datetime.now(timezone.utc).isoformat()}\nreason={reason}\n")
    logger.warning(f"🔒 手动锁已启用: {reason}")

def remove_manual_lock():
    """解除手动锁"""
    if os.path.exists(MANUAL_LOCK_FILE):
        os.remove(MANUAL_LOCK_FILE)
        logger.info("🔓 手动锁已解除")

def manual_lock_exists() -> bool:
    return os.path.exists(MANUAL_LOCK_FILE)

def manual_lock_reason() -> str:
    if not os.path.exists(MANUAL_LOCK_FILE):
        return ""
    with open(MANUAL_LOCK_FILE) as f:
        for line in f:
            if line.startswith("reason="):
                return line.split("=", 1)[1].strip()
    return "未知"

# ═══════════════════════════════════════════════════════════
# 封锁日志
# ═══════════════════════════════════════════════════════════

def _ts_from_iso(ts_str: str) -> float:
    """安全解析 ISO 时间戳为 Unix timestamp"""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except:
        return 0


def _load_lock_log():
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE) as f:
            return json.load(f)
    return {"blocks": [], "writes": [], "state": "normal"}

def _save_lock_log(data):
    tmp = LOCK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, LOCK_FILE)

def _log_block(entry: str, reason: str, trigger: str, allowed_actions: list, detail: dict = None):
    """统一封锁日志格式"""
    data = _load_lock_log()
    record = {
        "event": "EVOLUTION_BLOCKED",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "trigger": trigger,
        "entry": entry,
        "allowed": allowed_actions,
        "config_write": False,
        "detail": detail or {},
    }
    data["blocks"].append(record)
    # 只保留最近 100 条
    if len(data["blocks"]) > 100:
        data["blocks"] = data["blocks"][-100:]
    data["state"] = "locked" if reason != "ok" else "normal"
    _save_lock_log(data)

    # 同时输出标准日志
    reason_short = reason.replace("extreme_", "极端").replace("account_", "账户").replace("manual_", "人工")
    msg = (f"EVOLUTION_BLOCKED reason={reason} entry={entry} trigger={trigger} "
           f"allowed={','.join(allowed_actions[:3])} config_write=false")
    logger.warning(f"🚫 {msg}")

def _log_write(entry: str, coin: str, param: str, value: str, success: bool, detail: dict = None):
    data = _load_lock_log()
    data["writes"].append({
        "event": "CONFIG_WRITE" if success else "CONFIG_WRITE_BLOCKED",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry": entry,
        "coin": coin,
        "param": param,
        "value": str(value),
        "success": success,
        "detail": detail or {},
    })
    if len(data["writes"]) > 100:
        data["writes"] = data["writes"][-100:]
    _save_lock_log(data)

# ═══════════════════════════════════════════════════════════
# ① 第一道门: can_evolve()
# ═══════════════════════════════════════════════════════════

def can_evolve(entry: str = "unknown") -> tuple:
    """
    所有参数修改入口必须先调用。
    返回 (allowed: bool, reason: str)

    entry: 调用方标识，如 "brain.py auto-tune" / "ai_tuner.py --apply-safe"
    """
    # ── 手动锁 ──
    if manual_lock_exists():
        reason = f"manual_lock: {manual_lock_reason()}"
        _log_block(entry, "manual_lock", "manual_lock_file", ["rollback", "stop_loss", "alert"])
        return False, reason

    # ── 极端行情 ──
    try:
        from param_score import is_extreme_market
        extreme = is_extreme_market()
        if extreme.get("is_extreme"):
            triggers = "; ".join(extreme.get("reasons", ["unknown"]))
            _log_block(entry, "extreme_market", triggers,
                       ["rollback", "stop_loss", "reduce_position", "cooldown", "pause", "alert"])
            return False, "extreme_market"
    except ImportError:
        pass

    # ── 账户防护 ──
    try:
        from account_guard import Guard
        g = Guard()
        result = g.check()
        status = result.get("status", "normal")
        if status in ("protect", "halt"):
            # L2/L3 — 禁止进化
            _log_block(entry, f"account_guard_{status}",
                       f"account status: {status}",
                       ["rollback", "stop_loss", "reduce_position", "alert"])
            return False, f"account_guard_{status}"
    except ImportError:
        pass

    return True, "ok"


# ═══════════════════════════════════════════════════════════
# ② 第二道门: safe_write_config()
# ═══════════════════════════════════════════════════════════

def safe_write_config(coin: str, param: str, value, source: str = "unknown",
                      old_value=None) -> bool:
    """
    唯一的写入 config 入口。

    检查链:
      1. 手动锁
      2. Rollback → 快照校验（可绕过极端行情/账户锁）
      3. 全局锁 (can_evolve: 极端行情/账户 L2-L3)
      4. 参数黑名单
      5. 策略暂停
      6. TRX 保护

    返回 True 表示写入成功
    """
    import config

    is_rollback = "rollback" in source.lower()

    # ── 手动锁（最高优先级） ──
    if manual_lock_exists():
        if is_rollback:
            # 手动锁下回滚仍然允许，但要记录
            logger.warning(f"⚠️ 手动锁下执行回滚: {coin}.{param}={value}")
        else:
            logger.warning(f"🚫 safe_write_config 拦截: 手动锁 (source={source})")
            _log_write(source, coin, param, value, False, {"block_reason": "manual_lock"})
            return False

    # ── Rollback 快照校验 ──
    if is_rollback:
        is_valid, snap_detail = _validate_rollback_snapshot_full(coin, param, value)
        if not is_valid:
            logger.warning(f"🚫 safe_write_config rollback 快照校验失败: {snap_detail}")
            _log_write(source, coin, param, value, False, {
                "block_reason": "rollback_snapshot_mismatch",
                "detail": snap_detail,
            })
            return False
        # 回滚快照通过 → 直接写入，跳过全局锁
        logger.info(f"✅ rollback 快照校验通过: {snap_detail}")
        _do_write(config, coin, param, value, source, old_value,
                   extra_detail=snap_detail)
        return True

    # ── 全局锁（非回滚必须过） ──
    ok, reason = can_evolve(entry=source)
    if not ok:
        logger.warning(f"🚫 safe_write_config 拦截: {reason} (source={source})")
        _log_write(source, coin, param, value, False, {"block_reason": reason})
        return False

    # ── 参数黑名单 ──
    try:
        from param_score import is_on_cooldown
        cooldown = is_on_cooldown(coin, param, value)
        if cooldown.get("on_cooldown") and not cooldown.get("can_override"):
            logger.warning(f"🚫 safe_write_config 拦截: {cooldown['reason']}")
            _log_write(source, coin, param, value, False, {"block_reason": "cooldown", "detail": cooldown})
            return False
    except ImportError:
        pass

    # ── 策略暂停检查 ──
    try:
        from strategy_guard import StrategyGuard
        sg = StrategyGuard()
        from strategy_guard import ALL_STRATEGIES
        strats = ALL_STRATEGIES.get(coin, [])
        all_paused = True
        for sname in strats:
            r = sg.check(coin, sname)
            if r["allowed"]:
                all_paused = False
                break
        if all_paused and strats:
            logger.warning(f"🚫 safe_write_config 拦截: {coin} 所有策略已暂停/禁用")
            _log_write(source, coin, param, value, False, {"block_reason": "all_strategies_paused"})
            return False
    except ImportError:
        pass

    # ── TRX 参数保护 ──
    if coin in ("TRX", "TRX_SWAP"):
        logger.warning(f"🚫 safe_write_config 拦截: {coin} 参数不可自动修改 (用户锁定)")
        _log_write(source, coin, param, value, False, {"block_reason": "trx_protected"})
        return False

    # ── 实际写入 ──
    return _do_write(config, coin, param, value, source, old_value)


def _do_write(config, coin, param, value, source, old_value=None, extra_detail=None) -> bool:
    """实际执行写入操作"""
    try:
        setattr(config, param, value)
        _update_config_file(param, value, old_value)
        logger.info(f"✅ safe_write_config: {coin}.{param} = {value} (source={source})")
        _log_write(source, coin, param, value, True, extra_detail)
        return True
    except Exception as e:
        logger.error(f"❌ safe_write_config 写入失败: {coin}.{param} = {value}: {e}")
        _log_write(source, coin, param, value, False, {"error": str(e)})
        return False


# ═══════════════════════════════════════════════════════════
# Rollback 快照校验
# ═══════════════════════════════════════════════════════════

def _validate_rollback_snapshot_full(coin: str, param: str, value) -> tuple:
    """
    验证回滚写入是否匹配历史快照。

    规则:
      - 必须在 brain_state.json 的 rollback_queue 中找到匹配快照
      - coin 必须匹配
      - param 必须匹配（支持短名 grid_count 和全名 ETH_GRID_COUNT 两种格式）
      - new_value 必须等于 snapshot 中的 old_value（不能写任意值）
      - 已评估的快照也允许匹配（真实流程中先写后标记评估）

    返回 (is_valid, detail_dict)
    """
    brain_state_path = ROOT / "brain_state.json"
    if not brain_state_path.exists():
        return False, {"error": "brain_state.json 不存在", "coin": coin, "param": param}

    try:
        with open(brain_state_path) as f:
            brain_state = json.load(f)
    except Exception as e:
        return False, {"error": f"brain_state.json 读取失败: {e}", "coin": coin, "param": param}

    queue = brain_state.get("rollback_queue", [])
    if not queue:
        return False, {"error": "rollback_queue 为空", "coin": coin, "param": param}

    # 找匹配的快照 — param 可能用短名或全名
    for entry in queue:
        e_coin = entry.get("coin", "")
        e_param = entry.get("param", "")
        if e_coin != coin:
            continue
        # 尝试多种匹配方式
        if not _param_matches(e_param, param, coin):
            continue

        expected_old = entry.get("old_value")
        if expected_old is not None and float(value) == float(expected_old):
            return True, {
                "snapshot_matched": True,
                "snapshot_id": str(entry.get("applied_at", "unknown"))[:19],
                "coin": coin,
                "param": e_param,
                "revert_to": expected_old,
                "new_value_was": entry.get("new_value"),
                "evaluated": entry.get("evaluated", False),
                "source": entry.get("source", "unknown"),
            }

    # 没匹配到 — 列出可用的候选供排查
    candidates = []
    for entry in queue:
        e_coin = entry.get("coin", "")
        if e_coin == coin:
            candidates.append(f"{e_coin}.{entry.get('param')}: {entry.get('old_value')}←{entry.get('new_value')}")
    return False, {
        "error": "未找到匹配的快照",
        "coin": coin,
        "param": param,
        "actual_value": value,
        "available_snapshots": candidates[:5],
    }


def _param_matches(e_param: str, target_param: str, coin: str) -> bool:
    """判断两个参数名是否指向同一个配置项。
    支持短名(grid_count) ↔ 全名(ETH_GRID_COUNT) 互转。
    """
    if e_param == target_param:
        return True
    # 全名 ↔ 短名：ETH_GRID_COUNT → grid_count
    suffix = target_param.lower().replace(coin.lower() + "_", "").lstrip("_")
    if suffix == e_param.lower():
        return True
    prefix = e_param.lower().replace(coin.lower() + "_", "").lstrip("_")
    if prefix == target_param.lower():
        return True
    return False


def _update_config_file(param: str, value, old_value=None):
    """实际修改 config.py 文件内容"""
    config_path = str(ROOT / "config.py")

    with open(config_path) as f:
        content = f.read()

    import re
    # 匹配 config.py 中的变量赋值
    pattern = re.compile(rf"^({param}\s*=\s*)(.+?)(\s*#.*)?$", re.MULTILINE)

    new_val = str(value) if not isinstance(value, str) else f'"{value}"'
    if isinstance(value, float):
        new_val = str(value)
    elif isinstance(value, int):
        new_val = str(value)

    if pattern.search(content):
        new_content = pattern.sub(rf"\g<1>{new_val}\g<3>", content)
    else:
        # 变量不存在，追加
        if old_value is not None:
            comment = f"  # was {old_value}"
        else:
            comment = ""
        new_content = content.rstrip() + f"\n{param} = {new_val}{comment}\n"

    # 原子写入
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(new_content)
    os.replace(tmp_path, config_path)

    # 注意：不在此处 importlib.reload(config)。
    # 跨进程（brain CLI）reload 对运行中的 bot 无效；同进程 reload 又会与
    # 交易线程并发读取产生竞态。改动在 bot 下次重启时生效（调用方已 setattr 更新内存）。
    logger.info(f"  📝 config.py: {param} = {new_val}（重启后生效）")


# ═══════════════════════════════════════════════════════════
# ③ 统一入口函数 — 所有进化入口的标准模板
# ═══════════════════════════════════════════════════════════

def evolve_or_defend(entry: str = "unknown", auto_apply: bool = True) -> dict:
    """
    标准进化入口模板。

    使用方式 (替换所有现有入口):

      result = evolve_or_defend("brain.py auto-tune")
      if result["locked"]:
          return result  # 只做了防御动作

      # 正常进化逻辑...
      safe_write_config(...)

    返回:
      {locked, reason, actions_taken, blocked_actions}
    """
    ok, reason = can_evolve(entry)

    if not ok:
        # 只做防守
        try:
            from brain import check_rollback
            check_rollback(apply_revert=auto_apply)
        except Exception as e:
            logger.error(f"check_rollback failed: {e}")

        return {
            "locked": True,
            "reason": reason,
            "actions_taken": ["check_rollback"],
            "blocked_actions": [
                "explore", "backtest_promote", "write_config",
                "increase_position", "remove_blacklist",
                "enable_paused_strategy", "full_recovery",
            ],
        }

    return {"locked": False, "reason": "ok"}


# ═══════════════════════════════════════════════════════════
# ④ 审计命令
# ═══════════════════════════════════════════════════════════

def audit(hours: int = 24) -> str:
    """
    审计最近 N 小时内所有参数写入尝试。

    输出格式:
      ✅ rollback    ETH.grid_count   6→2  snapshot_id: abc
      ❌ blocked     ETH.grid_count   2→100  extreme_market  entry: ai_tuner.py
      ✅ allowed     SOL.grid_range   0.10→0.12  normal  entry: brain.py
    """
    data = _load_lock_log()
    writes = data.get("writes", [])
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - (hours * 3600)

    recent = []
    for w in writes:
        try:
            ts = datetime.fromisoformat(w["timestamp"].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except:
            ts = datetime.fromisoformat(w["timestamp"][:19]).replace(tzinfo=timezone.utc)
        if ts.timestamp() >= cutoff:
            recent.append(w)

    lines = []
    lines.append(f"\n{'='*70}")
    lines.append(f"📋 参数写入审计 — 最近 {hours}h")
    lines.append(f"{'='*70}")

    if not recent:
        lines.append("   (无写入记录)")
        return "\n".join(lines)

    for w in recent:
        ts = w["timestamp"][:19]
        coin = w.get("coin", "?")
        param = w.get("param", "?")
        val = w.get("value", "?")
        success = w.get("success", False)
        entry = w.get("entry", "?")
        detail = w.get("detail", {})

        if success:
            if "rollback" in entry.lower():
                snap_id = detail.get("snapshot_id", "?")
                if isinstance(snap_id, str) and len(snap_id) > 19:
                    snap_id = snap_id[:19]
                lines.append(f"  ✅ rollback  {coin}.{param:25s} → {val}  snapshot: {snap_id}")
            else:
                lines.append(f"  ✅ allowed   {coin}.{param:25s} = {val}  entry: {entry}")
        else:
            reason = detail.get("block_reason", "unknown")
            lines.append(f"  ❌ blocked   {coin}.{param:25s} → {val}  {reason:25s} entry: {entry}")

    lines.append(f"\n  总计: {len(recent)} 次写入尝试")
    lines.append(f"    ✅ 成功: {sum(1 for w in recent if w['success'])}")
    lines.append(f"    ❌ 拦截: {sum(1 for w in recent if not w['success'])}")

    return "\n".join(lines)

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "lock":
            reason = sys.argv[2] if len(sys.argv) > 2 else "人工锁定"
            set_manual_lock(reason)
            print(f"🔒 手动锁已启用: {reason}")

        elif cmd == "unlock":
            remove_manual_lock()
            print("🔓 手动锁已解除")

        elif cmd == "check":
            ok, reason = can_evolve("manual check")
            if ok:
                print("🟢 进化允许 — 所有条件通过")
            else:
                print(f"🔴 进化锁定 — {reason}")
                data = _load_lock_log()
                if data["blocks"]:
                    last = data["blocks"][-1]
                    print(f"   触发: {last['trigger']}")
                    print(f"   入口: {last['entry']}")
                    print(f"   允许: {', '.join(last['allowed'])}")

        elif cmd == "log":
            data = _load_lock_log()
            blocks = data.get("blocks", [])
            writes = data.get("writes", [])
            print(f"📋 进化锁日志: {len(blocks)} 次拦截, {len(writes)} 次写入")
            if blocks:
                print(f"\n最近拦截 (EVOLUTION_BLOCKED):")
                for b in blocks[-5:]:
                    print(f"  {b['timestamp'][:19]}  {b['reason']:25s}  entry={b['entry']}")
            if writes:
                print(f"\n最近写入尝试:")
                for w in writes[-5:]:
                    status = "✅" if w['success'] else "❌"
                    reason = w.get("detail", {}).get("block_reason", "")
                    suff = f" — {reason}" if not w['success'] and reason else ""
                    print(f"  {status} {w['timestamp'][:19]}  {w['coin']}.{w['param']}={w['value']}  from={w['entry']}{suff}")

        elif cmd == "audit":
            # python evolution_lock.py audit [hours] [--json]
            json_mode = "--json" in sys.argv
            hours = 24
            for a in sys.argv[2:]:
                if a != "--json":
                    try:
                        hours = int(a)
                    except:
                        pass
            if json_mode:
                import json as _json
                data = _load_lock_log()
                writes = data.get("writes", [])
                now = datetime.now(timezone.utc)
                cutoff = now.timestamp() - (hours * 3600)
                recent = [w for w in writes if _ts_from_iso(w.get("timestamp", "")) >= cutoff]
                rollback_ok = sum(1 for w in recent if w["success"] and "rollback" in w.get("entry", "").lower())
                non_rollback_ok = sum(1 for w in recent if w["success"] and "rollback" not in w.get("entry", "").lower())
                blocked = sum(1 for w in recent if not w["success"])
                # 经 safe_write_config 批准的 brain.py 写入是预期行为，不算异常
                # 真正异常 = 成功写入但来源不是已知的授权入口（brain.py, ai_tuner.py 等）
                AUTHORIZED_SOURCES = ("brain.py", "ai_tuner", "rollback")
                anomalies = [w for w in recent if w["success"]
                             and not any(src in w.get("entry", "").lower() for src in AUTHORIZED_SOURCES)]
                # 状态：safe=无写入或仅rollback；warning=有拦截(锁在工作)；info=授权写入；alert=未授权写入
                if anomalies:
                    status = "alert"
                elif blocked > 0:
                    status = "warning"
                elif non_rollback_ok > 0:
                    status = "info"
                else:
                    status = "safe"
                print(_json.dumps({
                    "total_writes": len(recent),
                    "rollback_success": rollback_ok,
                    "blocked": blocked,
                    "non_rollback_success": non_rollback_ok,
                    "anomalies": len(anomalies),
                    "status": status,
                    "anomaly_details": [{"coin": w["coin"], "param": w["param"], "value": w["value"], "entry": w["entry"]} for w in anomalies],
                }, indent=2, ensure_ascii=False))
            else:
                print(audit(hours=hours))

    else:
        # 默认: 检查状态
        ok, reason = can_evolve("cli_status")
        status_icon = "🟢" if ok else "🔴"
        manual = "🔒 手动锁: 是" if manual_lock_exists() else "🔓 手动锁: 否"
        print(f"🔐 进化锁状态: {status_icon} {'允许' if ok else '锁定'} ({reason})")
        print(f"   {manual}")
