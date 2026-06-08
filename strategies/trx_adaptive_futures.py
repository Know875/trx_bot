"""
TRX 合约自适应策略

继承 BaseAdaptiveStrategy，覆写合约交易接口。
与现货版本的核心差异：
  - 订单接口 → place_futures_order
  - 平仓接口 → close_futures_position  
  - 做空支持
  - 网格浮动盈亏监控
"""
import math
import logging

import config
import notify
from .base_adaptive import BaseAdaptiveStrategy

logger = logging.getLogger("trx_adaptive_futures")

# 复用基类状态枚举
IDLE           = "idle"
GRID_RUNNING   = "grid"
TREND_CONFIRM  = "trend_confirm"
TREND_RUNNING  = "trend"


class TRXAdaptiveFuturesStrategy(BaseAdaptiveStrategy):
    """TRX 合约自适应策略：双边网格 + 做多做空趋势"""

    def __init__(self, client, capital):
        super().__init__(client, capital)
        self.coin = "TRX_SWAP"
        self._ct_val = None
        # 合约网格特有追踪
        self._pending_long  = []   # 现货版没有的多头待平仓列表
        self._pending_short = []   # 现货版没有的空头待平仓列表

    # ══════════════════════════════════════════════════
    # 合约特性
    # ══════════════════════════════════════════════════

    def _allow_short(self) -> bool:
        return True

    def _on_start_hook(self):
        try:
            self.client.set_leverage()
        except Exception as e:
            logger.warning(f"设置杠杆失败: {e}")

    def _grid_position_pct(self):
        return config.TRX_SWAP_GRID_POSITION_PCT

    def _trend_position_pct(self):
        return config.TRX_SWAP_TREND_POSITION_PCT

    # ══════════════════════════════════════════════════
    # 交易接口实现
    # ══════════════════════════════════════════════════

    def _place_order(self, side, price, size, order_type="limit"):
        return self.client.place_futures_order(side, size, order_type=order_type, price=price)

    def _place_market_order(self, side, size):
        return self.client.place_futures_order(side, size, order_type="market")

    def _cancel_order(self, order_id):
        return self.client.cancel_order(order_id)

    def _cancel_all_pending(self):
        try:
            self.client.cancel_all_orders()
        except Exception:
            for o in self.client.get_pending_orders():
                try:
                    self.client.cancel_order(o["ordId"])
                except Exception:
                    pass

    def _get_pending_order_ids(self):
        return {o["ordId"] for o in self.client.get_pending_orders()}

    def _get_order(self, order_id):
        return self.client.get_order(order_id)

    def _get_ct_val(self):
        if self._ct_val is None:
            self._ct_val = self.client.get_ct_val()
            if self._ct_val <= 0:
                raise RuntimeError(f"无效合约面值: {self._ct_val}")
        return self._ct_val

    def _calc_trade_size(self, usdt, price):
        """合约：USDT → 张数"""
        ct_val = self._get_ct_val()
        raw = usdt * config.get_leverage("TRX_SWAP") / (price * ct_val)
        return max(1, math.floor(raw))

    def _calc_pnl(self, size, entry, exit_price, side="long"):
        """合约盈亏 = 张数 × 面值 × (出场 - 入场) - 双边手续费"""
        ct_val = self._get_ct_val()
        fee = size * ct_val * (entry + exit_price) * config.FUTURES_FEE_RATE
        if side == "long":
            return size * ct_val * (exit_price - entry) - fee
        return size * ct_val * (entry - exit_price) - fee

    def _calc_partial(self, total, ratio):
        return math.floor(total * ratio)

    def _get_filled_size(self, order):
        return int(float(order["sz"]))

    def _place_algo_stop(self, size, price):
        """合约止损：平多用 sell，平空用 buy（reduceOnly 由客户端处理）"""
        try:
            side = "sell" if self._trend_dir == "up" else "buy"
            return self.client.place_algo_order(side, size, price)
        except Exception as e:
            logger.warning(f"[合约趋势] 交易所止损单挂单失败: {e}")
            return None

    def _cancel_algo_orders(self):
        try:
            self.client.cancel_algo_orders()
        except Exception:
            pass

    # ══════════════════════════════════════════════════
    # 网格 — 初始卖单处理（做空）
    # ══════════════════════════════════════════════════

    def _is_initial_sell(self, buy_cost, price):
        """合约：buy_cost == 挂单价 → 初始做空"""
        return abs(buy_cost - price) < 1e-10

    def _on_initial_sell_fill(self, price, size):
        """做空初始成交 → 在下方补平仓买单"""
        logger.info(f"[合约网格] 初始做空成交 {size}张 @ {price:.6f}")
        idx = self._grid_prices.index(price) if price in self._grid_prices else -1
        if idx > 0:
            buy_price = self._grid_prices[idx - 1]
            if buy_price not in self._buy_orders:
                try:
                    oid = self._place_order("buy", buy_price, size)
                    self._buy_orders[buy_price] = {"order_id": oid, "size": size}
                    logger.info(f"[合约网格] 做空→补挂平仓买单 @ {buy_price:.6f}")
                except Exception as e:
                    logger.error(f"[合约网格] 补挂平仓买单失败: {e}")

    def _on_grid_sell_fill(self, price, size):
        """平多成交 → 下方补买单"""
        idx = self._grid_prices.index(price) if price in self._grid_prices else -1
        if idx > 0:
            buy_price = self._grid_prices[idx - 1]
            if buy_price not in self._buy_orders:
                try:
                    oid = self._place_order("buy", buy_price, size)
                    self._buy_orders[buy_price] = {"order_id": oid, "size": size}
                    logger.info(f"[合约网格] 补挂买单 @ {buy_price:.6f}")
                except Exception as e:
                    logger.error(f"[合约网格] 补挂买单失败: {e}")

    # ══════════════════════════════════════════════════
    # 网格 — 平仓（close_futures_position）
    # ══════════════════════════════════════════════════

    def _close_pending_positions(self, price):
        """合约网格平仓：统一平掉所有待平多头+空头"""
        pending_long = [
            {"size": info["size"], "buy_cost": info["buy_cost"]}
            for p, info in self._sell_orders.items()
            if abs(info.get("buy_cost", 0) - p) > 1e-10
        ]
        pending_short = [
            {"size": info["size"], "entry_price": info.get("buy_cost", 0)}
            for p, info in self._sell_orders.items()
            if abs(info.get("buy_cost", 0) - p) < 1e-10
        ]

        if not (pending_long or pending_short):
            return 0.0

        total_pnl = 0.0
        try:
            self.client.close_futures_position()
            ct_val = self._get_ct_val()
            for pos in pending_long:
                total_pnl += self._calc_pnl(pos["size"], pos["buy_cost"], price, "long")
            for pos in pending_short:
                total_pnl += self._calc_pnl(pos["size"], pos["entry_price"], price, "short")
            logger.info(f"[合约网格平仓] {len(pending_long)}多头 {len(pending_short)}空头 盈亏 {total_pnl:+.4f}")
        except Exception as e:
            logger.error(f"[合约网格平仓] 失败: {e}")
        return total_pnl

    # ══════════════════════════════════════════════════
    # 日志钩子（合约定制）
    # ══════════════════════════════════════════════════

    def _notify_trend_open(self, direction, size, price, stop, tp1, tag=""):
        side = "多" if direction == "up" else "空"
        logger.info(f"[合约趋势] 开{side} {size}张 @ {price:.6f}{tag}  止损={stop:.6f}  T1={tp1:.6f}")
        notify.send_tg(
            f"🚀 TRX 合约 开仓\n"
            f"开{side} {size}张 @ {price:.6f}{tag}\n"
            f"止损={stop:.6f}  T1={tp1:.6f}"
        )

    def _notify_trend_tp1(self, size, price, gain):
        notify.send_tg(
            f"💰 TRX 合约 T1止盈\n"
            f"平仓{size}张 @ {price:.6f}\n"
            f"+{gain:.4f} USDT"
        )

    def _notify_trend_close(self, reason, size, price, gain):
        emoji = "✅" if gain >= 0 else "❌"
        notify.send_tg(
            f"{emoji} TRX 合约 {reason}\n"
            f"平仓{size}张 @ {price:.6f}\n"
            f"{gain:+.4f} USDT"
        )
