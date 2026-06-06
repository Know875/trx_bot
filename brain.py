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

import json, os, sys, time, logging
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


def _optuna_findings(coin, current_params, ind):
    """用 Optuna 联合优化 width×levels，产出与离散扫参同构的 findings。
    返回 None 表示 Optuna 不可用（调用方回退离散扫参）；返回 [] 表示无显著提升。"""
    try:
        from optimize import optimize_grid, current_pnl
    except ImportError:
        return None
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
    from param_score import is_on_cooldown

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
    """
    从日志文件读取某币种在某个时间点之后的 PnL 表现。
    返回 {net_pnl, pnl_pct, settled_count, total_trades}
    
    追踪器报表是多行格式，累计盈亏行不带时间戳。
    我们记录每行最后一个有效时间戳，关联给后续的 PnL 行。
    """
    import re
    log_file = ROOT / f"bot_{coin}.log"
    if not log_file.exists():
        return {"net_pnl": 0, "pnl_pct": 0, "settled_count": 0, "total_trades": 0, "error": "无日志"}

    since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    since_local = (since_dt + timedelta(hours=8)).replace(tzinfo=None)

    # 先找应用时间点的累计盈亏（快照）
    snapshot_pnl = None
    current_pnl = None
    last_ts = None

    try:
        with open(log_file) as f:
            for line in f:
                # 尝试解析时间戳
                try:
                    ts_str = line[:19]
                    last_ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                except:
                    pass  # 无时间戳的行沿用 last_ts

                # 找累计盈亏（可以为缩进行）
                m = re.search(r'累计盈亏:\s+([+-][\d.]+)\s*USDT', line)
                if m:
                    val = float(m.group(1))
                    if last_ts is None or last_ts >= since_local:
                        current_pnl = val
                        if snapshot_pnl is None:
                            snapshot_pnl = val  # 第一条 = 快照

    except Exception as e:
        return {"net_pnl": 0, "pnl_pct": 0, "settled_count": 0, "error": str(e)}

    if current_pnl is None:
        return {"net_pnl": 0, "pnl_pct": 0, "settled_count": 0}

    # 期间净 PnL = 当前累计 - 快照
    net_pnl = current_pnl - (snapshot_pnl or 0)

    base_capitals = {"TRX": 50, "ETH": 3000, "SOL": 4000, "TRX_SWAP": 60, "ETH_SWAP": 120, "SOL_SWAP": 40}
    cap = base_capitals.get(coin, 50)
    pnl_pct = net_pnl / cap * 100 if cap > 0 else 0

    return {
        "net_pnl": round(net_pnl, 3),
        "pnl_pct": round(pnl_pct, 2),
        "settled_count": 0,
        "capital_est": cap,
        "total_trades": 0,
        "snapshot": snapshot_pnl,
        "current": current_pnl,
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
    from param_score import (
        COIN_THRESHOLDS, score_param, calc_fee_ratio,
        add_cooldown, is_on_cooldown, show_scores
    )

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
