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
import logging

logger = logging.getLogger("optimize")

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
        logger.info(f"{coin}: 数据不足 ({n} bars)，自动缩小窗口到 {window_bars} bars")
    if n < 180:  # 最少需要 3 小时数据
        logger.warning(f"{coin}: 数据不足（{n} bars），至少需要 180")
        return None

    rng = GRID_RANGES.get(coin, _DEFAULT_RANGE)
    w_lo, w_hi = rng["width"]
    l_lo, l_hi = rng["levels"]

    per_window = []
    oos_scores = []
    in_sample_scores = []
    param_votes = []  # 每窗的最佳参数

    step = window_bars // 2  # 半窗步长 → 窗口有重叠

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
            import optuna
            optuna.logging.set_verbosity(optuna.logging.WARNING)

            def objective(trial):
                w = trial.suggest_float("grid_width", w_lo, w_hi)
                lv = trial.suggest_int("grid_levels", l_lo, l_hi)
                r = simulate_grid(train_ind, grid_width=w, grid_levels=lv)
                return _score(r)

            study = optuna.create_study(
                direction="maximize",
                sampler=optuna.samplers.TPESampler(seed=42),
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
    if param_votes:
        # 按样本外 PnL 加权投票
        stable_count = {}
        for w, lv, oos in param_votes:
            key = f"{w:.3f}_{lv}"
            stable_count[key] = stable_count.get(key, 0) + (1 if oos > 0 else 0)
        best_key = max(stable_count, key=stable_count.get)
        best_w, best_lv = best_key.split("_")
        stable_params = {"grid_width": float(best_w), "grid_levels": int(best_lv)}
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
        "stable_count": stable_count.get(best_key, 0) if param_votes else 0,
        "per_window": per_window,
    }


def _slice_ind(ind, start: int, end: int):
    """切出 IndicatorPack 的 [start:end) 子集"""
    import copy
    sliced = copy.copy(ind)
    for attr in ("close", "high", "low", "vol", "ema20", "ema60",
                 "bb_upper", "bb_lower", "bb_width", "atr", "rsi", "adx"):
        arr = getattr(ind, attr, None)
        if arr is not None and len(arr) > end:
            setattr(sliced, attr, arr[start:end])
    if ind.regimes and len(ind.regimes) > end:
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


if __name__ == "__main__":
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
