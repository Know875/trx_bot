"""
网格策略：适用于震荡行情
- 在价格区间内均匀布置买卖网格
- 每格间距必须 > 0.2%（覆盖手续费）
- 买单成交后在上方挂卖单，卖单成交后在下方挂买单
- 盈亏在卖单成交时结算（买入成本记录在 pending_sells 中）
"""
import math
import logging
import config
import notify

logger = logging.getLogger("grid")


class GridStrategy:
    def __init__(self, client, lower, upper, grid_count, capital, size_decimals=0, symbol=""):
        self.client = client
        self.lower = lower
        self.upper = upper
        self.grid_count = int(grid_count)
        self.capital = capital
        self._size_dec = size_decimals
        self.symbol = symbol

        self.grid_prices = []
        self.orders = {}        # price -> {order_id, side, size}
        self.pending_sells = {} # sell_price -> buy_price（用于结算时计算真实盈亏）
        self.running = False

        # ── 最大持仓档位（SOL 限仓防过重暴露）──────────────────
        self._max_entries = getattr(config, "SOL_SPOT_MAX_GRID_ENTRIES", 99) if "SOL" in symbol else 99

        self._validate()
        self._build_grid()

    def _validate(self):
        spacing_pct = (self.upper - self.lower) / self.lower / self.grid_count
        if spacing_pct < config.GRID_MIN_PROFIT_PCT:
            raise ValueError(
                f"网格间距 {spacing_pct:.4%} 低于最低利润要求 {config.GRID_MIN_PROFIT_PCT:.4%}，"
                f"请缩小网格数量或扩大区间"
            )
        logger.info(f"网格间距: {spacing_pct:.4%}，区间: {self.lower:.5f} ~ {self.upper:.5f}")

    def _build_grid(self):
        step = (self.upper - self.lower) / self.grid_count
        self.grid_prices = [round(self.lower + i * step, 6) for i in range(self.grid_count + 1)]
        logger.info(f"网格价位: {self.grid_prices}")

    def _order_size(self, price):
        per_grid_usdt = self.capital / self.grid_count
        size = per_grid_usdt / price
        if self._size_dec == 0:
            return math.floor(size)
        factor = 10 ** self._size_dec
        return math.floor(size * factor) / factor

    def _fee_adjusted_size(self, size):
        """买单到账量 = size * (1 - fee)，保守取 0.999 避免卖单 balance insufficient"""
        adjusted = size * 0.999
        if self._size_dec == 0:
            return math.floor(adjusted)
        factor = 10 ** self._size_dec
        return math.floor(adjusted * factor) / factor

    def start(self, current_price):
        """初始化挂单：只挂当前价格以下的买单，卖单等买单成交后再补挂"""
        logger.info(f"启动网格，当前价格: {current_price}")

        placed = 0
        for price in self.grid_prices:
            if price >= current_price:
                continue
            if placed >= self._max_entries:
                logger.info(f"[网格] 初始达到最大档位 {self._max_entries}，停止挂单")
                break
            size = self._order_size(price)
            if size <= 0:
                continue
            try:
                order_id = self.client.place_order("buy", price, size)
                self.orders[price] = {"order_id": order_id, "side": "buy", "size": size}
                placed += 1
                logger.info(f"挂单 buy {size} @ {price}")
            except Exception as e:
                logger.error(f"挂单失败 buy @ {price}: {e}")

        if self.orders:
            self.running = True
            logger.info(f"网格启动成功，共 {sum(1 for info in self.orders.values() if isinstance(info, dict) and info.get('side')=='buy')}/{self._max_entries} 个买单挂单")
        else:
            logger.error("网格所有挂单均失败，策略未启动")

    def on_tick(self):
        """
        检查成交情况，成交后在对面挂反向单
        返回本次循环的盈亏（已实现）
        """
        realized_pnl = 0.0
        if not self.running:
            return realized_pnl

        for price, info in list(self.orders.items()):
            try:
                order = self.client.get_order(info["order_id"])
            except Exception as e:
                logger.warning(f"查询订单失败 @ {price}: {e}")
                continue

            if order["state"] != "filled":
                continue

            filled_side = order["side"]
            filled_size = float(order["sz"])
            filled_price = float(order["avgPx"])
            logger.info(f"成交: {filled_side} {filled_size} {self.symbol} @ {filled_price}")

            idx = self.grid_prices.index(price) if price in self.grid_prices else -1

            if filled_side == "buy":
                # 记录买入成本，在对应卖单成交时才结算盈亏
                if idx >= 0 and idx + 1 < len(self.grid_prices):
                    sell_price = self.grid_prices[idx + 1]
                    # OKX 买单手续费从收到的基础货币扣除，卖单 size 需相应缩减
                    sell_size = self._fee_adjusted_size(filled_size)
                    self.pending_sells[sell_price] = filled_price  # sell_price -> buy_cost
                    try:
                        oid = self.client.place_order("sell", sell_price, sell_size)
                        self.orders[sell_price] = {"order_id": oid, "side": "sell", "size": sell_size}
                        del self.orders[price]
                        logger.info(f"补挂卖单 @ {sell_price}")
                    except Exception as e:
                        logger.error(f"补挂卖单失败（下一 tick 重试）: {e}")
                        self.pending_sells.pop(sell_price, None)  # 卖单未挂出，回退 pending
                else:
                    del self.orders[price]

            elif filled_side == "sell":
                del self.orders[price]
                # 卖单成交时才结算真实盈亏
                buy_cost = self.pending_sells.pop(price, None)
                if buy_cost is not None:
                    fee = filled_size * (buy_cost + filled_price) * config.SPOT_FEE_RATE
                    pnl = filled_size * (filled_price - buy_cost) - fee
                    realized_pnl += pnl
                    logger.info(f"网格盈亏: +{pnl:.4f} USDT（买 {buy_cost:.6f} → 卖 {filled_price:.6f}）")
                    notify.send_tg(
                        f"💰 网格成交 [{self.symbol}]\n"
                        f"买 {buy_cost:.6f} → 卖 {filled_price:.6f}\n"
                        f"盈亏: +{pnl:.4f} USDT"
                    )

                # 在下方一格补挂买单（SOL 限仓检查）
                active_buys = sum(1 for info in self.orders.values()
                                  if isinstance(info, dict) and info.get("side") == "buy")
                held = len(self.pending_sells)
                if idx > 0 and (active_buys + held) < self._max_entries:
                    buy_price = self.grid_prices[idx - 1]
                    try:
                        oid = self.client.place_order("buy", buy_price, filled_size)
                        self.orders[buy_price] = {"order_id": oid, "side": "buy", "size": filled_size}
                        logger.info(f"补挂买单 @ {buy_price}")
                    except Exception as e:
                        logger.error(f"补挂买单失败: {e}")
                elif (active_buys + held) >= self._max_entries:
                    logger.info(f"[网格] 已达最大持仓档位 {self._max_entries}（买入{active_buys}+持仓{held}），跳过补挂")

        return realized_pnl

    def stop(self, keep_sells=False):
        """撤销网格。keep_sells=True 时保留限价卖单（趋势上行中自然成交），只撤买单"""
        self.running = False

        if keep_sells:
            # 只撤销买单，保留限价卖单让它们自然成交
            for price, info in list(self.orders.items()):
                if info["side"] == "buy":
                    try:
                        self.client.cancel_order(info["order_id"])
                        logger.info(f"[网格] 已撤买单 @ {price}")
                    except Exception as e:
                        logger.warning(f"[网格] 撤买单失败 @ {price}: {e}")
                    del self.orders[price]
            # 卖单保留，不清空 orders/pending_sells——由下次启动清理接管
            logger.info(f"[网格] 保留 {len(self.orders)} 个限价卖单（等待自然成交）")
            return 0

        # 原始逻辑：全部撤销 + 市价平仓
        logger.info("撤销网格所有挂单...")
        pending_positions = []
        for sell_price, buy_cost in list(self.pending_sells.items()):
            sell_info = self.orders.get(sell_price)
            if sell_info and sell_info["side"] == "sell":
                pending_positions.append({"size": sell_info["size"], "buy_cost": buy_cost})

        for price, info in list(self.orders.items()):
            try:
                self.client.cancel_order(info["order_id"])
                logger.info(f"已撤单 @ {price}")
            except Exception as e:
                logger.warning(f"撤单失败 @ {price}: {e}")
        self.orders.clear()
        self.pending_sells.clear()

        # 市价平掉持仓并结算盈亏
        total_pnl = 0.0
        for pos in pending_positions:
            try:
                ticker = self.client.get_ticker()
                market_price = float(ticker["last"])
                self.client.place_order("sell", market_price, pos["size"], order_type="market")
                fee = pos["size"] * (pos["buy_cost"] + market_price) * config.SPOT_FEE_RATE
                pnl = pos["size"] * (market_price - pos["buy_cost"]) - fee
                total_pnl += pnl
                logger.info(
                    f"[网格平仓] 卖出 {pos['size']} {self.symbol} @ {market_price:.4f} "
                    f"买成本 {pos['buy_cost']:.4f} 盈亏 {pnl:+.4f} USDT"
                )
            except Exception as e:
                logger.error(f"[网格平仓] 市价平仓失败: {e}")

        return total_pnl
