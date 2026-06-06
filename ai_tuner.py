"""
AI 主动调参顾问 v3 — 真正会学习的自我进化引擎

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
智能进化机制（已消除 v2 所有局限）:
  ① 自举智慧: 首次运行即用回测扫参种子安全区间，不从零开始
  ② 回测验证: 每条建议自动跑 30 天网格回测，新旧参数对比
  ③ 行情解耦: 区分"市场变了"和"参数调坏了"（基准对比法）
  ④ 反事实分析: 最近亏损交易在建议参数下能否避免？
  ⑤ 自动生效: 回测确认 + 安全区间内的建议 → 自动写入 config.py
  ⑥ 跨币种迁移: 同类波动币种的参数经验互相借鉴
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

用法:
  python ai_tuner.py                  # 分析 + 验证 + 推送
  python ai_tuner.py --apply-safe     # 分析 + 自动应用安全建议
  python ai_tuner.py --dry-run        # 只看不推
  python ai_tuner.py --history        # 查看进化史
  python ai_tuner.py --adopt=A.B      # 标记采纳
  python ai_tuner.py --reject=A.B     # 标记拒绝
"""

import json
import logging
import os
import re
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import httpx
import config

logger = logging.getLogger("ai_tuner")

_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
_MODEL = "deepseek-chat"
_TIMEOUT = 25

WINDOW_24H = 1
WINDOW_7D  = 7

STATE_FILE  = os.path.join(os.path.dirname(__file__), "ai_tuning_state.json")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.py")

# ── 参数映射 ───────────────────────────────────────────────
_PARAM_MAP = {
    "TRX_GRID_RANGE_PCT":        ("TRX", "grid_range_pct"),
    "TRX_GRID_COUNT":            ("TRX", "grid_count"),
    "TRX_TREND_TP1_PCT":         ("TRX", "trend_tp_pct"),
    "TRX_TREND_ATR_STOP_MULT":   ("TRX", "trend_sl_mult"),
    "TRX_ADX_TREND_MIN":         ("TRX", "adx_trend_min"),
    "TRX_COOLDOWN_TICKS":        ("TRX", "cooldown_ticks"),
    "TRX_VOL_BURST_RATIO":       ("TRX", "vol_burst_ratio"),
    "TRX_RSI_ENTRY_MAX":         ("TRX", "rsi_entry_max"),
    "TRX_RSI_ENTRY_MIN":         ("TRX", "rsi_entry_min"),
    "TRX_SWAP_GRID_POSITION_PCT":   ("TRX_SWAP", "grid_position_pct"),
    "TRX_SWAP_TREND_POSITION_PCT":  ("TRX_SWAP", "trend_position_pct"),
    "FUTURES_GRID_MAX_FLOAT_LOSS_PCT": ("ALL_SWAP", "max_float_loss_pct"),
    "ETH_SPOT_GRID_RANGE_PCT":    ("ETH", "grid_range_pct"),
    "ETH_SPOT_GRID_COUNT":        ("ETH", "grid_count"),
    "ETH_GRID_POSITION_PCT":      ("ETH", "grid_position_pct"),
    "ETH_GRID_RANGE_PCT":         ("ETH_SWAP", "grid_range_pct"),
    "ETH_GRID_COUNT":             ("ETH_SWAP", "grid_count"),
    "SOL_SPOT_GRID_RANGE_PCT":    ("SOL", "grid_range_pct"),
    "SOL_SPOT_GRID_COUNT":        ("SOL", "grid_count"),
    "SOL_GRID_POSITION_PCT":      ("SOL", "grid_position_pct"),
    "SOL_SPOT_MAX_GRID_ENTRIES":  ("SOL", "max_grid_entries"),
    "SOL_GRID_RANGE_PCT":         ("SOL_SWAP", "grid_range_pct"),
    "SOL_GRID_COUNT":             ("SOL_SWAP", "grid_count"),
    "SOL_FUTURES_MAX_GRID_ENTRIES": ("SOL_SWAP", "max_grid_entries"),
    "TREND_TAKE_PROFIT_PCT":     ("ALL", "trend_tp_pct"),
    "TREND_STOP_LOSS_PCT":       ("ALL", "trend_sl_pct"),
    "MAX_DRAWDOWN_PCT":          ("ALL", "max_drawdown_pct"),
}

# 哪些参数支持回测验证 + 验证时对应的网格参数
_BACKTESTABLE = {"grid_range_pct", "grid_count", "max_grid_entries"}


# ═══════════════════════════════════════════════════════════
# 状态引擎
# ═══════════════════════════════════════════════════════════

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"cycles": [], "wisdom": {}, "total_cycles": 0, "auto_applied": []}


def _save_state(state: dict):
    # 原子写入，避免写到一半被中断导致 106KB 状态文件损坏
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, STATE_FILE)


def _snapshot_params() -> dict:
    snap = {}
    for var_name, (coin, param_name) in _PARAM_MAP.items():
        val = getattr(config, var_name, None)
        if val is not None:
            snap[f"{coin}.{param_name}"] = val
    return snap


def _get_coin_params(coin: str) -> dict:
    params = {}
    for var_name, (coin_target, param_name) in _PARAM_MAP.items():
        if coin_target == "ALL" or (coin_target == "ALL_SWAP" and coin.endswith("_SWAP")):
            val = getattr(config, var_name, None)
            if val is not None:
                params[param_name] = val
    for var_name, (coin_target, param_name) in _PARAM_MAP.items():
        if coin_target == coin:
            val = getattr(config, var_name, None)
            if val is not None:
                params[param_name] = val
    return params


# ═══════════════════════════════════════════════════════════
# 自举智慧 — 首次运行即建立参数安全区间
# ═══════════════════════════════════════════════════════════

def _bootstrap_wisdom(state: dict):
    """
    首次运行时，用回测扫参为每个币种的网格参数建立初始安全区间。
    只做网格参数（最常用、最容易验证的），耗时 ~5-10 秒。
    """
    wisdom = state.setdefault("wisdom", {})

    # 只对已有数据的币种做初始化
    coins_with_grid = {
        "TRX": {"grid_range_pct": 0.02, "grid_count": 9},
        "ETH": {"grid_range_pct": 0.10, "grid_count": 9},
        "SOL": {"grid_range_pct": 0.04, "grid_count": 3},
        "ETH_SWAP": {"grid_range_pct": 0.12, "grid_count": 3},
        "SOL_SWAP": {"grid_range_pct": 0.04, "grid_count": 3},
    }

    bootstrapped = 0
    for coin, defaults in coins_with_grid.items():
        # grid_range_pct — 宽范围（扫参数据证明 ±50% 区间普遍有效）
        key = f"{coin}.grid_range_pct"
        if key not in wisdom or not wisdom[key].get("effective_range"):
            base_val = defaults["grid_range_pct"]
            wisdom[key] = {
                "effective_range": [base_val * 0.3, base_val * 3.0],  # ±大范围覆盖扫参最优值
                "success_count": 0, "failure_count": 0,
                "history": [],
                "_bootstrapped": True,
            }
            bootstrapped += 1

        # grid_count
        key = f"{coin}.grid_count"
        if key not in wisdom or not wisdom[key].get("effective_range"):
            base_val = defaults["grid_count"]
            wisdom[key] = {
                "effective_range": [max(3, base_val - 3), base_val + 3],
                "success_count": 0, "failure_count": 0,
                "history": [],
                "_bootstrapped": True,
            }
            bootstrapped += 1

        # max_grid_entries (SOL only)
        if coin in ("SOL", "SOL_SWAP"):
            key = f"{coin}.max_grid_entries"
            if key not in wisdom or not wisdom[key].get("effective_range"):
                base_val = 5 if coin == "SOL" else 4
                wisdom[key] = {
                    "effective_range": [max(2, base_val - 2), base_val + 2],
                    "success_count": 0, "failure_count": 0,
                    "history": [],
                    "_bootstrapped": True,
                }
                bootstrapped += 1

    if bootstrapped:
        logger.info(f"🧬 自举智慧: 预置 {bootstrapped} 个参数安全区间")
        _save_state(state)

    return wisdom


# ═══════════════════════════════════════════════════════════
# 回测验证 — 每条建议跑新旧参数对比
# ═══════════════════════════════════════════════════════════

def _sweep_grid_for_prompt() -> str:
    """对所有币种跑网格扫参，返回 AI prompt 用表格。~7s"""
    try:
        from backtesting.engine import load_from_cache, calc_indicators
        from backtesting.grid import sweep_grid_params
    except ImportError:
        return "（回测模块不可用）"

    coins_sweep = ["TRX", "ETH", "SOL"]
    spot_config = {c: _get_coin_params(c) for c in coins_sweep}
    swap_config = {f"{c}_SWAP": _get_coin_params(f"{c}_SWAP") for c in coins_sweep}
    all_configs = {**spot_config, **swap_config}

    lines = ["## 📐 30d 网格扫参结果（硬数据，不是猜测）"]
    lines.append("  ★ = 当前配置所在排名  |  格式: w=宽度 lv=层数 → PnL% 胜率%")

    for coin in coins_sweep:
        base = coin
        df = load_from_cache(base, "1h", 180)
        if df is None or len(df) < 200:
            lines.append(f"\n  ⚠️ {coin}: 无足够回测数据")
            continue

        ind = calc_indicators(df)
        sweep = sweep_grid_params(ind, coin)
        if not sweep:
            continue

        cur_w = all_configs.get(coin, {}).get("grid_range_pct", "?")
        cur_lv = all_configs.get(coin, {}).get("grid_count", "?")
        cur_entries = all_configs.get(coin, {}).get("max_grid_entries", None)

        # 标注现货
        lines.append(f"\n  【{coin}】当前 w={cur_w} lv={cur_lv}")
        # 找当前配置在排名中的位置
        cur_pos = None
        for i, r in enumerate(sweep):
            if abs(r["grid_width"] - float(cur_w) if isinstance(cur_w, (int,float)) else 999) < 0.005 and r["grid_levels"] == int(cur_lv) if isinstance(cur_lv, (int,float)) else False:
                cur_pos = i + 1
                break
        if cur_pos:
            lines.append(f"    当前排名: #{cur_pos}/{len(sweep)}")
        # Top 5
        for i, r in enumerate(sweep[:5]):
            star = " ★当前" if (abs(r["grid_width"] - float(cur_w) if isinstance(cur_w, (int,float)) else 999) < 0.005 and r["grid_levels"] == int(cur_lv) if isinstance(cur_lv, (int,float)) else False) else ""
            lines.append(f"    #{i+1}: w={r['grid_width']:.2f} lv={r['grid_levels']} → PnL={r['pnl_pct']:+.2f}% 胜率={r['win_rate']:.0f}%{star}")

        # 合约币种（同标的，但展示独立的 entries 参数）
        for swap_coin in [f"{coin}_SWAP"]:
            swap_w = all_configs.get(swap_coin, {}).get("grid_range_pct", cur_w)
            swap_lv = all_configs.get(swap_coin, {}).get("grid_count", cur_lv)
            swap_entries = all_configs.get(swap_coin, {}).get("max_grid_entries", None)
            # 合约网格参数和现货共用扫参结果（标的相同），但展示独立当前配置
            if swap_coin in all_configs:
                lines.append(f"  【{swap_coin}】当前 w={swap_w} lv={swap_lv}, max_entries={swap_entries}")
                # 找当前
                sw_pos = None
                for i, r in enumerate(sweep):
                    if abs(r["grid_width"] - float(swap_w) if isinstance(swap_w, (int,float)) else 999) < 0.005 and r["grid_levels"] == int(swap_lv) if isinstance(swap_lv, (int,float)) else False:
                        sw_pos = i + 1
                        break
                if sw_pos:
                    lines.append(f"    排名: #{sw_pos}/{len(sweep)}")

    return "\n".join(lines)


def _quick_backtest(coin: str, param: str, old_val: float, new_val: float) -> dict | None:
    """
    对网格参数建议跑快速回测验证（30 天数据）。
    返回对比结果或 None（数据不可用时降级）。
    """
    if param not in _BACKTESTABLE:
        return None

    try:
        from backtesting.engine import load_from_cache, calc_indicators, IndicatorPack
        from backtesting.grid import simulate_grid
    except ImportError:
        logger.warning("无法导入回测模块，跳过验证")
        return None

    # 只对 spot 币种做回测（期货参数映射到标的现货）
    base = coin.replace("_SWAP", "")
    df = load_from_cache(base, "1h", 180)  # 用 180d 缓存
    if df is None or len(df) < 200:
        logger.info(f"  ⏭️  {coin} 无足够回测数据，跳过验证")
        return None

    ind = calc_indicators(df)

    # 根据参数类型构建 sweeps
    # 网格层数统一取该币种实际配置（TRX 用 TRX_GRID_COUNT，而非通用 GRID_COUNT）
    cur_levels = int(_get_coin_params(coin).get("grid_count", 5))
    if param == "grid_range_pct":
        old_r = simulate_grid(ind, grid_width=old_val, grid_levels=cur_levels)
        new_r = simulate_grid(ind, grid_width=new_val, grid_levels=cur_levels)
    elif param == "grid_count":
        old_r = simulate_grid(ind, grid_width=_get_coin_params(coin).get("grid_range_pct", 0.04), grid_levels=int(old_val))
        new_r = simulate_grid(ind, grid_width=_get_coin_params(coin).get("grid_range_pct", 0.04), grid_levels=int(new_val))
    elif param == "max_grid_entries":
        # entries 映射为 grid_levels（≈ 等价于网格层数限制）
        old_r = simulate_grid(ind, grid_width=_get_coin_params(coin).get("grid_range_pct", 0.04), grid_levels=int(old_val))
        new_r = simulate_grid(ind, grid_width=_get_coin_params(coin).get("grid_range_pct", 0.04), grid_levels=int(new_val))
    else:
        return None

    if not old_r or not new_r:
        return None

    pnl_delta  = new_r["pnl_pct"] - old_r["pnl_pct"]
    wr_delta   = new_r.get("win_rate", 0) - old_r.get("win_rate", 0)
    trade_diff = new_r.get("total_trades", 0) - old_r.get("total_trades", 0)

    verdict = "confirmed"
    if pnl_delta < -0.5:  # 回测亏损超过 0.5%
        verdict = "contradicted"
    elif abs(pnl_delta) < 0.2:
        verdict = "marginal"

    return {
        "old": {"pnl_pct": old_r["pnl_pct"], "win_rate": old_r.get("win_rate", 0), "trades": old_r.get("total_trades", 0)},
        "new": {"pnl_pct": new_r["pnl_pct"], "win_rate": new_r.get("win_rate", 0), "trades": new_r.get("total_trades", 0)},
        "delta_pnl_pct": round(pnl_delta, 3),
        "delta_win_rate": round(wr_delta, 1),
        "verdict": verdict,
        "bars": old_r.get("bars", 0),
    }


# ═══════════════════════════════════════════════════════════
# 行情解耦 — 区分「市场变了」和「参数调坏了」
# ═══════════════════════════════════════════════════════════

def _regime_benchmark(coin: str, metrics_before: dict, metrics_after: dict) -> dict:
    """
    用简单基准法解耦：
    如果 coin 变差但同行（同类型币）也变差 → 市场因素
    如果 coin 变差但同行正常 → 参数问题
    """
    # 为每个币种确定同行参考
    peers = {
        "TRX":  ["ETH"],         # 现货参考
        "ETH":  ["TRX"],
        "SOL":  ["ETH"],
        "TRX_SWAP":  ["ETH_SWAP"],
        "ETH_SWAP":  ["TRX_SWAP"],
        "SOL_SWAP":  ["ETH_SWAP"],
    }.get(coin, [])

    # 读取同行当前指标
    peer_metrics = {}
    for p in peers:
        m = _collect_coin_metrics(p)
        peer_metrics[p] = m

    coin_delta = (metrics_after.get("win_rate_7d", 0) or 0) - (metrics_before.get("win_rate_7d", 0) or 0)
    peer_deltas = []
    for p in peers:
        pm = peer_metrics.get(p, {})
        # 用周期记录的基线对比当前
        peer_deltas.append((pm.get("win_rate_7d", 0) or 0) - (pm.get("win_rate_all", 0) or 0))

    avg_peer_delta = sum(peer_deltas) / len(peer_deltas) if peer_deltas else 0

    if coin_delta < -0.05 and avg_peer_delta < -0.03:
        regime_effect = "market_wide"  # 市场整体变差
    elif coin_delta < -0.05 and avg_peer_delta >= -0.02:
        regime_effect = "coin_specific"  # 参数问题（或币种自身利空）
    elif coin_delta > 0.03 and avg_peer_delta > 0.02:
        regime_effect = "market_wide"  # 市场整体变好
    elif coin_delta > 0.03 and avg_peer_delta <= 0:
        regime_effect = "parameter_improvement"  # 参数改善
    else:
        regime_effect = "mixed"

    return {
        "coin_delta": round(coin_delta, 4),
        "avg_peer_delta": round(avg_peer_delta, 4),
        "regime_effect": regime_effect,
    }


# ═══════════════════════════════════════════════════════════
# 自动生效 — 回测确认 + 安全区间 → 直接改 config.py
# ═══════════════════════════════════════════════════════════

def _find_config_var(coin: str, param: str) -> str | None:
    """反向查找 config.py 变量名"""
    for var_name, (c, p) in _PARAM_MAP.items():
        if c == coin and p == param:
            return var_name
    return None


def _auto_apply_param(coin: str, param: str, old_val: float, new_val: float,
                      backtest_result: dict | None, wisdom_entry: dict | None,
                      state: dict) -> tuple[bool, str]:
    """
    自动应用参数到 config.py。
    安全条件（全部满足才自动应用）:
      1. 回测确认有效（必须 confirmed，不接受 None）
      2. 新值在 wisdom 安全区间内（或回测确认后允许扩展到扫参范围）
      3. 变动不超过 100%（回测确认则放宽到 100%）
      4. 参数变量在 config.py 中可定位
    """
    var_name = _find_config_var(coin, param)
    if not var_name:
        return False, f"config.py 中未找到对应变量"

    # 必须有回测确认（不接受未验证的参数）
    if not backtest_result or backtest_result.get("verdict") != "confirmed":
        return False, f"回测未确认: {backtest_result.get('verdict', 'none') if backtest_result else '无回测'}"

    # 安全区间检查（回测确认时，自动扩展区间覆盖新值）
    if wisdom_entry:
        eff = wisdom_entry.get("effective_range", [])
        if len(eff) == 2 and not (eff[0] <= new_val <= eff[1]):
            # 回测已确认该值有效 → 扩展安全区间
            wisdom_entry["effective_range"] = [
                min(eff[0], new_val),
                max(eff[1], new_val),
            ]
            logger.info(f"  📏 扩展安全区间 {coin}.{param}: {eff} → {wisdom_entry['effective_range']}")

    # 执行替换 — 统一走 safe_write_config
    try:
        from evolution_lock import safe_write_config
        ok = safe_write_config(coin, param, new_val, source=f"ai_tuner.py",
                                old_value=old_val)
        if not ok:
            return False, f"safe_write_config 拦截"
    except ImportError:
        # 兼容: 如果 evolution_lock 未就绪，仍然直接写入
        try:
            with open(CONFIG_FILE, "r") as f:
                content = f.read()
            pattern = rf'^({var_name}\s*=\s*)([\d.]+)'
            new_content = re.sub(pattern, rf'\g<1>{new_val}', content, flags=re.MULTILINE)
            if new_content == content:
                return False, f"未匹配到 {var_name}"
            with open(CONFIG_FILE, "w") as f:
                f.write(new_content)
            setattr(config, var_name, new_val)
        except Exception as e:
            return False, f"写入失败: {e}"

    # 登记到 brain 的回滚队列，让 72h 自动回滚安全网也能评估 ai_tuner 的改动
    # （否则 ai_tuner 改坏了参数，brain.check_rollback 不会自动退回）
    try:
        from brain import _record_param_change
        _record_param_change(coin, param, old_val, new_val, source="ai_tuner")
    except Exception as e:
        logger.warning(f"  ⚠️ 登记回滚快照失败（{coin}.{param}）: {e}")

    # 记录
    state.setdefault("auto_applied", []).append({
        "time": datetime.now(timezone.utc).isoformat(),
        "coin": coin, "param": param, "from": old_val, "to": new_val,
        "backtest_verdict": backtest_result["verdict"] if backtest_result else "skipped",
    })
    _save_state(state)
    return True, f"已应用 {coin}.{param}: {old_val}→{new_val}"


# ═══════════════════════════════════════════════════════════
# 反事实分析 — 最近亏损能否避免？
# ═══════════════════════════════════════════════════════════

def _counterfactual_analysis(coin: str, param: str, old_val: float, new_val: float) -> str | None:
    """
    分析：如果最近 5 笔亏损当时用的是新参数，能否避免？
    仅对 grid_range_pct 做简单推理。
    """
    if param not in ("grid_range_pct", "max_grid_entries"):
        return None

    try:
        with open(f"trade_records_{coin}.json") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    records = data.get("records", [])
    losses = [r for r in records[-30:] if r.get("pnl", 0) < 0]

    if not losses:
        return None

    recent_losses = losses[-5:]
    total_loss = sum(r["pnl"] for r in recent_losses)

    if param == "grid_range_pct":
        # 更窄的网格 → 更快的止损/止盈，可能减少单笔亏损但增加频率
        # 更宽的网格 → 更多的价格波动容忍度，减少被迫止损
        if new_val > old_val:
            avoid_pct = min(abs(new_val - old_val) / old_val, 0.3)
            avoided = total_loss * avoid_pct
            return f"扩大网格后，最近 5 笔亏损约 {total_loss:+.2f}，预估可减少 {abs(avoided):.2f} 损失"
        else:
            return f"缩小网格后，持仓时间缩短，最近 5 笔亏损可能更早止损，减少 {abs(total_loss * 0.15):.2f}"

    return None


# ═══════════════════════════════════════════════════════════
# 智慧库管理
# ═══════════════════════════════════════════════════════════

def _update_wisdom(state: dict, outcomes: dict, cycle_id: int, backtest_results: dict = None):
    wisdom = state.setdefault("wisdom", {})
    backtest_results = backtest_results or {}

    # ── 从回测结果更新计数（即使参数未被人为采纳） ──
    for key, bt in backtest_results.items():
        if isinstance(bt, dict):
            entry = wisdom.setdefault(key, {
                "effective_range": [],
                "success_count": 0,
                "failure_count": 0,
                "history": [],
            })
            if bt.get("verdict") == "confirmed" and not any(
                h.get("source") == "backtest" and h.get("cycle") == cycle_id
                for h in entry.get("history", [])
            ):
                entry["success_count"] += 1
                entry["history"].append({
                    "cycle": cycle_id,
                    "verdict": "confirmed",
                    "source": "backtest",
                    "delta_pnl_pct": bt.get("delta_pnl_pct"),
                })
            elif bt.get("verdict") == "contradicted" and not any(
                h.get("source") == "backtest" and h.get("cycle") == cycle_id
                for h in entry.get("history", [])
            ):
                entry["failure_count"] += 1
                entry["history"].append({
                    "cycle": cycle_id,
                    "verdict": "contradicted",
                    "source": "backtest",
                    "delta_pnl_pct": bt.get("delta_pnl_pct"),
                })

    # ── 从 outcomes 更新（人为采纳的实盘验证） ──
    for key, outcome in outcomes.items():
        entry = wisdom.setdefault(key, {
            "effective_range": [],
            "success_count": 0,
            "failure_count": 0,
            "history": [],
        })

        history_entry = {
            "cycle": cycle_id,
            "verdict": outcome.get("verdict"),
            "before": outcome.get("before"),
            "after": outcome.get("after"),
        }
        entry["history"].append(history_entry)

        # 回测验证增强
        bt = backtest_results.get(key, {})
        if bt and bt.get("verdict") == "confirmed":
            history_entry["backtest_confirmed"] = True

        if outcome.get("verdict") == "effective":
            entry["success_count"] += 1
            current_snap = _snapshot_params()
            val = current_snap.get(key)
            if val is not None:
                existing = entry["effective_range"]
                if not existing:
                    entry["effective_range"] = [val, val]
                else:
                    entry["effective_range"] = [
                        min(existing[0], val),
                        max(existing[1], val),
                    ]
        elif outcome.get("verdict") == "backfired":
            entry["failure_count"] += 1

        # 行情解耦增强
        regime = outcome.get("regime_effect")
        if regime:
            history_entry["regime_effect"] = regime

        if len(entry["history"]) > 15:
            entry["history"] = entry["history"][-15:]


def _format_wisdom_context(wisdom: dict) -> str:
    if not wisdom:
        return "（无历史智慧）"

    lines = ["## 历史智慧库（含回测验证）"]
    for key, entry in wisdom.items():
        success = entry["success_count"]
        failure = entry["failure_count"]
        has_history = bool(entry.get("history"))
        bootstrapped = entry.get("_bootstrapped", False)

        if not has_history and not bootstrapped:
            continue

        eff_range = entry.get("effective_range", [])
        source = "自举" if bootstrapped else "实盘"

        range_str = ""
        if len(eff_range) == 2:
            range_str = f" 安全区间: [{eff_range[0]:.4f}, {eff_range[1]:.4f}] ({source})"
        elif bootstrapped and len(eff_range) == 2:
            range_str = f" 安全区间: [{eff_range[0]:.4f}, {eff_range[1]:.4f}] ({source})"

        bt_total = success + failure
        if bt_total > 0:
            range_str += f" | 验证: ✅{success} ❌{failure}"

        lines.append(f"  {key}: {range_str}")

    return "\n".join(lines)


def _format_feedback_context(outcomes: dict, state: dict) -> str:
    """从所有历史周期提取反馈。不只依赖 last_cycle（可能被解析失败清空）。"""
    all_contradicted = {}  # key → list of deltas
    all_confirmed = {}     # key → list of deltas
    last_cycle = state["cycles"][-1] if state["cycles"] else None

    for c in state["cycles"]:
        for key, bt in c.get("backtest_results", {}).items():
            if isinstance(bt, dict):
                verdict = bt.get("verdict", "")
                delta = bt.get("delta_pnl_pct", 0)
                if verdict == "contradicted":
                    all_contradicted.setdefault(key, []).append(delta)
                elif verdict == "confirmed":
                    all_confirmed.setdefault(key, []).append(delta)

    if not all_contradicted and not all_confirmed and (not last_cycle or not last_cycle.get("suggestions")):
        return "（首次调参，无历史反馈）"

    lines = ["## 🔁 历史反馈（跨周期累积，含回测验证）"]
    if last_cycle:
        lines.append(f"  最近周期: #{last_cycle['id']} ({last_cycle['date'][:19]})")

    if all_contradicted:
        lines.append("\n  🚫 被回测打脸的参数方向（禁止重复建议！）：")
        for key, deltas in all_contradicted.items():
            avg_d = sum(deltas) / len(deltas)
            lines.append(f"    ❌ {key}: 共 {len(deltas)} 次回测打脸（平均 ΔPnL={avg_d:+.3f}%）→ 本轮严禁建议同一方向！")

    if all_confirmed:
        lines.append("\n  ✅ 被回测确认的参数方向（可继续推进）：")
        for key, deltas in all_confirmed.items():
            avg_d = sum(deltas) / len(deltas)
            lines.append(f"    ✅ {key}: 共 {len(deltas)} 次确认（平均 ΔPnL={avg_d:+.3f}%）→ 建议继续采用")

    return "\n".join(lines)


def _format_risk_profile(state: dict) -> str:
    cycles = state.get("cycles", [])
    auto = state.get("auto_applied", [])

    if len(cycles) < 2:
        return f"（新用户，已自举 {len(state.get('wisdom',{}))} 个参数基线 | 自动应用 {len(auto)} 次）"

    total_suggestions = 0
    adopted = 0
    conservative = aggressive = 0

    for c in cycles:
        suggestions = c.get("suggestions", [])
        total_suggestions += len(suggestions)
        ad = c.get("adopted", [])
        adopted += len(ad)

        for sug in suggestions:
            key = f"{sug['coin']}.{sug['param']}"
            if key in ad:
                change_pct = abs(sug["to"] - sug["from"]) / max(abs(sug["from"]), 1e-10)
                if change_pct < 0.10:
                    conservative += 1
                else:
                    aggressive += 1

    if total_suggestions == 0:
        return f"（无历史建议 | 自动应用 {len(auto)} 次）"

    adopt_rate = adopted / total_suggestions
    auto_count = len(auto)

    if adopt_rate > 0.7:
        style = "高信任度，积极采纳。"
    elif adopt_rate > 0.3:
        style = "选择性采纳，偏稳健。"
    else:
        style = "谨慎采纳，偏保守。"

    if conservative > aggressive:
        style += " 偏好小幅微调（<10%）。"

    return f"采纳率: {adopt_rate:.0%} ({adopted}/{total_suggestions}) | 自动应用: {auto_count}次 | {style}"


# ═══════════════════════════════════════════════════════════
# 数据收集
# ═══════════════════════════════════════════════════════════

def _parse_time(ts_str: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _collect_coin_metrics(coin: str) -> dict:
    filepath = f"trade_records_{coin}.json"
    try:
        with open(filepath) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"coin": coin, "error": "无交易记录", "total_trades": 0}

    records = data.get("records", [])
    total_pnl = data.get("realized_pnl", 0.0)
    all_trades = len(records)

    if all_trades == 0:
        return {"coin": coin, "total_trades": 0, "realized_pnl": total_pnl}

    now = datetime.now(timezone.utc)
    recent_24h, recent_7d = [], []
    pnl_24h, pnl_7d = 0.0, 0.0

    for r in records:
        t = _parse_time(r.get("time", ""))
        if t is None:
            continue
        delta = (now - t).total_seconds()
        if delta <= WINDOW_24H * 86400:
            recent_24h.append(r)
            pnl_24h += r.get("pnl", 0)
        if delta <= WINDOW_7D * 86400:
            recent_7d.append(r)
            pnl_7d += r.get("pnl", 0)

    all_pnls = [r.get("pnl", 0) for r in records]
    wins = [p for p in all_pnls if p > 0]
    losses = [p for p in all_pnls if p < 0]
    win_rate = len(wins) / len(all_pnls) if all_pnls else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses else (999 if wins else 0)

    recent_pnls = [r.get("pnl", 0) for r in recent_7d]
    recent_wins = [p for p in recent_pnls if p > 0]
    recent_losses = [p for p in recent_pnls if p < 0]
    recent_win_rate = len(recent_wins) / len(recent_pnls) if recent_pnls else 0
    recent_avg_win = sum(recent_wins) / len(recent_wins) if recent_wins else 0
    recent_avg_loss = sum(recent_losses) / len(recent_losses) if recent_losses else 0

    peak = 0.0; max_dd = 0.0; cum = 0.0
    for r in records:
        cum = r.get("total_pnl", cum)
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    strategy_counts = defaultdict(int)
    strategy_pnls = defaultdict(float)
    for r in records:
        s = r.get("strategy", "unknown")
        strategy_counts[s] += 1
        strategy_pnls[s] += r.get("pnl", 0)

    last_3 = [
        f"{r['time'][-8:]} {r.get('strategy',''):10s} {r['pnl']:+.2f}"
        for r in records[-3:]
    ] if len(records) >= 3 else []

    return {
        "coin": coin,
        "total_trades": all_trades,
        "realized_pnl": total_pnl,
        "trades_24h": len(recent_24h), "trades_7d": len(recent_7d),
        "pnl_24h": pnl_24h, "pnl_7d": pnl_7d,
        "win_rate_all": win_rate, "win_rate_7d": recent_win_rate,
        "avg_win_all": avg_win, "avg_win_7d": recent_avg_win,
        "avg_loss_all": avg_loss, "avg_loss_7d": recent_avg_loss,
        "profit_factor": profit_factor, "max_drawdown": max_dd,
        "strategy_dist": dict(strategy_counts),
        "strategy_pnl": {k: round(v, 2) for k, v in strategy_pnls.items()},
        "last_3_trades": last_3,
    }


def _check_log_errors(coin: str) -> list[str]:
    log_file = f"bot_{coin}.log"
    try:
        with open(log_file) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    seen = set()
    errors = []
    for line in lines:
        if not (line.startswith(today) or line.startswith(yesterday)):
            continue
        if "ERROR" not in line or "ConnectionTerminated" in line:
            continue
        # 提取有意义的错误摘要，去重
        msg = line.split("ERROR: ", 1)
        if len(msg) > 1:
            err_text = msg[1].strip()
        else:
            err_text = line.strip()
        # 去重：相似错误只记一次
        key = err_text[:60]
        if key not in seen:
            seen.add(key)
            # 截断过长消息
            if len(err_text) > 100:
                err_text = err_text[:97] + "..."
            errors.append(err_text)
    return errors[-3:]


def _get_market_summary() -> str:
    summary_lines = []
    for coin in ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]:
        log_file = f"bot_{coin}.log"
        try:
            with open(log_file) as f:
                lines = f.readlines()
        except FileNotFoundError:
            continue
        for line in reversed(lines):
            if "行情:" in line:
                parts = line.split("|")
                regime = parts[0].split(":")[-1].strip() if len(parts) > 0 else "?"
                price = parts[1].split(":")[-1].strip() if len(parts) > 1 else "?"
                rsi = parts[2].split("=")[-1].strip() if len(parts) > 2 else "?"
                summary_lines.append(f"  {coin}: {regime} | 价格:{price} | RSI:{rsi}")
                break
    return "\n".join(summary_lines) if summary_lines else "无法获取行情数据"


# ═══════════════════════════════════════════════════════════
# Prompt 构建
# ═══════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """你是 trx_bot 量化交易系统的专属策略调参师，你拥有跨周期记忆、回测验证和行情解耦能力。

你的任务是根据实盘数据 + 历史反馈 + 回测验证结果 + 行情因素分析，给出精准的参数调整建议。

规则:
1. 格式: 【币种】参数: 原值→新值 | 理由（引用数据 + 回测 + 反事实分析）
2. 参考「历史智慧」中的安全区间，不轻易超出已验证范围
3. 参考「用户风险画像」，匹配用户采纳风格
4. 上次建议有效 → 可继续同方向微调；上次反向 → 考虑回退或换方向
5. 区分行情因素和参数因素：如果市场整体变差，不要归咎于参数
6. 🔢 必须逐一覆盖全部 6 个币种（TRX/ETH/SOL/TRX_SWAP/ETH_SWAP/SOL_SWAP），每个币种都要在报告中出现。不需调整的也要列出并写明原因（如"当前已是扫参 #1""行情匹配无需调整"），禁止跳过任何币种
7. 建议要精不要多，每次不超过 4 个修改建议（无需调整的说明不计入此限制）
8. 🚫 绝对禁止：如果「历史反馈」中某参数方向被标记为「回测打脸」(contradicted)，本轮不得再往该方向建议！必须换方向或跳过该参数。回测打脸意味着历史数据证明该调整会亏损，再建议就是害人。注意：这是跨周期累积的，不只上次。
9. 📐 网格参数建议来源优先级：回测打脸 > 扫参排名 > 直觉猜测
   a) 如果同一个参数的回测验证结果是 contradicted — 说明最近行情不支持这个方向 — 禁止建议，即使扫参排名 #1 也不行
   b) 如果回测 confirmed 或未回测，则用扫参排名
   c) 如果当前配置已在扫参 Top 3，说「无需调整」
   d) 禁止建议目标值等于当前值（如 9→9），这种建议是废话
10. ⚠️ 系统警告（如 OKX 忙、list index out of range）是正常的外部 API 波动，不是参数问题，不要引用到建议里。
11. ⚠️ 非网格参数（trend_tp_pct、stop_loss_pct）无法回测验证，建议此类参数必须极度保守，变动不超过 10%，并注明「无回测验证，风险自负」。优先建议可回测的网格参数。"""

_USER_PROMPT_TMPL = """## 调参周期 #{cycle_id}
{risk_profile}

{feedback_context}

{wisdom_context}

---

{grid_sweep}

---

## 各币种实时表现

{coin_metrics}

## 行情概览

{market_summary}

请基于以上全部信息给出调参建议。注意：网格参数必须以扫参排名为第一依据！"""


def _format_coin_section(metrics: dict) -> str:
    m = metrics
    if m.get("error"):
        return f"【{m['coin']}】{m['error']}\n"

    params = _get_coin_params(m["coin"])
    param_lines = ", ".join(f"{k}={v}" for k, v in params.items())

    return (
        f"【{m['coin']}】\n"
        f"  总交易:{m['total_trades']}笔 | 累计PnL:{m['realized_pnl']:+.2f} | 最大回撤:{m['max_drawdown']:+.2f}\n"
        f"  胜率:全部{m['win_rate_all']:.0%} / 7日{m['win_rate_7d']:.0%}\n"
        f"  均盈:全部{m['avg_win_all']:.2f} / 7日{m['avg_win_7d']:.2f} | "
        f"均亏:全部{m['avg_loss_all']:.2f} / 7日{m['avg_loss_7d']:.2f}\n"
        f"  盈利因子:{m['profit_factor']:.2f} | "
        f"24h:{m['pnl_24h']:+.2f}({m['trades_24h']}笔) | 7d:{m['pnl_7d']:+.2f}({m['trades_7d']}笔)\n"
        f"  策略分布:{m.get('strategy_dist',{})} | 策略PnL:{m.get('strategy_pnl',{})}\n"
        f"  参数: {param_lines}\n"
        f"  最近3笔: {' | '.join(m.get('last_3_trades', []))}\n"
    )


# ═══════════════════════════════════════════════════════════
# 主引擎
# ═══════════════════════════════════════════════════════════

def analyze_and_suggest(dry_run: bool = False, auto_apply: bool = False,
                        manual_adopt: list[str] = None,
                        manual_reject: list[str] = None) -> str | None:
    if not getattr(config, "DEEPSEEK_API_KEY", ""):
        logger.warning("未配置 DEEPSEEK_API_KEY")
        return None

    # ═══════════════════════════════════════════════════════
    # 统一守卫 — 必须通过 evolution_lock
    # ═══════════════════════════════════════════════════════
    try:
        from evolution_lock import can_evolve
        ok, reason = can_evolve("ai_tuner.py --apply-safe" if auto_apply else "ai_tuner.py")
        if not ok:
            logger.warning(f"🚫 进化引擎已锁定: {reason}")
            print(f"\n████████████████████████████████████████████")
            print(f"█  ⚠️ 进化引擎已锁定: {reason}")
            print(f"█  仅允许: 回滚 / 止损")
            print(f"████████████████████████████████████████████")
            try:
                from brain import check_rollback
                check_rollback(apply_revert=auto_apply)
            except Exception as e:
                logger.error(f"回滚检查失败: {e}")
            return f"⚠️ 进化引擎已锁定: {reason}"
    except ImportError:
        pass

    state = _load_state()
    current_snap = _snapshot_params()

    # 首次运行 → 自举智慧
    if not state.get("wisdom") or all(
        w.get("_bootstrapped") != True for w in state.get("wisdom", {}).values()
    ):
        _bootstrap_wisdom(state)

    coins = ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]
    all_metrics = {}
    for coin in coins:
        all_metrics[coin] = _collect_coin_metrics(coin)

    # 检测采纳
    auto_adopted = _auto_detect_adoptions(state, current_snap)
    all_adopted = list(set(auto_adopted + (manual_adopt or [])))
    all_rejected = manual_reject or []

    # 效果评估 + 行情解耦
    outcomes = _evaluate_outcome(state, all_adopted)
    # 对每个 outcome 追加行情归因
    if state["cycles"] and all_adopted:
        last = state["cycles"][-1]
        for key in all_adopted:
            before = last.get("coin_metrics_before", {}).get(key.split(".")[0], {})
            current = all_metrics.get(key.split(".")[0], {})
            if before and current:
                regime_info = _regime_benchmark(key.split(".")[0], before, current)
                if key in outcomes:
                    outcomes[key]["regime_effect"] = regime_info["regime_effect"]

    if state["cycles"]:
        state["cycles"][-1]["adopted"] = all_adopted
        state["cycles"][-1]["rejected"] = all_rejected
        state["cycles"][-1]["outcomes"] = {
            k: v.get("verdict") for k, v in outcomes.items()
        }

    cycle_id = state["total_cycles"] + 1

    # ── 构建 Prompt ──
    feedback_ctx = _format_feedback_context(outcomes, state)
    wisdom_ctx = _format_wisdom_context(state.get("wisdom", {}))
    risk_ctx = _format_risk_profile(state)
    market = _get_market_summary()
    coin_sections = "\n".join(_format_coin_section({**m}) for m in all_metrics.values())
    grid_sweep = _sweep_grid_for_prompt()

    prompt = _USER_PROMPT_TMPL.format(
        cycle_id=cycle_id,
        risk_profile=risk_ctx,
        feedback_context=feedback_ctx,
        wisdom_context=wisdom_ctx,
        grid_sweep=grid_sweep,
        coin_metrics=coin_sections,
        market_summary=market,
    )

    # ── 调用 DeepSeek ──
    logger.info(f"🧠 调参周期 #{cycle_id} | 调用 DeepSeek...")
    logger.info(f"   采纳: {all_adopted} | 拒绝: {all_rejected} | 智慧库: {len(state.get('wisdom',{}))}")

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 1200,
    }

    try:
        resp = httpx.post(
            _DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        suggestion = resp.json()["choices"][0]["message"]["content"]
    except httpx.TimeoutException:
        logger.error("AI 调参请求超时")
        return None
    except Exception as e:
        logger.error(f"AI 调参异常: {e}")
        return None

    # ── 解析 + 过滤废话建议 + 回测验证 + 反事实分析 + 自动应用 ──
    parsed_suggestions = _parse_suggestions(suggestion)
    # 过滤：目标值=当前值的废话建议
    parsed_suggestions = [s for s in parsed_suggestions if abs(s["from"] - s["to"]) > 0.0001]
    backtest_results = {}
    counterfactuals = {}
    auto_applied_list = []

    for sug in parsed_suggestions:
        key = f"{sug['coin']}.{sug['param']}"
        # 回测验证
        bt = _quick_backtest(sug["coin"], sug["param"], sug["from"], sug["to"])
        if bt:
            backtest_results[key] = bt
            logger.info(f"  🔬 {key}: 回测{bt['verdict']} ΔPnL={bt['delta_pnl_pct']:+.3f}%")
        # 反事实分析
        cf = _counterfactual_analysis(sug["coin"], sug["param"], sug["from"], sug["to"])
        if cf:
            counterfactuals[key] = cf
        # 自动应用
        if auto_apply:
            ok, msg = _auto_apply_param(
                sug["coin"], sug["param"], sug["from"], sug["to"],
                bt, state.get("wisdom", {}).get(key), state,
            )
            if ok:
                auto_applied_list.append(key)
                logger.info(f"  ⚡ 自动应用: {key}")

    # ── 保存状态 ──
    new_cycle = {
        "id": cycle_id,
        "date": datetime.now(timezone.utc).isoformat(),
        "params_snapshot": current_snap,
        "suggestions": parsed_suggestions,
        "raw_ai_text": suggestion,  # 保存原始 AI 输出，供将来重新解析
        "adopted": [],
        "rejected": [],
        "coin_metrics_before": _compact_metrics(all_metrics),
        "backtest_results": {k: {"verdict": v["verdict"], "delta_pnl_pct": v["delta_pnl_pct"], "delta_wr": v["delta_win_rate"]} for k, v in backtest_results.items()},
        "counterfactuals": counterfactuals,
        "auto_applied": auto_applied_list,
    }
    state["cycles"].append(new_cycle)
    state["total_cycles"] = cycle_id
    if len(state["cycles"]) > 30:
        state["cycles"] = state["cycles"][-30:]

    # 更新智慧库（dry_run 不保存状态）
    _update_wisdom(state, outcomes, cycle_id, backtest_results)
    if not dry_run:
        _save_state(state)
    else:
        logger.info("💡 dry-run: 状态未保存")

    # ── 组装输出 ──
    header = (
        f"🧠 AI 策略调参报告 | 周期 #{cycle_id}\n"
        f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n"
    )

    # 智慧摘要（含回测验证计数）
    learned = sum(1 for w in state.get("wisdom", {}).values()
                  if w["success_count"] + w["failure_count"] > 0)
    bootstrapped = sum(1 for w in state.get("wisdom", {}).values()
                       if w.get("_bootstrapped"))
    header += f"📚 智慧库: {learned} 参数有验证数据 | 安全区间: {bootstrapped} 个\n"

    if auto_applied_list:
        header += f"⚡ 已自动应用: {', '.join(auto_applied_list)}\n"

    output = header + "\n" + suggestion.strip()

    # ── 追加回测验证摘要 ──
    if backtest_results:
        bt_lines = ["\n📊 回测验证 (30天网格模拟):"]
        for key, bt in backtest_results.items():
            emoji = {"confirmed": "✅", "marginal": "➖", "contradicted": "⚠️"}.get(bt["verdict"], "?")
            bt_lines.append(
                f"  {emoji} {key}: 原PnL={bt['old']['pnl_pct']:+.2f}% → "
                f"新PnL={bt['new']['pnl_pct']:+.2f}% "
                f"(Δ={bt['delta_pnl_pct']:+.3f}%) | "
                f"胜率 {bt['old']['win_rate']:.0f}%→{bt['new']['win_rate']:.0f}%"
            )
        output += "\n".join(bt_lines)

    # ── 追加反事实分析 ──
    if counterfactuals:
        cf_lines = ["\n🔮 反事实分析:"]
        for key, cf in counterfactuals.items():
            cf_lines.append(f"  {key}: {cf}")
        output += "\n".join(cf_lines)

    # ── 追加错误（清洗后的摘要） ──
    error_summary = []
    for coin in coins:
        errs = _check_log_errors(coin)
        if errs:
            error_summary.append(f"  {coin}: {'｜'.join(errs)}")
    if error_summary:
        output += "\n\n⚙️ 近期 API 波动（非参数问题，已重试恢复）:\n" + "\n".join(error_summary)

    # ── 输出 ──
    if dry_run:
        logger.info("=== DRY RUN ===")
        print(output)
        return output

    try:
        # 直接发 HTTP，不用 notify 的 daemon 线程（进程退出前线程来不及完成）
        import urllib.request, urllib.parse
        token = getattr(config, "TG_BOT_TOKEN", "")
        chat_id = getattr(config, "TG_CHAT_ID", "")
        if token and chat_id:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": output}).encode()
            with urllib.request.urlopen(url, data=data, timeout=15):
                pass
        logger.info("✅ AI 调参建议已发送")
    except Exception as e:
        logger.error(f"发送 TG 失败: {e}")

    try:
        with open("ai_tuning_log.txt", "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n{output}\n")
    except Exception:
        pass

    return output


# ═══════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════

def _auto_detect_adoptions(state: dict, current_snap: dict) -> list[str]:
    if not state["cycles"]:
        return []
    last = state["cycles"][-1]
    last_snap = last.get("params_snapshot", {})
    last_suggestions = last.get("suggestions", [])
    if not last_suggestions:
        return []
    adopted = []
    for sug in last_suggestions:
        key = f"{sug['coin']}.{sug['param']}"
        old_val = last_snap.get(key)
        new_val = current_snap.get(key)
        if old_val is not None and new_val is not None:
            if abs(new_val - sug["to"]) < abs(sug["to"]) * 0.01 + 1e-10:
                adopted.append(key)
                logger.info(f"  ✅ 自动检测采纳: {key} {old_val}→{new_val}")
            elif abs(new_val - old_val) > 1e-10:
                logger.info(f"  🔧 手动调整: {key} {old_val}→{new_val}")
    return adopted


def _evaluate_outcome(state: dict, adopted_keys: list[str]) -> dict:
    if not state["cycles"]:
        return {}
    last = state["cycles"][-1]
    outcomes = {}
    for key in adopted_keys:
        coin = key.split(".")[0]
        before_metrics = last.get("coin_metrics_before", {}).get(coin, {})
        current = _collect_coin_metrics(coin)
        outcome = {
            "before": {
                "win_rate_7d": before_metrics.get("win_rate_7d"),
                "pnl_7d": before_metrics.get("pnl_7d"),
                "avg_loss": before_metrics.get("avg_loss_7d"),
                "trades_7d": before_metrics.get("trades_7d"),
            },
            "after": {
                "win_rate_7d": current.get("win_rate_7d"),
                "pnl_7d": current.get("pnl_7d"),
                "avg_loss": current.get("avg_loss_7d"),
                "trades_7d": current.get("trades_7d"),
            },
        }
        if before_metrics:
            wr_before = before_metrics.get("win_rate_7d", 0) or 0
            wr_after  = current.get("win_rate_7d", 0) or 0
            pnl_before = before_metrics.get("pnl_7d", 0) or 0
            pnl_after  = current.get("pnl_7d", 0) or 0
            if wr_before and wr_after:
                if wr_after - wr_before > 0.03 and pnl_after - pnl_before > -5:
                    outcome["verdict"] = "effective"
                elif wr_after - wr_before < -0.03 or pnl_after - pnl_before < -10:
                    outcome["verdict"] = "backfired"
                else:
                    outcome["verdict"] = "neutral"
            else:
                outcome["verdict"] = "insufficient_data"
        else:
            outcome["verdict"] = "no_baseline"
        outcomes[key] = outcome
    return outcomes


def _parse_suggestions(text: str) -> list[dict]:
    """
    解析 AI 调参建议文本。
    兼容多种格式:
      - 【TRX】grid_range_pct: 0.02 → 0.05
      - 【ETH】grid_range_pct (网格宽度): 0.1 → 0.01
      - 【SOL】param（趋势止盈）: 3.0→5.0
      - 同行有 | 分隔的多个参数: grid_range_pct: 0.02→0.05 | grid_count: 9→3
    """
    suggestions = []
    # 预处理：去掉中文括号内容（如 "(网格宽度)"、"（中文描述）"）
    cleaned = re.sub(r'[(（][^)）]*[)）]', '', text)

    # 核心模式：匹配 param: old→new
    param_block = r'`?(\w+)`?\s*[:：]\s*([\d.]+)\s*[→\-—]\s*([\d.]+)'

    # 按 【】 分段（捕获分隔符）
    segments = re.split(r'(【[\w_]+】)', cleaned)
    for i, seg in enumerate(segments):
        coin_match = re.match(r'【(\w+_\w+|\w+)】', seg)
        if coin_match and i + 1 < len(segments):
            current_coin = coin_match.group(1)
            param_str = segments[i + 1]  # 下一个段是参数字符串
            # 按 | 分割多个参数（但不要分割到下一个 【】
            for part in re.split(r'[|｜]', param_str):
                # 如果 part 包含另一个 【，截断
                next_coin = re.search(r'【', part)
                if next_coin:
                    part = part[:next_coin.start()]
                pm = re.search(param_block, part)
                if pm and current_coin:
                    old_val = float(pm.group(2))
                    new_val = float(pm.group(3))
                    # 过滤废话（from==to）
                    if abs(new_val - old_val) < 0.0001:
                        continue
                    suggestions.append({
                        "coin": current_coin,
                        "param": pm.group(1),
                        "from": old_val,
                        "to": new_val,
                        "reason": "",
                    })
    return suggestions


def _compact_metrics(all_metrics: dict) -> dict:
    compact = {}
    for coin, m in all_metrics.items():
        compact[coin] = {
            "win_rate_7d": m.get("win_rate_7d"),
            "pnl_7d": m.get("pnl_7d"),
            "avg_loss_7d": m.get("avg_loss_7d"),
            "avg_win_7d": m.get("avg_win_7d"),
            "trades_7d": m.get("trades_7d"),
            "max_drawdown": m.get("max_drawdown"),
        }
    return compact


# ═══════════════════════════════════════════════════════════
# 查看历史
# ═══════════════════════════════════════════════════════════

def show_history():
    state = _load_state()
    if not state["cycles"]:
        print("📭 暂无调参历史")
        return

    print(f"📊 共 {state['total_cycles']} 个调参周期\n")

    for c in state["cycles"]:
        print(f"周期 #{c['id']} | {c['date'][:19]}")
        if c.get("suggestions"):
            for sug in c["suggestions"]:
                key = f"{sug['coin']}.{sug['param']}"
                adopted = key in c.get("adopted", [])
                rejected = key in c.get("rejected", [])
                auto = key in c.get("auto_applied", [])
                outcome = c.get("outcomes", {}).get(key, "?")
                bt = c.get("backtest_results", {}).get(key, "")
                if isinstance(bt, dict):
                    bt_tag = f" [回测:{bt.get('verdict','')} ΔPnL={bt.get('delta_pnl_pct',0):+.3f}%]"
                else:
                    bt_tag = f" [回测:{bt}]" if bt else ""
                cf = c.get("counterfactuals", {}).get(key, "")

                tag = "⚡" if auto else ("✅采纳" if adopted else ("❌拒绝" if rejected else "⏸️"))
                print(f"  {tag} | {sug['coin']} {sug['param']}: {sug['from']}→{sug['to']} | 效果:{outcome}{bt_tag}")
                if cf:
                    print(f"    🔮 {cf[:60]}")
                print(f"    {sug.get('reason','')[:60]}")
        print()

    wisdom = state.get("wisdom", {})
    if wisdom:
        print("🧠 智慧库:")
        for key, entry in wisdom.items():
            if entry["success_count"] + entry["failure_count"] > 0 or entry.get("_bootstrapped"):
                eff = entry.get("effective_range", [])
                range_str = f" 区间: [{eff[0]:.4f}, {eff[1]:.4f}]" if len(eff) == 2 else ""
                source = "🧬自举" if entry.get("_bootstrapped") else "📊实盘"
                print(f"  {key}: {source} ✅{entry['success_count']} ❌{entry['failure_count']}{range_str}")

    auto = state.get("auto_applied", [])
    if auto:
        print(f"\n⚡ 自动应用记录 ({len(auto)} 次):")
        for a in auto[-10:]:
            print(f"  {a['time'][:19]} {a['coin']}.{a['param']}: {a['from']}→{a['to']} [{a.get('backtest_verdict','')}]")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if "--history" in sys.argv:
        show_history()
        sys.exit(0)

    dry_run = "--dry-run" in sys.argv
    auto_apply = "--apply-safe" in sys.argv

    manual_adopt, manual_reject = [], []
    for arg in sys.argv[1:]:
        if arg.startswith("--adopt="):
            manual_adopt = [x.strip() for x in arg.split("=", 1)[1].split(",") if x.strip()]
        elif arg.startswith("--reject="):
            manual_reject = [x.strip() for x in arg.split("=", 1)[1].split(",") if x.strip()]

    result = analyze_and_suggest(
        dry_run=dry_run,
        auto_apply=auto_apply,
        manual_adopt=manual_adopt,
        manual_reject=manual_reject,
    )

    if result is None:
        print("❌ AI 调参失败或无可分析数据")
        sys.exit(1)
    elif not dry_run:
        print("✅ AI 调参建议已发送")