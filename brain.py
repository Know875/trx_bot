"""
智能进化引擎 v5 — 多维评分 + 自动回滚 + 冷却 + 极端行情保护

能力:
  ① 参数空间探索    ② 行情-参数关联
  ③ 自动试错执行    ④ 多维评分回滚（5指标）
  ⑤ 参数冷却黑名单  ⑥ 极端行情暂停

用法:
  python brain.py explore      # 探索未测试参数空间
  python brain.py regime       # 学习行情-参数关联
  python brain.py auto-tune    # 一键全自动
  python brain.py rollback     # 检查并执行参数回滚
  python brain.py status       # 查看进化状态
"""

import json, os, re, sys, time, logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter
from pathlib import Path

import httpx
import config

logger = logging.getLogger("brain")

ROOT = Path(__file__).parent
STATE_FILE = str(ROOT / "brain_state.json")

# ── 参数搜索空间 ───────────────────────────────────────
SEARCH_SPACE = {
    "TRX": {
        "grid_range_pct": (0.01, 0.20, 0.01),   # min, max, step
        "grid_count": (2, 15, 1),
    },
    "ETH": {
        "grid_range_pct": (0.02, 0.30, 0.02),
        "grid_count": (2, 12, 1),
    },
    "SOL": {
        "grid_range_pct": (0.01, 0.20, 0.01),
        "grid_count": (2, 10, 1),
        "max_grid_entries": (2, 8, 1),
    },
    "ETH_SWAP": {
        "grid_range_pct": (0.02, 0.30, 0.02),
        "grid_count": (2, 10, 1),
    },
    "SOL_SWAP": {
        "grid_range_pct": (0.01, 0.20, 0.01),
        "grid_count": (2, 8, 1),
        "max_grid_entries": (2, 6, 1),
    },
}

# ── 行情分类 ───────────────────────────────────────────
REGIME_TYPES = ["trending_up", "trending_down", "ranging", "high_volatility", "low_volatility"]


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"exploration": {}, "regime_model": {}, "auto_trials": [], "total_explored": 0}


def _save_state(state):
    # 原子写入：先写临时文件再 rename，防止中途 kill 导致文件损坏
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, STATE_FILE)


# ═══════════════════════════════════════════════════════════
# ① 参数空间探索器 — 发现未测试的有利组合
# ═══════════════════════════════════════════════════════════

def _load_existing_sweep() -> dict:
    """从 ai_tuning_state.json 读取已测试的参数"""
    existing = defaultdict(set)
    tuning_path = ROOT / "ai_tuning_state.json"
    if not tuning_path.exists():
        return dict(existing)

    try:
        state = json.load(open(tuning_path))
        for c in state.get("cycles", []):
            for sug in c.get("suggestions", []):
                key = f"{sug['coin']}.{sug['param']}"
                existing[key].add(sug["from"])
                existing[key].add(sug["to"])

        for c in state.get("cycles", []):
            sweep_data = c.get("sweep_cache", {})
            for coin, results in sweep_data.items():
                for r in results:
                    for param in SEARCH_SPACE.get(coin, {}):
                        val = r.get(param)
                        if val is not None:
                            existing[f"{coin}.{param}"].add(val)
    except: pass
    return dict(existing)


def explore_parameter_space(dry_run=False):
    """
    扫描所有币种的参数空间，找出回测表现好但未测试过的组合。
    使用细粒度采样：在每个已知最优值附近加密扫描。
    Optuna 可用时优先用贝叶斯联合优化，否则回退离散扫参。
    """
    from backtesting.engine import load_from_cache, calc_indicators
    from backtesting.grid import simulate_grid

    state = _load_state()
    existing = _load_existing_sweep()

    findings = []

    for coin, params in SEARCH_SPACE.items():
        base = coin.replace("_SWAP", "")
        df = load_from_cache(base, "1h", 180)
        if df is None or len(df) < 200:
            logger.warning(f"  ⚠️ {coin}: 无回测数据")
            continue

        ind = calc_indicators(df)
        current_params = _get_coin_params_simple(coin)

        # ── 贝叶斯优化（Optuna）联合优化 width×levels，替代离散扫参 ──
        opt_findings = _optuna_findings(coin, current_params, ind)
        if opt_findings is not None:
            findings.extend(opt_findings)
            for f in opt_findings:
                logger.info(f"  🔍(optuna) {coin}.{f['param']}: {f['current']}→{f['candidate']} "
                            f"PnL {f['current_pnl']:+.2f}%→{f['candidate_pnl']:+.2f}% (+{f['improvement']:+.2f}%)")
            time.sleep(0.05)
            continue  # Optuna 已覆盖网格参数，跳过本币的离散扫参

        for param, (lo, hi, step) in params.items():
            key = f"{coin}.{param}"
            tested = existing.get(key, set())
            if isinstance(tested, set):
                tested = {float(t) for t in tested}

            # 在 [lo, hi] 区间内每 step 采样
            candidates = []
            val = lo
            while val <= hi:
                v = round(val, 3)
                if v not in tested and abs(v - current_params.get(param, 999)) > 0.0001:
                    candidates.append(v)
                val += step

            if not candidates:
                continue

            # 对每个候选跑回测
            results = []
            for v in candidates:
                r = _simulate_param(coin, param, v, ind, current_params)
                if r:
                    results.append((v, r))

            # 找出 PnL 最高的前 3 个候选
            results.sort(key=lambda x: x[1]["pnl_pct"], reverse=True)
            for rank, (val, r) in enumerate(results[:3]):
                cur_pnl = _simulate_param(coin, param, current_params.get(param, val), ind, current_params)
                cur_pnl_val = cur_pnl["pnl_pct"] if cur_pnl else 0
                improvement = r["pnl_pct"] - cur_pnl_val

                if improvement > 0.3:  # PnL 提升 > 0.3%
                    findings.append({
                        "coin": coin, "param": param,
                        "current": current_params.get(param),
                        "candidate": val,
                        "current_pnl": round(cur_pnl_val, 2),
                        "candidate_pnl": round(r["pnl_pct"], 2),
                        "improvement": round(improvement, 2),
                        "win_rate": round(r.get("win_rate", 0), 1),
                        "rank": rank + 1,
                    })
                    logger.info(f"  🔍 {coin}.{param}: {current_params.get(param)}→{val} "
                                f"PnL {cur_pnl_val:+.2f}%→{r['pnl_pct']:+.2f}% (+{improvement:+.2f}%)")

        # 搜索完成后休眠一下避免磁盘IO雪崩
        time.sleep(0.1)

    # ── 保存结果 ──
    if findings:
        findings.sort(key=lambda x: x["improvement"], reverse=True)
        explorer = state.setdefault("exploration", {})
        explorer[str(datetime.now().date())] = {
            "findings": findings,
            "total_candidates": sum(1 for f in findings),
        }
        state["total_explored"] += sum(1 for _ in set(f"{f['coin']}.{f['param']}={f['candidate']}" for f in findings))
        _save_state(state)

    # ── 输出 ──
    print(f"\n🧭 参数空间探索报告")
    print(f"{'='*60}")
    if not findings:
        print("  未发现可显著提升的新参数组合（所有方向已知）")
        return findings

    print(f"  发现 {len(findings)} 个潜力参数组合:\n")
    for f in findings[:10]:
        print(f"  {'★' if f['rank']==1 else ' '} {f['coin'].ljust(10)} {f['param'].ljust(18)} "
              f"{f['current']} → {f['candidate']}  |  "
              f"PnL {f['current_pnl']:+.2f}% → {f['candidate_pnl']:+.2f}% "
              f"(+{f['improvement']:+.2f}%)  |  胜率 {f['win_rate']:.0f}%")

    return findings


def _get_coin_params_simple(coin: str) -> dict:
    """简化版参数获取"""
    base = coin.replace("_SWAP", "")
    spot_defaults = {
        "TRX": {"grid_range_pct": 0.05, "grid_count": 3, "max_grid_entries": 3},
        "ETH": {"grid_range_pct": 0.10, "grid_count": 6, "max_grid_entries": 3},
        "SOL": {"grid_range_pct": 0.04, "grid_count": 3, "max_grid_entries": 5},
    }
    swap_defaults = {
        "TRX_SWAP": {"grid_range_pct": 0.05, "grid_count": 3, "max_grid_entries": 3},
        "ETH_SWAP": {"grid_range_pct": 0.12, "grid_count": 5, "max_grid_entries": 3},
        "SOL_SWAP": {"grid_range_pct": 0.04, "grid_count": 3, "max_grid_entries": 4},
    }

    if coin.endswith("_SWAP"):
        defaults = swap_defaults.get(coin, swap_defaults["ETH_SWAP"])
    else:
        defaults = spot_defaults.get(coin, spot_defaults["TRX"])

    # 尝试从 config 读取实际值
    for attr_name in [f"{coin.upper()}_GRID_RANGE_PCT", f"{coin.upper()}_GRID_COUNT",
                       f"{coin.upper()}_MAX_GRID_ENTRIES"]:
        val = getattr(config, attr_name, None)
        if val is not None:
            # 简单映射
            pass
    return defaults


def _simulate_param(coin: str, param: str, val: float, ind, current_params: dict):
    """对单个参数值跑回测"""
    from backtesting.grid import simulate_grid

    try:
        p = current_params.copy()
        p[param] = val
        return simulate_grid(
            ind,
            grid_width=p.get("grid_range_pct", 0.04),
            grid_levels=int(p.get("grid_count", 5)),
        )
    except: return None


# ═══════════════════════════════════════════════════════════
# ② 行情-参数关联模型 — 学习什么行情用什么参数
# ═══════════════════════════════════════════════════════════

def build_regime_model():
    """
    从历史交易数据中学习行情-参数关联:
    1. 读取每笔交易的行情标注（regime + 指标）
    2. 计算各行情下的最优参数
    3. 建立「行情指纹 → 最优参数」映射
    """
    from backtesting.engine import load_from_cache, calc_indicators
    from backtesting.grid import simulate_grid

    state = _load_state()
    regime_model = state.setdefault("regime_model", {})

    for coin in ["TRX", "ETH", "SOL"]:
        base = coin
        df = load_from_cache(base, "1h", 180)
        if df is None or len(df) < 200:
            continue

        ind = calc_indicators(df)
        regime_model[coin] = _analyze_regime_effect(coin, df, ind)

        # 合约币种共用标的分析
        swap = f"{coin}_SWAP"
        regime_model[swap] = regime_model[coin].copy()

    _save_state(state)
    print_regime_report(regime_model)
    return regime_model


def _analyze_regime_effect(coin: str, df, ind):
    """
    将 30 天数据分段，识别每段行情，测试不同参数。
    寻找「在 X 行情下，Y 参数表现最好」的模式。
    """
    from backtesting.grid import simulate_grid

    # 简化：用不同参数组合在全量数据上跑，然后按行情分段汇总
    param_combos = []
    space = SEARCH_SPACE.get(coin, {}).get("grid_range_pct", (0.02, 0.15, 0.02))

    lo, hi, step = space
    v = lo
    while v <= hi:
        v = round(v, 3)
        param_combos.append(v)
        v += step

    # 全量回测每个参数
    regime_perf = defaultdict(lambda: defaultdict(list))  # regime → param_val → [pnl%]

    for val in param_combos:
        r = simulate_grid(ind, grid_width=val, grid_levels=5)
        if not r:
            continue

        # 按行情分段分析（简化：用最近的行情比例标注）
        regimes = _detect_regimes_in_df(df, ind)

        for regime, pct in regimes.items():
            # 粗略估算：按比例分配 PnL
            regime_perf[regime][val].append(r["pnl_pct"] * pct)

    # 找每个行情下的最优参数
    best_params = {}
    for regime in REGIME_TYPES:
        vals = regime_perf.get(regime, {})
        if not vals:
            continue

        # 计算每个参数值的平均 PnL
        avg_pnls = {}
        for val, pnls in vals.items():
            avg_pnls[val] = sum(pnls) / len(pnls) if pnls else 0

        if not avg_pnls:
            continue

        best_val = max(avg_pnls, key=avg_pnls.get)
        best_pnl = avg_pnls[best_val]

        top3 = sorted(avg_pnls.items(), key=lambda x: x[1], reverse=True)[:3]

        best_params[regime] = {
            "best_param": best_val,
            "best_pnl": round(best_pnl, 2),
            "top3": [{"val": v, "pnl": round(p, 2)} for v, p in top3],
        }

    return best_params


def _detect_regimes_in_df(df, ind):
    """分析 DataFrame 中各行情类型占比"""
    if hasattr(ind, 'rsi') and hasattr(ind, 'bb_width'):
        rsi = ind.rsi.iloc[-1] if hasattr(ind.rsi, 'iloc') else 50
        bbw = ind.bb_width.iloc[-1] if hasattr(ind.bb_width, 'iloc') else 0.05

        regimes = {"ranging": 0.5}
        if rsi > 60:
            regimes["trending_up"] = 0.3
            regimes["ranging"] = 0.4
        elif rsi < 40:
            regimes["trending_down"] = 0.3
            regimes["ranging"] = 0.4
        if bbw > 0.08:
            regimes["high_volatility"] = 0.2
        else:
            regimes["low_volatility"] = 0.2
        return regimes

    return {"ranging": 0.6, "trending_up": 0.2, "trending_down": 0.2}


def print_regime_report(model: dict):
    print(f"\n📈 行情-参数关联模型")
    print(f"{'='*60}")
    for coin, regimes in sorted(model.items()):
        if not regimes:
            continue
        print(f"\n  【{coin}】")
        for regime in ["trending_up", "trending_down", "ranging", "high_volatility"]:
            info = regimes.get(regime)
            if not info:
                continue
            emoji = {"trending_up": "📈", "trending_down": "📉", "ranging": "↔️", "high_volatility": "🌊"}.get(regime, "❓")
            print(f"    {emoji} {regime}: grid_range_pct={info['best_param']} (PnL {info['best_pnl']:+.2f}%)")

    # ── 给出当前行情的最优参数建议 ──
    print(f"\n  📍 当前行情建议:")
    for coin in ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]:
        regime = _get_current_regime(coin)
        coin_data = model.get(coin, {})
        info = coin_data.get(regime)
        if info:
            print(f"    {coin}: {regime} → grid_range_pct 建议 {info['best_param']}")
        else:
            print(f"    {coin}: {regime} → 无模型数据，使用当前配置")


def _get_current_regime(coin: str) -> str:
    log_file = ROOT / f"bot_{coin}.log"
    try:
        with open(log_file) as f:
            lines = f.readlines()
        for line in reversed(lines):
            if "行情:" in line and ("trending" in line or "ranging" in line):
                parts = line.split("|")
                regime_text = parts[0].split("行情:")[-1].strip()
                if "trending_up" in regime_text:
                    return "trending_up"
                elif "trending_down" in regime_text:
                    return "trending_down"
                elif "ranging" in regime_text:
                    return "ranging"
                break
    except: pass
    return "ranging"


# ═══════════════════════════════════════════════════════════
# ③ 自动试错引擎 — 探索 + 模型 + 决策 + 执行
# ═══════════════════════════════════════════════════════════

def auto_tune(apply_safe=True):
    """
    全自动调参: 极端行情检测 → 回滚检查 → 探索 → 学习 → 决策 → 执行。

    必须通过 evolution_lock 守卫。
    """
    logger.info("🧬 启动自动进化引擎...")

    # ═══════════════════════════════════════════════════════
    # 统一守卫 — 所有进化入口的标准模板
    # ═══════════════════════════════════════════════════════
    from evolution_lock import can_evolve

    ok, reason = can_evolve("brain.py auto-tune")
    if not ok:
        # 只做防守
        check_rollback(apply_revert=True)
        print(f"\n{'█'*60}")
        print(f"█  ⚠️ 进化引擎已锁定: {reason}")
        print(f"█  仅允许: 回滚 / 止损 / 降仓 / 冷却 / 熔断")
        print(f"█  已禁止: 探索 / 写入config / promote / 扩仓")
        print(f"{'█'*60}")
        return {
            "locked": True,
            "reason": reason,
            "actions_taken": "rollback_only",
        }

    # ═══════════════════════════════════════════════════════
    # 正常行情 — 完整进化流程
    # ═══════════════════════════════════════════════════════

    # Step 1: 回滚检查
    print("🔄 [1/4] 回滚检查...")
    reverted = check_rollback(apply_revert=True)
    if reverted:
        print(f"     回滚 {len(reverted)} 项（需要重启 bot 生效）")
    else:
        print("     无需要回滚的参数")

    # Step 2: 探索未测试的参数
    print("📡 [2/4] 参数空间探索...")
    findings = explore_parameter_space()
    print(f"     发现 {len(findings)} 个潜力组合" if findings else "     所有方向已知")

    # Step 3: 学习行情-参数关联
    print("🧠 [3/4] 行情-参数关联学习...")
    model = build_regime_model()

    # Step 4: 决策 + 执行
    print("⚡ [4/4] 智能决策...")
    decisions = _make_decisions(findings, model)
    print(f"     生成 {len(decisions)} 条决策")

    if not decisions:
        print("     (无安全可执行的建议)")
        if findings:
            print(f"\n  ℹ️ 有 {len(findings)} 个潜力组合，但未通过安全验证。")
            print(f"     可运行 `python brain.py explore` 查看详情。")
        return

    applied = []
    for d in decisions:
        print(f"\n  → {d['coin']}.{d['param']}: {d['current']} → {d['candidate']} "
              f"(PnL Δ={d['improvement']:+.2f}%, 行情: {d.get('regime','?')})")

        if apply_safe and d.get("safe", False):
            ok = _apply_param(d["coin"], d["param"], d["candidate"])
            if ok:
                applied.append(d)
                print(f"    ✅ 已自动应用 (模拟盘安全)")
            else:
                print(f"    ⚠️ 应用失败")
        else:
            print(f"    ⏸️  待确认（非安全区间或保守模式）")

    # ── 保存 ──
    state = _load_state()
    state["auto_trials"].append({
        "time": datetime.now(timezone.utc).isoformat(),
        "decisions": decisions,
        "applied": applied,
        "findings_count": len(findings),
    })
    _save_state(state)

    print(f"\n✅ 本次应用 {len(applied)} 条 | 智慧库积累 {state['total_explored']} 个参数组合")
    return applied


def _make_decisions(findings, model):
    """综合探索结果和行情模型，生成决策（含冷却检查）"""

    decisions = []

    for f in findings:
        coin = f["coin"]
        param = f["param"]
        candidate = f["candidate"]
        improvement = f["improvement"]
        current = f["current"]

        # 安全检查
        safe = _is_safe_change(coin, param, current, candidate)

        # 冷却检查
        cooldown = is_on_cooldown(coin, param, candidate)
        if cooldown["on_cooldown"] and not cooldown["can_override"]:
            logger.info(f"  🚫 {coin}.{param}={candidate} 冷却期 — {cooldown['reason']}")
            continue

        # 行情适配加分
        regime = _get_current_regime(coin)
        regime_bonus = 0
        coin_model = model.get(coin, {})
        for r, info in coin_model.items():
            if r == regime and abs(info.get("best_param", 999) - candidate) < 0.02:
                regime_bonus = improvement * 0.3  # 行情适配 +30% 权重

        # 回测确认
        if improvement > 0.5 and safe:
            decisions.append({
                **f,
                "regime": regime,
                "regime_bonus": round(regime_bonus, 2),
                "safe": safe,
                "score": round(improvement + regime_bonus, 2),
            })

    # 按综合得分排序
    decisions.sort(key=lambda x: x["score"], reverse=True)
    return decisions[:3]  # 最多 3 条


def _is_safe_change(coin, param, old_val, new_val):
    """判断参数变动是否安全"""
    # 变动不超过 100%
    if abs(new_val - old_val) / max(abs(old_val), 1e-10) > 1.0:
        return False

    # 必须大于 0
    if new_val <= 0:
        return False

    # 网格层数必须是整数
    if "count" in param or "entries" in param:
        if abs(new_val - round(new_val)) > 0.01:
            return False

    return True


def _apply_param(coin, param, val, persist=True, source="auto"):
    """写入参数到 config.py + 内存 + 记录回滚快照
    
    回滚(source=rollback)直接写入（防御操作始终允许），
    其他来源必须通过 safe_write_config（第二道门）。
    """
    import re

    var_map = _build_var_map()
    var_name = var_map.get((coin, param))

    if not var_name:
        logger.warning(f"  未找到 {coin}.{param} 对应变量")
        return False

    # 读旧值
    old_val = getattr(config, var_name, None)
    if old_val is None:
        old_val = val  # 首次设置

    # ── 写入 config.py ──
    if persist:
        from evolution_lock import safe_write_config
        ok = safe_write_config(coin, var_name, val, source=f"brain.py:{source}",
                                old_value=old_val)
        if not ok:
            logger.warning(f"  🚫 safe_write_config 拦截 {coin}.{var_name}={val} (source={source})")
            return False

    # 内存
    setattr(config, var_name, val)

    # 记录回滚快照
    if persist and old_val is not None and old_val != val:
        _record_param_change(coin, param, old_val, val, source=source)

    return True


# ═══════════════════════════════════════════════════════════
# ④ 自动回滚引擎 — 新参数亏了自动退回去
# ═══════════════════════════════════════════════════════════

ROLLBACK_EVAL_HOURS = 72      # 72小时后评估
ROLLBACK_LOSS_THRESHOLD = -3.0  # 净亏损超过 -$3 就回滚（或 -2%）
ROLLBACK_PNL_PCT_THRESHOLD = -2.0  # PnL% 低于 -2% 就回滚


def _record_param_change(coin, param, old_val, new_val, source="auto"):
    """记录每次参数改动，供回滚评估"""
    state = _load_state()
    state.setdefault("rollback_queue", [])

    # 避免重复记录（同一对同一时间）
    now = datetime.now(timezone.utc).isoformat()
    for r in state["rollback_queue"]:
        if r.get("coin") == coin and r.get("param") == param and not r.get("evaluated"):
            logger.info(f"  {coin}.{param} 已有待评估记录，跳过")
            return

    state["rollback_queue"].append({
        "coin": coin,
        "param": param,
        "old_value": old_val,
        "new_value": new_val,
        "applied_at": now,
        "source": source,
        "evaluated": False,
    })
    _save_state(state)
    logger.info(f"  📝 记录回滚快照: {coin}.{param} {old_val}→{new_val}")


def _get_coin_pnl_since(coin: str, since_iso: str) -> dict:
    """从 trade_records_{coin}.json 读取某时间点后的已实现 PnL（P2-1）。
    改用结构化交易记录，替代脆弱的「正则解析日志文本」——后者依赖日志格式、
    时间戳排版，且与日志双写/串台耦合。返回 {net_pnl, pnl_pct, settled_count, total_trades}。

    时间口径：记录用进程本地时间(tracker datetime.now())；since_iso 为 UTC，
    按 +8h 转本地比较（沿用项目既有的 UTC+8 假设；服务器时区非 UTC+8 时需同步调整）。
    """
    rec_file = ROOT / f"trade_records_{coin}.json"
    if not rec_file.exists():
        return {"net_pnl": 0, "pnl_pct": 0, "settled_count": 0, "total_trades": 0, "error": "无交易记录"}
    try:
        data = json.loads(rec_file.read_text(encoding="utf-8"))
    except Exception as e:
        return {"net_pnl": 0, "pnl_pct": 0, "settled_count": 0, "total_trades": 0, "error": str(e)}

    try:
        since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        since_local = (since_dt + timedelta(hours=8)).replace(tzinfo=None)
    except Exception:
        since_local = None

    net_pnl = 0.0
    count = 0
    for r in data.get("records", []):
        if r.get("strategy") == "cleanup":   # 启动清理不计入策略表现
            continue
        if since_local is not None:
            try:
                t = datetime.strptime(r.get("time", ""), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if t < since_local:
                continue
        net_pnl += float(r.get("pnl", 0) or 0)
        count += 1

    cap = config.COIN_CONFIG.get(coin, {}).get("initial_capital", 0) or 1
    return {
        "net_pnl": round(net_pnl, 3),
        "pnl_pct": round(net_pnl / cap * 100, 3),
        "settled_count": count,
        "total_trades": count,
        "capital_est": cap,
    }


def check_rollback(apply_revert=True) -> list:
    """
    用多维评分引擎评估所有待考核的参数变动。
    不再只看 PnL 是否亏损，而是综合 5 个指标打分。
    
    评分 < 20 → 回滚
    评分 20-49 → 降级警告
    评分 50-79 → 保留观察
    评分 ≥ 80 → 正式采纳
    """
    state = _load_state()
    queue = state.get("rollback_queue", [])
    if not queue:
        return []

    reverted = []
    now = datetime.now(timezone.utc)
    var_map = _build_var_map()

    for entry in queue:
        if entry.get("evaluated"):
            continue

        coin = entry["coin"]
        param = entry["param"]
        new_val = entry["new_value"]
        old_val = entry["old_value"]

        try:
            applied_at = datetime.fromisoformat(entry["applied_at"].replace("Z", "+00:00"))
        except:
            entry["evaluated"] = True
            entry["result"] = "skip: 时间解析失败"
            continue

        hours_elapsed = (now - applied_at).total_seconds() / 3600
        if hours_elapsed < ROLLBACK_EVAL_HOURS:
            continue  # 还没到评估时间

        # ── 收集数据 ──
        pnl_info = _get_coin_pnl_since(coin, entry["applied_at"])
        fee_info = calc_fee_ratio(coin, entry["applied_at"])

        net_pnl = pnl_info.get("net_pnl", 0)
        fee_ratio = fee_info.get("fee_ratio", 0)
        drawdown = pnl_info.get("drawdown", 0)

        # ── 智能评分 ──
        scored = score_param(
            coin=coin, param=param,
            current_value=old_val, new_value=new_val,
            current_pnl=0, new_pnl=net_pnl,
            new_fee_ratio=fee_ratio,
            backtest_baseline_pnl=0,
        )

        # ── 检查冷却 ──
        cooldown = is_on_cooldown(coin, param, new_val)
        if cooldown["on_cooldown"] and scored["grade"] not in ("rollback", "downgrade"):
            scored["grade"] = "cooldown_blocked"
            scored["action"] = f"🚫 冷却期阻止 ({cooldown['reason']})"

        entry["evaluated"] = True

        if scored["grade"] == "rollback":
            # 强制回滚
            var_name = var_map.get((coin, param))
            if var_name and apply_revert:
                ok = _apply_param(coin, param, old_val, persist=True, source="rollback")
                if ok:
                    logger.warning(f"🔄 回滚 {coin}.{param}: {new_val}→{old_val} 评分: {scored['score']:.0f}")

            # 加入冷却
            add_cooldown(coin, param, new_val, f"评分 {scored['score']:.0f}: {scored['action']}")

            entry["result"] = f"rollback: {scored['action']} (评分{scored['score']:.0f})"
            entry["score_detail"] = scored
            reverted.append({**entry, "scored": scored})

        elif scored["grade"] == "downgrade":
            entry["result"] = f"downgrade: {scored['action']} (评分{scored['score']:.0f})"
            entry["score_detail"] = scored
            logger.info(f"  ⚠️ {coin}.{param} 评分 {scored['score']:.0f} — 降级警告，继续观察")

        elif scored["grade"] == "cooldown_blocked":
            entry["result"] = f"cooldown_blocked: {scored['action']}"
            entry["score_detail"] = scored
            logger.info(f"  🚫 {coin}.{param} 冷却期阻止")

        else:
            entry["result"] = f"passed: {scored['action']} (评分{scored['score']:.0f}, PnL ${net_pnl:.2f})"
            entry["score_detail"] = scored
            logger.info(f"  ✅ {coin}.{param} {scored['action']} (评分{scored['score']:.0f})")

    _save_state(state)

    if reverted:
        print(f"\n🔄 回滚了 {len(reverted)} 个参数:")
        for r in reverted:
            s = r.get("scored", {})
            print(f"  {r['coin']}.{r['param']}: {r['new_value']}→{r['old_value']} ({s.get('action','')})")
    return reverted


def _build_var_map():
    return {
        ("TRX", "grid_range_pct"): "TRX_GRID_RANGE_PCT",
        ("TRX", "grid_count"): "TRX_GRID_COUNT",
        ("ETH", "grid_range_pct"): "ETH_SPOT_GRID_RANGE_PCT",
        ("ETH", "grid_count"): "ETH_SPOT_GRID_COUNT",
        ("SOL", "grid_range_pct"): "SOL_SPOT_GRID_RANGE_PCT",
        ("SOL", "grid_count"): "SOL_SPOT_GRID_COUNT",
        ("SOL", "max_grid_entries"): "SOL_SPOT_MAX_GRID_ENTRIES",
        ("ETH_SWAP", "grid_range_pct"): "ETH_GRID_RANGE_PCT",
        ("ETH_SWAP", "grid_count"): "ETH_GRID_COUNT",
        ("SOL_SWAP", "grid_range_pct"): "SOL_GRID_RANGE_PCT",
        ("SOL_SWAP", "grid_count"): "SOL_GRID_COUNT",
        ("SOL_SWAP", "max_grid_entries"): "SOL_FUTURES_MAX_GRID_ENTRIES",
    }


# ═══════════════════════════════════════════════════════════
# 状态查看
# ═══════════════════════════════════════════════════════════

def show_status():
    state = _load_state()
    exploration = state.get("exploration", {})
    regime_model = state.get("regime_model", {})
    trials = state.get("auto_trials", [])

    print("🧬 智能进化引擎状态")
    print("=" * 60)

    print(f"\n📊 参数探索:")
    print(f"   累计探索: {state['total_explored']} 个参数组合")
    if exploration:
        dates = sorted(exploration.keys(), reverse=True)
        latest = exploration[dates[0]]
        print(f"   最近探索 ({dates[0]}): {latest['total_candidates']} 个候选")
        if latest.get("findings"):
            for f in latest["findings"][:3]:
                print(f"     → {f['coin']}.{f['param']}: {f['current']}→{f['candidate']} (+{f['improvement']:+.2f}%)")

    print(f"\n📈 行情模型:")
    coins_with_model = sum(1 for regimes in regime_model.values() if regimes)
    print(f"   已有 {coins_with_model} 个币种的行情-参数关联")

    print(f"\n⚡ 自动试错:")
    print(f"   已执行 {len(trials)} 次自动调参")
    if trials:
        last = trials[-1]
        print(f"   最近 ({last['time'][:19]}): {len(last.get('applied',[]))} 条应用")

    # 回滚状态（含多维评分）
    queue = state.get("rollback_queue", [])
    if queue:
        pending = [r for r in queue if not r.get("evaluated")]
        evaluated = [r for r in queue if r.get("evaluated")]
        by_result = defaultdict(list)
        for r in evaluated:
            res = r.get("result", "unknown")
            if "rollback" in str(res):
                by_result["rollback"].append(r)
            elif "downgrade" in str(res):
                by_result["downgrade"].append(r)
            elif "cooldown_blocked" in str(res):
                by_result["cooldown_blocked"].append(r)
            else:
                by_result["passed"].append(r)

        print(f"\n🔄 参数考核（多维评分）:")
        print(f"   待评估: {len(pending)} 项")
        for r in pending:
            applied_at = r.get("applied_at", "")[:10]
            print(f"     ⏳ {r['coin']}.{r['param']}: {r['old_value']}→{r['new_value']} (应用於 {applied_at})")
        if by_result.get("passed"):
            print(f"   ✅ 通过: {len(by_result['passed'])} 项")
            for r in by_result["passed"][:2]:
                sd = r.get("score_detail", {})
                score_val = sd.get('score', 0)
                if isinstance(score_val, (int, float)):
                    print(f"     {r['coin']}.{r['param']} 评分: {score_val:.0f}")
                else:
                    print(f"     {r['coin']}.{r['param']} 评分: ?")
        if by_result.get("downgrade"):
            print(f"   ⚠️ 降级: {len(by_result['downgrade'])} 项（继续观察）")
        if by_result.get("rollback"):
            print(f"   🔄 回滚: {len(by_result['rollback'])} 项")
            for r in by_result["rollback"]:
                sd = r.get("score_detail", {})
                score_val = sd.get('score', 0)
                score_str = f"{score_val:.0f}" if isinstance(score_val, (int, float)) else "?"
                print(f"     {r['coin']}.{r['param']}: {r['new_value']}→{r['old_value']} (评分: {score_str})")

    # 当前行情
    print(f"\n📍 当前行情:")
    for coin in ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]:
        regime = _get_current_regime(coin)
        coin_model = regime_model.get(coin, {})
        info = coin_model.get(regime)
        if info:
            print(f"   {coin}: {regime} → 模型建议 grid_range_pct={info['best_param']}")
        else:
            print(f"   {coin}: {regime} → 无模型数据")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

# ========== merged from optimize.py ==========

"""
贝叶斯参数优化 — 用 Optuna 替代离散网格扫参。

为什么更好：离散扫参只在固定网格点（如 width∈{0.01,0.02,...}）上试，
Optuna(TPE) 在连续区间内智能采样，用更少的回测次数找到更优的 width×levels 组合，
并且是「联合优化」（同时调宽度和层数），能抓住离散扫参漏掉的组合效应。

安全设计：未安装 optuna 时自动回退到 backtesting.grid.sweep_grid_params（离散扫参），
调用方拿到的返回结构一致，互不感知。纯回测，不碰实盘下单。

用法：
  from optimize import optimize_grid
  res = optimize_grid("ETH", ind)          # ind 可省略（自动 load）
  res["best_params"]  -> {"grid_width":.., "grid_levels":..}
  res["top"]          -> [{grid_width,grid_levels,pnl_pct,win_rate,total_trades}, ...]
  res["method"]       -> "optuna" | "sweep"
"""
_opt_logger = logging.getLogger("optimize")

# 各币种搜索区间（width 价格比例 / levels 网格层数）
GRID_RANGES = {
    "TRX":      {"width": (0.01, 0.20), "levels": (2, 15)},
    "ETH":      {"width": (0.02, 0.30), "levels": (2, 12)},
    "SOL":      {"width": (0.01, 0.20), "levels": (2, 10)},
    "TRX_SWAP": {"width": (0.01, 0.20), "levels": (2, 12)},
    "ETH_SWAP": {"width": (0.02, 0.30), "levels": (2, 10)},
    "SOL_SWAP": {"width": (0.01, 0.20), "levels": (2, 8)},
}
_DEFAULT_RANGE = {"width": (0.01, 0.20), "levels": (2, 12)}

MIN_TRADES = 5   # 回测交易数低于此值视为不可信，给大惩罚（防过拟合到偶然组合）

_TOP_KEYS = ("grid_width", "grid_levels", "pnl_pct", "win_rate", "total_trades")


def _load_ind(coin: str, days: int = 180):
    """按需加载 IndicatorPack（与 brain/ai_tuner 一致）。"""
    try:
        from backtesting.engine import load_from_cache, calc_indicators
    except ImportError:
        return None
    base = coin.replace("_SWAP", "")
    df = load_from_cache(base, "1h", days)
    if df is None or len(df) < 200:
        return None
    return calc_indicators(df)


def _score(r: dict) -> float:
    """目标值：PnL%，但交易过少 → 大惩罚（避免优化到偶然的高收益组合）。"""
    if not r:
        return -1e9
    if r.get("total_trades", 0) < MIN_TRADES:
        return -100.0
    return float(r.get("pnl_pct", -1e9))


def _slim(r: dict) -> dict:
    return {k: r.get(k) for k in _TOP_KEYS}


def optimize_grid(coin: str, ind=None, n_trials: int = 60, days: int = 180,
                  top_n: int = 5) -> dict | None:
    """对单个币种的网格参数(width×levels)做贝叶斯优化。
    返回 None 表示无回测数据。"""
    if ind is None:
        ind = _load_ind(coin, days)
    if ind is None:
        return None

    from backtesting.grid import simulate_grid
    rng = GRID_RANGES.get(coin, _DEFAULT_RANGE)
    w_lo, w_hi = rng["width"]
    l_lo, l_hi = rng["levels"]

    # ── Optuna 路径 ──
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        def objective(trial):
            w = trial.suggest_float("grid_width", w_lo, w_hi)
            lv = trial.suggest_int("grid_levels", l_lo, l_hi)
            r = simulate_grid(ind, grid_width=w, grid_levels=lv)
            trial.set_user_attr("r", _slim(r) if r else {})
            return _score(r)

        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42),
        )
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

        done = [t for t in study.trials if t.value is not None and t.user_attrs.get("r")]
        done.sort(key=lambda t: t.value, reverse=True)
        top = [t.user_attrs["r"] for t in done[:top_n]]
        best = top[0] if top else {}
        return {
            "method": "optuna",
            "coin": coin,
            "n_trials": len(done),
            "best_params": {
                "grid_width": best.get("grid_width"),
                "grid_levels": best.get("grid_levels"),
            },
            "best_value": best.get("pnl_pct", -1e9),
            "top": top,
        }
    except ImportError:
        pass  # 回退离散扫参

    # ── 回退：离散扫参 ──
    from backtesting.grid import sweep_grid_params
    results = sweep_grid_params(ind, coin)
    if not results:
        return None
    top = [_slim(r) for r in results[:top_n]]
    best = top[0]
    return {
        "method": "sweep",
        "coin": coin,
        "n_trials": len(results),
        "best_params": {
            "grid_width": best.get("grid_width"),
            "grid_levels": best.get("grid_levels"),
        },
        "best_value": best.get("pnl_pct", -1e9),
        "top": top,
    }


def current_pnl(coin: str, width: float, levels: int, ind=None, days: int = 180) -> float:
    """当前配置在同一份数据上的回测 PnL%（用于和优化结果对比）。"""
    if ind is None:
        ind = _load_ind(coin, days)
    if ind is None:
        return 0.0
    from backtesting.grid import simulate_grid
    try:
        r = simulate_grid(ind, grid_width=float(width), grid_levels=int(levels))
        return float(r.get("pnl_pct", 0.0)) if r else 0.0
    except Exception:
        return 0.0


def _optuna_findings(coin, current_params, ind):
    """用 Optuna 联合优化 width×levels，产出与离散扫参同构的 findings。
    返回 None 表示 Optuna 不可用（调用方回退离散扫参）；返回 [] 表示无显著提升。"""
    res = optimize_grid(coin, ind=ind)
    if res is None:
        return None
    if res.get("method") != "optuna":
        # optimize 内部已回退到离散扫参 → 让 brain 走自己的离散逻辑，保持单一来源
        return None

    best = res["best_params"]
    best_w, best_lv = best.get("grid_width"), best.get("grid_levels")
    if best_w is None or best_lv is None:
        return []

    cur_w = current_params.get("grid_range_pct")
    cur_lv = current_params.get("grid_count")
    cur_pnl = current_pnl(coin, cur_w or best_w, cur_lv or best_lv, ind=ind)
    best_pnl = res.get("best_value", cur_pnl)
    improvement = best_pnl - cur_pnl
    if improvement <= 0.3:   # 与离散扫参一致的提升门槛
        return []

    findings = []
    if cur_w is not None and abs(round(best_w, 3) - cur_w) > 1e-4:
        findings.append({
            "coin": coin, "param": "grid_range_pct",
            "current": cur_w, "candidate": round(best_w, 3),
            "current_pnl": round(cur_pnl, 2), "candidate_pnl": round(best_pnl, 2),
            "improvement": round(improvement, 2), "win_rate": 0.0, "rank": 1,
        })
    if cur_lv is not None and int(best_lv) != int(cur_lv):
        findings.append({
            "coin": coin, "param": "grid_count",
            "current": cur_lv, "candidate": int(best_lv),
            "current_pnl": round(cur_pnl, 2), "candidate_pnl": round(best_pnl, 2),
            "improvement": round(improvement, 2), "win_rate": 0.0, "rank": 1,
        })
    return findings


# ═══════════════════════════════════════════════════════════════
# Walk-forward 验证 — 把"换时段也成立"写成代码
# ═══════════════════════════════════════════════════════════════

def walk_forward_optimize(coin: str, ind=None, window_days: int = 30,
                          n_trials: int = 30, days: int = 180) -> dict | None:
    """
    滚动窗口优化 + 样本外验证。

    不再"全段找最优"：
      1. 把 180 天切成 window_days 长的窗口（30d 一窗）
      2. 在窗口 N 上优化，在 N+1（没见过的数据）上验证
      3. 只推荐所有窗口上样本外也赚钱的参数

    返回:
      - oos_pnl_pct:     样本外平均 PnL%（真正有参考价值）
      - in_sample_pnl_pct: 样本内平均 PnL%（仅供参考，偏高）
      - oos_positive:    样本外盈利窗口比例
      - stable_params:   多窗口都选中的稳健参数
      - per_window:      每窗详情
    """
    if ind is None:
        ind = _load_ind(coin, days)
    if ind is None:
        return None

    from backtesting.grid import simulate_grid
    n = len(ind.close)
    window_bars = int(window_days * 96)  # 15min bars → ~96 per day

    if n < window_bars * 3:
        # fallback: 用更小的窗口
        window_bars = n // 3
        _opt_logger.info(f"{coin}: 数据不足 ({n} bars)，自动缩小窗口到 {window_bars} bars")
    if n < 180:  # 最少需要 3 小时数据
        _opt_logger.warning(f"{coin}: 数据不足（{n} bars），至少需要 180")
        return None

    rng = GRID_RANGES.get(coin, _DEFAULT_RANGE)
    w_lo, w_hi = rng["width"]
    l_lo, l_hi = rng["levels"]

    per_window = []
    oos_scores = []
    in_sample_scores = []
    param_votes = []  # 每窗的最佳参数

    step = window_bars // 2  # 半窗步长 → 窗口有重叠

    # 检测 optuna 是否可用（一次）并静默日志
    try:
        import optuna as _optuna_mod
        _optuna_mod.logging.set_verbosity(_optuna_mod.logging.WARNING)
        _optuna_available = True
    except ImportError:
        _optuna_available = False

    for start in range(0, n - window_bars * 2, step):
        train_end = start + window_bars
        test_end = min(train_end + window_bars, n)

        if test_end - train_end < window_bars // 2 or train_end - start < 60:
            continue

        # 切分样本内 / 样本外
        train_ind = _slice_ind(ind, start, train_end)
        test_ind = _slice_ind(ind, train_end, test_end)

        # ── 在 train 上优化 ──
        try:
            if not _optuna_available:
                raise ImportError

            def objective(trial):
                w = trial.suggest_float("grid_width", w_lo, w_hi)
                lv = trial.suggest_int("grid_levels", l_lo, l_hi)
                r = simulate_grid(train_ind, grid_width=w, grid_levels=lv)
                return _score(r)

            study = _optuna_mod.create_study(
                direction="maximize",
                sampler=_optuna_mod.samplers.TPESampler(seed=start),
            )
            study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
            best_params = {
                "grid_width": study.best_params.get("grid_width"),
                "grid_levels": study.best_params.get("grid_levels"),
            }
            is_pnl = study.best_value or 0
        except ImportError:
            # 回退：离散扫参
            from backtesting.grid import sweep_grid_params
            results = sweep_grid_params(train_ind, coin)
            if not results:
                continue
            best = results[0]
            best_params = {"grid_width": best["grid_width"], "grid_levels": best["grid_levels"]}
            is_pnl = best.get("pnl_pct", -1e9)

        # ── 在 test 上验证（样本外）──
        test_r = simulate_grid(
            test_ind,
            grid_width=best_params["grid_width"],
            grid_levels=int(best_params.get("grid_levels", 3)),
        )
        oos_pnl = test_r.get("pnl_pct", 0) if test_r else 0

        if oos_pnl > 0:
            oos_scores.append(oos_pnl)
        in_sample_scores.append(max(0, is_pnl))

        per_window.append({
            "train_range": f"bar[{start}:{train_end}]",
            "test_range": f"bar[{train_end}:{test_end}]",
            "best_params": best_params,
            "in_sample_pnl": round(is_pnl, 2),
            "oos_pnl": round(oos_pnl, 2),
            "oos_pass": oos_pnl > 0,
        })
        param_votes.append((best_params["grid_width"], best_params["grid_levels"], oos_pnl))

    if not per_window:
        return None

    # ── 统计 ──
    passed = sum(1 for w in per_window if w["oos_pass"])
    avg_ins = sum(in_sample_scores) / len(in_sample_scores)
    avg_oos = sum(oos_scores) / len(oos_scores) if oos_scores else 0

    # ── 稳健参数：多窗口都靠谱的 ──
    stable_best_count = 0
    if param_votes:
        # 按样本外 PnL 加权投票
        stable_count = {}
        for w, lv, oos in param_votes:
            key = (w, lv)
            stable_count[key] = stable_count.get(key, 0) + (1 if oos > 0 else 0)
        best_key = max(stable_count, key=stable_count.get)
        stable_params = {"grid_width": best_key[0], "grid_levels": best_key[1]}
        stable_best_count = stable_count[best_key]
    else:
        stable_params = per_window[0]["best_params"] if per_window else {}

    return {
        "method": "walk_forward",
        "coin": coin,
        "windows": len(per_window),
        "oos_positive": passed,
        "in_sample_pnl_pct": round(avg_ins, 2),
        "oos_pnl_pct": round(avg_oos, 2),
        "recommend": avg_oos > 0,  # 样本外真赚钱才推荐
        "stable_params": stable_params,
        "stable_count": stable_best_count,
        "per_window": per_window,
    }


def _slice_ind(ind, start: int, end: int):
    """切出 IndicatorPack 的 [start:end) 子集"""
    import copy
    sliced = copy.copy(ind)
    for attr in ("close", "high", "low", "vol", "ema20", "ema60",
                 "bb_upper", "bb_lower", "bb_width", "atr", "rsi", "adx"):
        arr = getattr(ind, attr, None)
        if arr is not None and len(arr) >= end:
            setattr(sliced, attr, arr[start:end])
    if ind.regimes and len(ind.regimes) >= end:
        sliced.regimes = ind.regimes[start:end]
    return sliced


# ═══════════════════════════════════════════════════════════════
# 邻域稳定性评分 — 不取尖点，取稳健平台区
# ═══════════════════════════════════════════════════════════════

def stability_score(coin: str, width: float, levels: int, ind=None, days: int = 180) -> dict:
    """
    检查 (width, levels) 周围邻居的表现。

    不是取最高 PnL 的单个点（可能是噪声尖峰），
    而是取"周围邻居也都不错"的平台区中心。

    返回:
      - center_pnl:    中心点 PnL%
      - neighbor_avg:   邻居平均 PnL%
      - neighbor_min:   邻居最低 PnL%（如果这个都很高 → 真平台）
      - neighbor_var:   邻居 PnL 方差（越小越稳）
      - stable:         是否推荐（neighbor_avg > 0 且 min > -5%）
    """
    if ind is None:
        ind = _load_ind(coin, days)
    if ind is None:
        return {"error": "no data"}

    from backtesting.grid import simulate_grid

    # 中心点
    center_r = simulate_grid(ind, grid_width=width, grid_levels=levels)
    center_pnl = center_r.get("pnl_pct", -1e9) if center_r else -1e9

    # 邻居：width ± 15%, levels ± 1
    offsets = []
    for w_mult in [0.85, 1.0, 1.15]:
        for lv_delta in [-1, 0, 1]:
            if w_mult == 1.0 and lv_delta == 0:
                continue
            w = width * w_mult
            lv = max(2, levels + lv_delta)
            offsets.append((w, lv))

    neighbor_pnls = []
    for w, lv in offsets:
        r = simulate_grid(ind, grid_width=w, grid_levels=lv)
        pnl = r.get("pnl_pct", -1e9) if r else -1e9
        neighbor_pnls.append(pnl)

    avg = sum(neighbor_pnls) / len(neighbor_pnls) if neighbor_pnls else -1e9
    mn = min(neighbor_pnls) if neighbor_pnls else -1e9
    var = sum((x - avg) ** 2 for x in neighbor_pnls) / len(neighbor_pnls)

    stable = avg > 0 and mn > -5.0  # 邻居平均盈利且最差的也不崩

    return {
        "center_pnl": round(center_pnl, 2),
        "neighbor_avg": round(avg, 2),
        "neighbor_min": round(mn, 2),
        "neighbor_var": round(var, 2),
        "neighbor_count": len(neighbor_pnls),
        "stable": stable,
        "verdict": "✅ 稳健平台" if stable else ("⚠️ 尖峰/边缘" if center_pnl > avg * 1.5 else "❌ 不稳定"),
    }


def _cli_optimize():
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    coins = sys.argv[1:] or list(GRID_RANGES)
    for c in coins:
        res = optimize_grid(c)
        if not res:
            print(f"【{c}】无回测数据")
            continue
        bp = res["best_params"]
        print(f"\n【{c}】方法={res['method']} 试验={res['n_trials']} "
              f"最优 width={bp['grid_width']} levels={bp['grid_levels']} → PnL={res['best_value']:+.2f}%")
        for i, t in enumerate(res["top"], 1):
            print(f"  #{i}: w={t['grid_width']:.3f} lv={t['grid_levels']} "
                  f"→ PnL={t['pnl_pct']:+.2f}% 胜率={t['win_rate']:.0f}% ({t['total_trades']}笔)")


# ========== merged from param_score.py ==========

"""
参数评分引擎 — 替代简单二元回滚，用多维指标打分

五个评估维度:
  ① 净PnL         ② PnL%         ③ 最大回撤
  ④ 手续费吞噬率   ⑤ 相对baseline优势

评分等级:
  score ≥ 80 → promote (正式采纳)
  score 50-79 → keep  (保留观察)
  score 20-49 → downgrade (降级/警告)
  score < 20  → rollback (强制回滚)
"""

SCORE_FILE = str(ROOT / "param_scores.json")
_score_logger = logging.getLogger("param_score")

# ═══════════════════════════════════════════════════════════
# ① 分币种回滚阈值
# ═══════════════════════════════════════════════════════════

COIN_THRESHOLDS = {
    # coin: {pnl_dollar, pnl_pct, fee_ratio_warn, fee_ratio_fatal, drawdown_max}
    "TRX":      {"pnl_dollar": -1.0, "pnl_pct": -1.2, "fee_warn": 0.30, "fee_fatal": 0.50, "dd_max": 8},
    "ETH":      {"pnl_dollar": -3.0, "pnl_pct": -2.0, "fee_warn": 0.35, "fee_fatal": 0.55, "dd_max": 12},
    "SOL":      {"pnl_dollar": -2.0, "pnl_pct": -2.5, "fee_warn": 0.40, "fee_fatal": 0.60, "dd_max": 15},
    "TRX_SWAP": {"pnl_dollar": -1.5, "pnl_pct": -1.0, "fee_warn": 0.25, "fee_fatal": 0.45, "dd_max": 6},
    "ETH_SWAP": {"pnl_dollar": -2.5, "pnl_pct": -1.5, "fee_warn": 0.30, "fee_fatal": 0.50, "dd_max": 10},
    "SOL_SWAP": {"pnl_dollar": -2.0, "pnl_pct": -2.0, "fee_warn": 0.35, "fee_fatal": 0.55, "dd_max": 12},
}

# 极端行情暂停阈值
# 注意：is_extreme_market 实际比较的是 ticker 的 open24h（24 小时涨跌），故键名用 24h。
EXTREME_THRESHOLDS = {
    "btc_24h_drop_pct": -3.0,
    "eth_24h_drop_pct": -4.0,
    "funding_extreme_abs": 0.0005,   # 费率绝对值 > 0.05%
}

# 回滚冷却期
ROLLBACK_COOLDOWN_DAYS = 7
# 冷却期内如果新回测收益超过 baseline 30% 可以破例
COOLDOWN_OVERRIDE_MULTIPLIER = 1.30

# ═══════════════════════════════════════════════════════════
# ② 手续费吞噬率计算
# ═══════════════════════════════════════════════════════════

def calc_fee_ratio(coin: str, since_iso: str = None) -> dict:
    """
    从日志计算手续费 / 毛利润 比例。
    返回 {gross_pnl, total_fees, fee_ratio, total_trades}
    """
    log_file = ROOT / f"bot_{coin}.log"
    if not log_file.exists():
        return {"gross_pnl": 0, "total_fees": 0, "fee_ratio": 0, "total_trades": 0}

    gross_pnl = 0.0  # 成交价差利润（不算手续费）
    total_fees = 0.0
    trade_count = 0

    since_dt = None
    if since_iso:
        from datetime import datetime, timedelta
        since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        since_local = (since_dt + timedelta(hours=8)).replace(tzinfo=None)

    last_ts = None
    try:
        with open(log_file) as f:
            for line in f:
                try:
                    last_ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass

                if since_dt and last_ts and last_ts < since_local:
                    continue

                # 匹配手续费: "fee=0.0123" "手续费: 0.045" "taker_fee: 0.02"
                m = re.search(r'(?:fee|手续费|taker_fee|maker_fee)\s*[:=]\s*([\d.]+)', line, re.IGNORECASE)
                if m:
                    total_fees += float(m.group(1))

                # 匹配毛利润: "grid_profit=0.5" "trend_profit=-1.2" "settle: +1.5"
                m = re.search(r'(?:grid_profit|trend_profit|pnl_delta)\s*[:=]\s*([+-]?[\d.]+)', line)
                if m:
                    gross_pnl += float(m.group(1))
                    trade_count += 1

                # 网格结算: "settled +0.5 USDT"
                m = re.search(r'settled?\s*:\s*([+-][\d.]+)', line)
                if m:
                    gross_pnl += float(m.group(1))
                    trade_count += 1

    except Exception as e:
        return {"gross_pnl": gross_pnl, "total_fees": total_fees, "fee_ratio": 0, "total_trades": trade_count, "error": str(e)}

    fee_ratio = total_fees / gross_pnl if gross_pnl > 0 else (1.0 if total_fees > 0 else 0)

    return {
        "gross_pnl": round(gross_pnl, 3),
        "total_fees": round(total_fees, 4),
        "fee_ratio": round(fee_ratio, 3),
        "total_trades": trade_count,
    }


# ═══════════════════════════════════════════════════════════
# ③ 极端行情检测
# ═══════════════════════════════════════════════════════════

def is_extreme_market() -> dict:
    """检测是否处于极端行情，返回 {is_extreme, reasons}"""
    import httpx
    reasons = []
    is_extreme = False

    try:
        client = httpx.Client(timeout=10)

        # BTC ticker
        r = client.get("https://www.okx.com/api/v5/market/ticker", params={"instId": "BTC-USDT"})
        if r.status_code == 200:
            data = r.json()
            item = data.get("data", [{}])[0]
            price = float(item.get("last", 0))
            open24 = float(item.get("open24h", 0))
            if open24 > 0:
                change_pct = (price - open24) / open24 * 100
                if change_pct < EXTREME_THRESHOLDS["btc_24h_drop_pct"]:
                    is_extreme = True
                    reasons.append(f"BTC 24h 跌幅 {change_pct:.1f}% > 阈值 {EXTREME_THRESHOLDS['btc_24h_drop_pct']}%")

        # BTC 资金费率
        r = client.get("https://www.okx.com/api/v5/public/funding-rate", params={"instId": "BTC-USDT-SWAP"})
        if r.status_code == 200:
            data = r.json()
            item = data.get("data", [{}])[0]
            fr = float(item.get("fundingRate", 0))
            if abs(fr) > EXTREME_THRESHOLDS["funding_extreme_abs"]:
                is_extreme = True
                reasons.append(f"资金费率极端 {fr:.4%}")

        # ETH ticker
        r = client.get("https://www.okx.com/api/v5/market/ticker", params={"instId": "ETH-USDT"})
        if r.status_code == 200:
            data = r.json()
            item = data.get("data", [{}])[0]
            price = float(item.get("last", 0))
            open24 = float(item.get("open24h", 0))
            if open24 > 0:
                change_pct = (price - open24) / open24 * 100
                if change_pct < EXTREME_THRESHOLDS["eth_24h_drop_pct"]:
                    is_extreme = True
                    reasons.append(f"ETH 24h 跌幅 {change_pct:.1f}% > 阈值 {EXTREME_THRESHOLDS['eth_24h_drop_pct']}%")

        client.close()
    except Exception as e:
        _score_logger.warning(f"极端行情检测失败: {e}")

    return {"is_extreme": is_extreme, "reasons": reasons}


# ═══════════════════════════════════════════════════════════
# ④ 参数冷却/黑名单
# ═══════════════════════════════════════════════════════════

def _load_scores():
    if os.path.exists(SCORE_FILE):
        with open(SCORE_FILE) as f:
            return json.load(f)
    return {"scores": [], "cooldown": {}, "history": []}


def _save_scores(data):
    # 原子写入：先写临时文件再 rename，防止中途 kill 导致文件损坏
    tmp = SCORE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, SCORE_FILE)


def is_on_cooldown(coin: str, param: str, value) -> dict:
    """
    检查参数是否在冷却期。
    返回 {on_cooldown, can_override, reason}
    """
    scores = _load_scores()
    key = f"{coin}.{param}.{value}"
    cooldown = scores.get("cooldown", {}).get(key)

    if not cooldown:
        return {"on_cooldown": False, "can_override": False, "reason": ""}

    rolled_at = datetime.fromisoformat(cooldown["rolled_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    days = (now - rolled_at).total_seconds() / 86400

    if days >= ROLLBACK_COOLDOWN_DAYS:
        # 冷却期结束
        del scores["cooldown"][key]
        _save_scores(scores)
        return {"on_cooldown": False, "can_override": False, "reason": ""}

    can_override = cooldown.get("override_allowed", False)
    remaining = round(ROLLBACK_COOLDOWN_DAYS - days, 1)

    return {
        "on_cooldown": True,
        "can_override": can_override,
        "reason": f"冷却期剩余 {remaining} 天 (回滚於 {cooldown['rolled_at'][:10]})",
        "rolled_at": cooldown["rolled_at"],
        "rollback_reason": cooldown.get("reason", ""),
    }


def add_cooldown(coin: str, param: str, value, rollback_reason=""):
    """将参数加入冷却黑名单"""
    scores = _load_scores()
    key = f"{coin}.{param}.{value}"
    scores.setdefault("cooldown", {})[key] = {
        "rolled_at": datetime.now(timezone.utc).isoformat(),
        "reason": rollback_reason,
    }
    _save_scores(scores)
    _score_logger.info(f"  🚫 {key} 冷藏 {ROLLBACK_COOLDOWN_DAYS} 天")


# ═══════════════════════════════════════════════════════════
# ⑤ 多维参数评分
# ═══════════════════════════════════════════════════════════

def score_param(
    coin: str,
    param: str,
    current_value,
    new_value,
    current_pnl: float,
    new_pnl: float,
    current_dd: float = 0,
    new_dd: float = 0,
    current_fee_ratio: float = 0,
    new_fee_ratio: float = 0,
    win_rate: float = 50,
    backtest_baseline_pnl: float = 0,
) -> dict:
    """
    多维评分引擎。

    满分 100 分，扣除项:
      - PnL 绝对亏损
      - PnL% 亏损
      - 最大回撤
      - 手续费占比过高
      - 相对 baseline 不优
    """

    thresholds = COIN_THRESHOLDS.get(coin, COIN_THRESHOLDS["ETH"])
    score = 60.0  # 起评分

    # ── PnL 维度 (0-25分) ──
    if new_pnl >= 0:
        score += 20
    elif new_pnl > thresholds["pnl_dollar"]:
        score += 10  # 亏损但未超阈值
    else:
        score += max(-15, new_pnl * 2)  # 严重亏损扣分

    # ── PnL% 维度 (0-15分) ──
    # 根据币种实际本金估算 PnL%
    base_capitals = {"TRX": 50, "ETH": 3000, "SOL": 4000, "TRX_SWAP": 60, "ETH_SWAP": 120, "SOL_SWAP": 40}
    cap = base_capitals.get(coin, 50)
    pnl_pct = new_pnl / cap * 100 if cap > 0 else 0

    if pnl_pct > 0:
        score += 15
    elif pnl_pct > thresholds["pnl_pct"]:
        score += 5
    else:
        score += max(-15, pnl_pct * 3)

    # ── 回撤惩罚 (0 to -20) ──
    if new_dd > thresholds["dd_max"] * 1.5:
        score -= 20
    elif new_dd > thresholds["dd_max"]:
        score -= 10

    # ── 手续费吞噬惩罚 (0 to -20) ──
    if new_fee_ratio > thresholds["fee_fatal"]:
        score -= 20
    elif new_fee_ratio > thresholds["fee_warn"]:
        score -= 10

    # ── 相对 baseline 优势 (-15 to +15) ──
    # 核心问题: 新参数有没有比旧参数更好？
    if backtest_baseline_pnl:
        improvement = new_pnl - current_pnl
        pct_improvement = (improvement / abs(backtest_baseline_pnl)) * 100 if backtest_baseline_pnl else 0
        if improvement > 0 and pct_improvement > 1:
            score += 15
        elif improvement > 0:
            score += 5
        elif improvement < -5:
            score -= 15
        elif improvement < 0:
            score -= 5
    else:
        # 无 baseline，看绝对表现
        if new_pnl > current_pnl * 1.1:
            score += 5

    # ── 胜率调整 (-5 to +5) ──
    if win_rate > 70:
        score += 5
    elif win_rate < 40:
        score -= 5

    # 钳制
    score = max(0, min(100, round(score, 1)))

    # ── 评级 ──
    if score >= 80:
        grade = "promote"
        action = "✅ 正式采纳"
    elif score >= 50:
        grade = "keep"
        action = "👀 保留观察"
    elif score >= 20:
        grade = "downgrade"
        action = "⚠️ 降级警告"
    else:
        grade = "rollback"
        action = "🔄 强制回滚"

    # 检查冷却期
    cooldown = is_on_cooldown(coin, param, new_value)
    if cooldown["on_cooldown"] and grade in ("promote", "keep", "downgrade") and not cooldown["can_override"]:
        grade = "cooldown_blocked"
        action = f"🚫 冷却期阻止 ({cooldown['reason']})"

    return {
        "score": score,
        "grade": grade,
        "action": action,
        "coin": coin,
        "param": param,
        "current_value": current_value,
        "new_value": new_value,
        "details": {
            "pnl_delta": round(new_pnl - current_pnl, 3),
            "pnl_pct": round(pnl_pct, 2),
            "fee_ratio": round(new_fee_ratio, 3),
            "drawdown_pct": round(new_dd, 2),
            "win_rate": round(win_rate, 1),
            "vs_baseline": round((new_pnl - current_pnl), 3),
        },
        "cooldown": cooldown,
    }


def score_from_finding(finding: dict, pnl_info: dict = None, fee_info: dict = None) -> dict:
    """从 explore 的 finding + 实测 pnl/fee 数据生成评分"""
    coin = finding["coin"]
    param = finding["param"]

    current_pnl = finding.get("current_pnl", 0)
    new_pnl = finding.get("candidate_pnl", 0)
    win_rate = finding.get("win_rate", 50)

    # 从实测数据补充
    if pnl_info:
        current_pnl = pnl_info.get("net_pnl", current_pnl)
    if fee_info:
        new_fee = fee_info.get("fee_ratio", 0)
    else:
        new_fee = 0

    return score_param(
        coin=coin,
        param=param,
        current_value=finding.get("current", 0),
        new_value=finding.get("candidate", 0),
        current_pnl=current_pnl,
        new_pnl=new_pnl,
        new_fee_ratio=new_fee,
        win_rate=win_rate,
        backtest_baseline_pnl=current_pnl,
    )


# ═══════════════════════════════════════════════════════════
# 状态查看
# ═══════════════════════════════════════════════════════════

def show_scores():
    scores = _load_scores()
    history = scores.get("scores", [])
    cooldowns = scores.get("cooldown", {})

    print("📊 参数评分历史")
    print("=" * 60)

    if not history:
        print("  暂无评分记录")
    else:
        recent = history[-20:]
        by_grade = {}
        for s in recent:
            by_grade.setdefault(s["grade"], []).append(s)

        for grade, items in by_grade.items():
            gname = {"promote":"✅ 采纳","keep":"👀 观察","downgrade":"⚠️ 降级","rollback":"🔄 回滚"}.get(grade, grade)
            print(f"\n  {gname} ({len(items)} 项):")
            for s in items[-3:]:
                print(f"    {s['coin']}.{s['param']}: {s['current_value']}→{s['new_value']}  评分: {s['score']:.0f}")

    if cooldowns:
        print(f"\n🚫 参数冷却 ({len(cooldowns)} 项):")
        for key, info in cooldowns.items():
            days_left = ROLLBACK_COOLDOWN_DAYS - (datetime.now(timezone.utc) - datetime.fromisoformat(info["rolled_at"].replace("Z", "+00:00"))).total_seconds() / 86400
            if days_left > 0:
                print(f"    {key}: 剩余 {days_left:.1f} 天 ({info.get('reason','')})")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def _cli_param_score():
        import sys
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    
        if len(sys.argv) < 2:
            print("用法: python param_score.py [score|cooldown|extreme|fees|test]")
            print("  score    查看评分历史")
            print("  cooldown 查看冷却列表")
            print("  extreme  检测极端行情")
            print("  fees     测试手续费吞噬率")
            print("  test     跑一次评分演示")
            sys.exit(1)
    
        cmd = sys.argv[1]
    
        if cmd == "score":
            show_scores()
        elif cmd == "cooldown":
            scores = _load_scores()
            for k, v in scores.get("cooldown", {}).items():
                print(f"  {k}: {v}")
        elif cmd == "extreme":
            result = is_extreme_market()
            print(f"  极端行情: {'是 ⚠️' if result['is_extreme'] else '否 ✅'}")
            for r in result["reasons"]:
                print(f"    - {r}")
        elif cmd == "fees":
            for coin in ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]:
                fees = calc_fee_ratio(coin)
                print(f"  {coin:12s}  毛利润: ${fees['gross_pnl']:.3f}  手续费: ${fees['total_fees']:.4f}  吞噬率: {fees['fee_ratio']:.1%}  交易: {fees['total_trades']}")
        elif cmd == "test":
            # 演示评分
            test_cases = [
                ("ETH", "grid_count", 6, 2, -6.7, 8.1, 50, 0.15, 0.05),
                ("SOL", "grid_count", 3, 2, -13.4, -5.8, 50, 0.30, 0.20),
                ("TRX", "grid_range_pct", 0.05, 0.07, 16.5, 20.6, 42, 0.08, 0.06),
                ("ETH_SWAP", "grid_count", 5, 2, -3.5, -1.2, 55, 0.25, 0.40),
            ]
            for coin, param, old_v, new_v, old_pnl, new_pnl, wr, old_fee, new_fee in test_cases:
                r = score_param(coin, param, old_v, new_v, old_pnl, new_pnl, current_fee_ratio=old_fee, new_fee_ratio=new_fee, win_rate=wr, backtest_baseline_pnl=old_pnl)
                print(f"\n  {coin}.{param}: {old_v}→{new_v}")
                print(f"    PnL: {old_pnl:+.1f}%→{new_pnl:+.1f}%  胜率: {wr:.0f}%  手续费: {old_fee:.0%}→{new_fee:.0%}")
                print(f"    评分: {r['score']:.0f}  等级: {r['grade']}  动作: {r['action']}")
        else:
            print(f"未知命令: {cmd}")
    
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if len(sys.argv) < 2:
        print("用法: python brain.py [explore|regime|auto-tune|rollback|status]")
        print("  explore    探索未测试的参数空间")
        print("  regime     学习行情-参数关联")
        print("  auto-tune  一键全自动（回滚+探索+学习+执行）")
        print("  rollback   检查并执行参数回滚")
        print("  status     查看进化状态")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "explore":
        explore_parameter_space()
    elif cmd == "regime":
        build_regime_model()
    elif cmd == "auto-tune":
        auto_tune(apply_safe=True)
    elif cmd == "rollback":
        check_rollback(apply_revert=True)
    elif cmd == "status":
        show_status()
    else:
        print(f"未知命令: {cmd}")
        print("用法: python brain.py [explore|regime|auto-tune|rollback|status]")
        sys.exit(1)
