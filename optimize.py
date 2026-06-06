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
