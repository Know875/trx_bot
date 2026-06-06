"""
网格策略回测模拟器 — 基于统一的 IndicatorPack
"""
import numpy as np
from typing import Optional

from .engine import IndicatorPack


def simulate_grid(ind: IndicatorPack, grid_width: float, grid_levels: int,
                  capital: float = 1000.0, rsi_limit: Optional[float] = None,
                  regime_filter: Optional[str] = None,
                  fee_rate: Optional[float] = None,
                  slippage: Optional[float] = None) -> dict:
    """
    网格策略模拟（含手续费 + 滑点，结果贴近实盘）。

    参数:
        ind:          预计算的指标包
        grid_width:   网格宽度（价格比例，如 0.02 = ±2%）
        grid_levels:  网格档位数
        capital:      初始资金 USDT
        rsi_limit:    RSI 上限（None = 不限）
        regime_filter: 只在指定行情下建仓（None = 全天）
        fee_rate:     单边手续费率（None → config.BACKTEST_FEE_RATE）
        slippage:     单边滑点（None → config.BACKTEST_SLIPPAGE），买高卖低
    返回:
        dict 含 pnl_pct, pnl, trades, win_rate, total_fees 等
    """
    n = len(ind.close)
    if n < 60:
        return {}

    # 成本参数：默认从 config 读，回测因此和实盘同口径
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

    usdt = capital
    coin = 0.0
    buy_orders  = []  # [(price, size_usdt)]
    sell_orders = []  # [(price, size_coin)]
    all_trades = []
    total_fees = 0.0

    for i in range(60, n):
        p_high, p_low, p_close = ind.high[i], ind.low[i], ind.close[i]
        regime = ind.regimes[i]
        rsi_now = ind.rsi[i]

        # ── 成交检查：买单（成交价含滑点上浮，到手币量扣手续费）──
        new_buys = []
        for bp, sz in buy_orders:
            if p_low <= bp and sz <= usdt:
                fill_px = bp * (1 + slippage)        # 买入滑点：实际成交略高
                amt = sz / fill_px
                fee = amt * fee_rate                  # 现货手续费从到手基础货币扣
                amt -= fee
                usdt -= sz
                coin += amt
                total_fees += fee * fill_px
                sell_orders.append((bp * (1 + grid_width), amt))
                all_trades.append({"side": "buy", "price": fill_px, "amt": amt})
            else:
                new_buys.append((bp, sz))
        buy_orders = new_buys

        # ── 成交检查：卖单（成交价含滑点下压，收入扣手续费）──
        new_sells = []
        for sp, sz in sell_orders:
            if p_high >= sp and sz <= coin:
                fill_px = sp * (1 - slippage)         # 卖出滑点：实际成交略低
                proceeds = sz * fill_px
                fee = proceeds * fee_rate
                coin -= sz
                usdt += proceeds - fee
                total_fees += fee
                if rsi_limit is None or rsi_now <= rsi_limit:
                    buy_orders.append((sp * (1 - grid_width), proceeds - fee))
                all_trades.append({"side": "sell", "price": fill_px, "amt": sz})
            else:
                new_sells.append((sp, sz))
        sell_orders = new_sells

        # ── 建仓控制 ──
        can_build = True
        if regime_filter is not None and regime != regime_filter:
            can_build = False
        if rsi_limit is not None and rsi_now > rsi_limit:
            can_build = False

        if can_build:
            if not buy_orders and not sell_orders and coin < 1e-8 and usdt > 0:
                _build_grid(buy_orders, sell_orders, p_close, grid_width, grid_levels, usdt)
            elif not buy_orders and usdt > capital * 0.1:
                _build_grid(buy_orders, sell_orders, p_close, grid_width, grid_levels, usdt)

    final = usdt + coin * ind.close[-1]
    pnl = final - capital
    pnl_pct = pnl / capital * 100

    sells = [t for t in all_trades if t["side"] == "sell"]
    buys  = [t for t in all_trades if t["side"] == "buy"]
    wins = sum(1 for s in sells if s["amt"] > 0)

    return {
        "grid_width":  grid_width,
        "grid_levels": grid_levels,
        "rsi_limit":   rsi_limit or 999,
        "regime_filter": regime_filter or "all",
        "pnl":         round(pnl, 4),
        "pnl_pct":     round(pnl_pct, 4),
        "total_trades": len(all_trades),
        "buy_trades":  len(buys),
        "sell_trades": len(sells),
        "win_rate":    round(wins / len(sells) * 100, 1) if sells else 0,
        "total_fees":  round(total_fees, 4),
        "bars":        n,
    }


def _build_grid(buys, sells, price, width, levels, usdt):
    per = usdt / levels
    for i in range(levels):
        buys.append((price * (1 - width * (i + 1) / levels), per))
        sells.append((price * (1 + width * (i + 1) / levels), 0))


def sweep_grid_params(ind: IndicatorPack, name: str,
                      widths: list = None, levels: list = None,
                      rsi_limits: list = None,
                      regime_filter: Optional[str] = None) -> list:
    """遍历所有参数组合，返回按 PnL% 排序的结果列表。"""
    widths = widths or [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
    levels = levels or [3, 5, 7, 9]
    rsi_limits = rsi_limits or [None, 70, 75, 80]

    results = []
    for gw in widths:
        for gl in levels:
            for rl in rsi_limits:
                r = simulate_grid(ind, gw, gl, rsi_limit=rl, regime_filter=regime_filter)
                if r:
                    r["symbol"] = name
                    results.append(r)
    return sorted(results, key=lambda x: x["pnl_pct"], reverse=True)
