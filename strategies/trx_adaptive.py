"""
TRX 现货自适应策略

继承 BaseAdaptiveStrategy，只覆写现货交易接口。
状态机、网格逻辑、趋势逻辑全部由基类提供。
"""
import math
import logging

import config
import notify
from .base_adaptive import BaseAdaptiveStrategy

logger = logging.getLogger("trx_adaptive")

class TRXAdaptiveStrategy(BaseAdaptiveStrategy):
    """TRX 现货自适应策略：网格 + 趋势（仅做多）"""

    def __init__(self, client, capital, size_decimals=0):
        super().__init__(client, capital)
        self.coin = "TRX"
        self._size_decimals = size_decimals
        # 用 coin 专属 logger，避免基类共享 logger 串台到其他币种文件
        self._log = logger

    def _fee_adjust_sell_size(self, size):
        """现货：买单成交后到手 base 币 = size×(1-fee)，卖单需相应缩量，
        否则超卖导致 'balance insufficient'。保守用 0.999 并向下取整到精度。"""
        adjusted = size * 0.999
        if self._size_decimals == 0:
            return math.floor(adjusted)
        factor = 10 ** self._size_decimals
        return math.floor(adjusted * factor) / factor

    # ══════════════════════════════════════════════════
    # 现货特性
    # ══════════════════════════════════════════════════

    def _allow_short(self) -> bool:
        return False

    # ══════════════════════════════════════════════════
    # 交易接口实现
    # ══════════════════════════════════════════════════

    def _place_order(self, side, price, size, order_type="limit"):
        return self.client.place_order(side, price, size, order_type=order_type)

    def _place_market_order(self, side, size, reduce_only=False):
        # 现货无 reduceOnly 概念（卖出即减持），忽略该标志
        ticker = self.client.get_ticker()
        price = float(ticker["last"])
        return self.client.place_order(side, price, size, order_type="market")

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

    def _calc_trade_size(self, usdt, price):
        """现货：USDT → 币数"""
        size = usdt / price
        if self._size_decimals == 0:
            return int(size)
        return round(size, self._size_decimals)

    def _calc_pnl(self, size, entry, exit_price, side="long"):
        """现货做多盈亏 = size × (exit - entry) - 双边手续费"""
        fee = size * (entry + exit_price) * config.SPOT_FEE_RATE
        return size * (exit_price - entry) - fee

    def _get_filled_size(self, order):
        raw = float(order["sz"])
        return raw  # 现货订单 sz 就是币数

    def _is_initial_sell(self, buy_cost, price):
        # 现货：buy_cost 记录的是成交价，不等于 price 说明是买入后补挂的单
        return abs(buy_cost - price) < 1e-10

    def _on_initial_sell_fill(self, price, size):
        """现货：没有初始卖单（只能做多）"""
        pass

    def _on_grid_sell_fill(self, price, size):
        # 继承基类默认行为：在下方补买单
        super()._on_grid_sell_fill(price, size)

    def _close_pending_positions(self, price):
        """现货网格平仓：市价卖出所有待平仓的多头"""
        from strategies.trx_utils import is_asia_peak
        # 计算待平多头
        pending_long = []
        for p, info in self._sell_orders.items():
            buy_cost = info.get("buy_cost", 0)
            if abs(buy_cost - p) > 1e-10:  # buy_cost ≠ 挂单价 → 多头待平
                pending_long.append({"size": info["size"], "buy_cost": buy_cost})

        if not pending_long:
            return 0.0

        total_size = sum(p["size"] for p in pending_long)
        # Smart 卖出：如果非高峰且有余额重叠，集中卖出减少手续费
        if not is_asia_peak() and total_size > 100:
            try:
                self.client.place_order("sell", price, total_size, order_type="market")
                total_pnl = sum(
                    self._calc_pnl(p["size"], p["buy_cost"], price, "long")
                    for p in pending_long
                )
                logger.info(f"[现货网格平仓] 集中卖出 {total_size}币 @ {price:.6f}  盈亏 {total_pnl:+.4f}")
                return total_pnl
            except Exception:
                pass

        return 0.0

    def _trend_position_pct(self):
        return config.TRX_TREND_POSITION_PCT

    def _calc_partial(self, total, ratio):
        return int(total * ratio)

    def _place_algo_stop(self, size, price):
        """现货委托止损单（由 OKX algo order 实现）— 触发后市价卖出平多"""
        try:
            return self.client.place_algo_order("sell", size, price)
        except Exception as e:
            logger.warning(f"[现货趋势] 交易所止损单挂单失败: {e}")
            return None

    def _cancel_algo_orders(self):
        try:
            self.client.cancel_algo_orders()
        except Exception:
            pass

    # ══════════════════════════════════════════════════
    # 日志钩子（现货定制）
    # ══════════════════════════════════════════════════

    def _notify_trend_open(self, direction, size, price, stop, tp1, tag=""):
        logger.info(f"[现货趋势] 开多 {size}币 @ {price:.6f}{tag}  止损={stop:.6f}  T1={tp1:.6f}")
        notify.send_tg(
            f"🚀 TRX 现货开仓\n"
            f"[现货趋势] 开多 {size}币 @ {price:.6f}{tag}\n"
            f"止损={stop:.6f}  T1={tp1:.6f}"
        )

    def _notify_trend_tp1(self, size, price, gain):
        notify.send_tg(
            f"💰 TRX 现货 T1止盈\n"
            f"平仓{size}币 @ {price:.6f}\n"
            f"+{gain:.4f} USDT"
        )

    def _notify_trend_close(self, reason, size, price, gain):
        emoji = "✅" if gain >= 0 else "❌"
        notify.send_tg(
            f"{emoji} TRX 现货 {reason}\n"
            f"平仓{size}币 @ {price:.6f}\n"
            f"{gain:+.4f} USDT"
        )
