"""
趋势策略回测模拟器 — 基于统一的 IndicatorPack
"""
import numpy as np

from .engine import IndicatorPack


# 默认参数
TP_PCT    = 0.020
SL_PCT    = 0.010
TRAIL_PCT = 0.008
RSI_OB    = 70
RSI_OS    = 30
COOLDOWN  = 5
POSITION_PCT = 0.45


def simulate_trend(ind: IndicatorPack, capital: float = 1000.0,
                   tp_pct: float = TP_PCT, sl_pct: float = SL_PCT,
                   trail_pct: float = TRAIL_PCT, rsi_ob: float = RSI_OB,
                   rsi_os: float = RSI_OS, cooldown_ticks: int = COOLDOWN,
                   position_pct: float = POSITION_PCT,
                   fee_rate: float = None, slippage: float = None) -> dict:
    """
    趋势策略模拟：只在 trending_up/down 期间开仓（含双边手续费 + 滑点）。

    返回:
        dict 含 pnl_pct, pnl, trades, win_rate, reasons 等
    """
    if fee_rate is None or slippage is None:
        try:
            import config as _cfg
            if fee_rate is None:
                fee_rate = getattr(_cfg, "BACKTEST_FEE_RATE", 0.001)
            if slippage is None:
                slippage = getattr(_cfg, "BACKTEST_SLIPPAGE", 0.0005)
        except Exception:
            fee_rate = 0.001 if fee_rate is None else fee_rate
            slippage = 0.0005 if slippage is None else slippage
    cost_rate = fee_rate + slippage   # 单边总成本（开仓和平仓各一次）

    n = len(ind.close)
    usdt = capital
    pos = 0.0
    pos_side = None  # "long" | "short"
    entry_price = 0.0
    tp_price = 0.0
    stop_price = 0.0
    best_price = 0.0
    cooldown = 0
    trades = []
    regime_active = False

    for i in range(60, n):
        p_close = ind.close[i]
        p_high  = ind.high[i]
        p_low   = ind.low[i]
        regime  = ind.regimes[i]

        if cooldown > 0:
            cooldown -= 1

        # 非趋势期间：有仓则平
        if regime not in ("trending_up", "trending_down"):
            regime_active = False
            if pos > 0:
                pnl = _pnl(pos, pos_side, entry_price, p_close, cost_rate)
                trades.append({"side": pos_side, "entry": entry_price, "exit": p_close,
                               "pnl": pnl, "reason": "regime_end"})
                usdt += pos + pnl
                pos = 0
                pos_side = None
            continue

        if not regime_active:
            regime_active = True

        # ── 开仓 ──
        if pos == 0 and cooldown == 0:
            r = ind.rsi[i]
            pos_size = usdt * position_pct
            if regime == "trending_up" and r < rsi_ob and pos_size > 0:
                pos, pos_side = pos_size, "long"
                entry_price = best_price = p_close
                tp_price   = p_close * (1 + tp_pct)
                stop_price = p_close * (1 - sl_pct)
                continue
            elif regime == "trending_down" and r > rsi_os and pos_size > 0:
                pos, pos_side = pos_size, "short"
                entry_price = best_price = p_close
                tp_price   = p_close * (1 - tp_pct)
                stop_price = p_close * (1 + sl_pct)
                continue

        if pos == 0:
            continue

        # ── 出场条件 ──
        exit_price, reason = None, None

        if pos_side == "long":
            if p_high > best_price:
                best_price = p_high
            dd = (best_price - p_low) / best_price if best_price > 0 else 0
            if dd >= trail_pct:
                exit_price, reason = best_price * (1 - trail_pct), "trailing"
            elif p_high >= tp_price:
                exit_price, reason = tp_price, "tp"
            elif p_low <= stop_price:
                exit_price, reason = stop_price, "sl"
        else:
            if p_low < best_price:
                best_price = p_low
            dd = (p_high - best_price) / best_price if best_price > 0 else 0
            if dd >= trail_pct:
                exit_price, reason = best_price * (1 + trail_pct), "trailing"
            elif p_low <= tp_price:
                exit_price, reason = tp_price, "tp"
            elif p_high >= stop_price:
                exit_price, reason = stop_price, "sl"

        if exit_price is not None:
            pnl = _pnl(pos, pos_side, entry_price, exit_price, cost_rate)
            trades.append({"side": pos_side, "entry": entry_price, "exit": exit_price,
                           "pnl": pnl, "reason": reason})
            usdt += pos + pnl
            pos = 0
            pos_side = None
            cooldown = cooldown_ticks

    # 强制平仓
    if pos > 0:
        pnl = _pnl(pos, pos_side, entry_price, ind.close[-1], cost_rate)
        trades.append({"side": pos_side, "entry": entry_price, "exit": ind.close[-1],
                       "pnl": pnl, "reason": "forced"})
        usdt += pos + pnl

    total_pnl = usdt - capital
    pnl_pct = total_pnl / capital * 100

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0

    from collections import Counter
    reason_dist = Counter(t["reason"] for t in trades)

    return {
        "pnl_pct":   round(pnl_pct, 2),
        "pnl":       round(total_pnl, 2),
        "trades":    len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "avg_win":   round(float(avg_win), 2),
        "avg_loss":  round(float(avg_loss), 2),
        "reasons":   dict(reason_dist),
    }


def _pnl(pos, side, entry, exit_price, cost_rate=0.0):
    """计算平仓盈亏（USDT），扣双边成本(开+平)"""
    if side == "long":
        gross = pos * (exit_price - entry) / entry
    else:
        gross = pos * (entry - exit_price) / entry
    return gross - pos * 2 * cost_rate
