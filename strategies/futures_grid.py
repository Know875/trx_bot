"""
合约网格策略：适用于震荡行情（永续合约）
- 只在 ranging 行情下启动
- 通过合约面值(ctVal)计算张数
- 支持多/空网格（单向做多）
- 买单成交后在上方挂卖单，卖单成交后在下方挂买单
"""
import math
import logging
import config
import notify

logger = logging.getLogger("futures_grid")


class FuturesGridStrategy:
    def __init__(self, client, lower, upper, grid_count, capital, symbol=""):
        self.client = client
        self.lower = lower
        self.upper = upper
        self.grid_count = int(grid_count)
        self.capital = capital  # USDT 本金
        self.symbol = symbol

        self.grid_prices = []
        self.buy_orders = {}   # price → {"order_id", "contracts"}
        self.sell_orders = {}  # price → {"order_id", "contracts", "buy_price"}
        self._ct_val = None
        self.running = False

        # ── 连续阴跌检测 ──
        self._prev_price = 0.0
        self._consecutive_drops = 0
        self._max_consecutive_drops = 3   # 连续3根K线收跌则暂停挂买单

        # ── SOL 最大持仓档位 ──
        self._max_entries = getattr(config, "SOL_FUTURES_MAX_GRID_ENTRIES", 99) if "SOL" in symbol else 99

        self._validate()
        self._build_grid()

    def _get_ct_val(self):
        if self._ct_val is None:
            self._ct_val = self.client.get_ct_val()
            if self._ct_val <= 0:
                raise RuntimeError(f"合约面值无效: {self._ct_val}")
        return self._ct_val

    def _validate(self):
        spacing_pct = (self.upper - self.lower) / self.lower / self.grid_count
        min_profit = config.GRID_MIN_PROFIT_PCT * 0.5  # 合约网格利润门槛可以低一些
        if spacing_pct < min_profit:
            raise ValueError(
                f"合约网格间距 {spacing_pct:.4%} 低于最低 {min_profit:.4%}"
            )
        logger.info(f"合约网格间距: {spacing_pct:.4%}")

    def _build_grid(self):
        step = (self.upper - self.lower) / self.grid_count
        self.grid_prices = [round(self.lower + i * step, 6) for i in range(self.grid_count + 1)]

    def _calc_contracts(self, usdt, price):
        ct_val = self._get_ct_val()
        raw = usdt * config.get_leverage(self.symbol) / (price * ct_val)
        return max(1, math.floor(raw))

    def start(self, current_price):
        logger.info(f"启动合约网格，当前价格: {current_price} 区间: {self.lower:.5f}~{self.upper:.5f}")
        try:
            self.client.set_leverage()
        except Exception as e:
            logger.warning(f"设置杠杆失败: {e}")

        placed = 0
        per_level_usdt = self.capital / self.grid_count
        for price in self.grid_prices:
            if price >= current_price:
                continue
            if placed >= self._max_entries:
                logger.info(f"[合约网格] 初始达到最大档位 {self._max_entries}，停止挂单")
                break
            contracts = self._calc_contracts(per_level_usdt, price)
            if contracts <= 0:
                continue
            try:
                oid = self.client.place_futures_order("buy", contracts, order_type="limit", price=price)
                self.buy_orders[price] = {"order_id": oid, "contracts": contracts}
                placed += 1
                logger.info(f"[合约网格] 挂买单 {contracts}张 @ {price:.6f}")
            except Exception as e:
                logger.error(f"[合约网格] 挂单失败 @ {price:.6f}: {e}")

        if self.buy_orders:
            self.running = True
            logger.info(f"[合约网格] 启动，共 {len(self.buy_orders)}/{self._max_entries} 个挂单")
        else:
            logger.error("[合约网格] 所有挂单均失败")

    def on_tick(self, price=None):
        realized_pnl = 0.0
        if not self.running:
            return realized_pnl

        # ── 连续阴跌检测 ────────────────────────────────
        if price is not None:
            if self._prev_price > 0 and price < self._prev_price * 0.999:  # 下跌 >0.1% 才算有效跌
                self._consecutive_drops += 1
            elif price > self._prev_price:
                self._consecutive_drops = 0  # 价格回升，重置计数
            self._prev_price = price

        # ── 浮亏止损：持仓亏损超阈值时平仓 ───────────────
        if price is not None and self.sell_orders:
            try:
                ct_val = self._get_ct_val()
                unrealized = sum(
                    info["contracts"] * ct_val * (price - info["buy_price"])
                    for info in self.sell_orders.values()
                )
                # 使用币种专属阈值，未配置的用默认值
                max_loss_pct = getattr(config, "FUTURES_GRID_MAX_FLOAT_LOSS", {}).get(
                    self.symbol, config.FUTURES_GRID_MAX_FLOAT_LOSS_PCT
                )
                max_loss = self.capital * max_loss_pct
                if unrealized < -max_loss:
                    logger.warning(
                        f"[合约网格] 浮亏 {unrealized:.2f} USDT 超出阈值 -{max_loss:.2f} USDT，止损平仓"
                    )
                    import notify as _notify
                    _notify.send_tg(
                        f"⚠️ 合约网格止损 [{self.symbol}]\n"
                        f"浮亏: {unrealized:.2f} USDT（阈值: -{max_loss:.2f}）\n"
                        f"已强制平仓"
                    )
                    return self.stop()
            except Exception as e:
                logger.warning(f"[合约网格] 浮亏检查失败: {e}")

        # 批量查询挂单列表，减少API调用
        try:
            pending_ids = {o["ordId"] for o in self.client.get_pending_orders()}
        except Exception as e:
            logger.warning(f"[合约网格] 批量查询挂单失败: {e}")
            return realized_pnl

        ct_val = self._get_ct_val()

        # 检查买单成交
        for price in list(self.buy_orders.keys()):
            info = self.buy_orders[price]
            if info["order_id"] in pending_ids:
                continue
            try:
                order = self.client.get_order(info["order_id"])
            except Exception:
                continue
            if order["state"] != "filled":
                continue
            filled_contracts = int(float(order.get("sz", 0)))
            filled_price = float(order.get("avgPx", price))

            idx = self.grid_prices.index(price) if price in self.grid_prices else -1
            if idx >= 0 and idx + 1 < len(self.grid_prices):
                sell_price = self.grid_prices[idx + 1]
                if sell_price not in self.sell_orders:
                    try:
                        oid = self.client.place_futures_order("sell", filled_contracts, order_type="limit", price=sell_price)
                        self.sell_orders[sell_price] = {"order_id": oid, "contracts": filled_contracts, "buy_price": filled_price}
                        del self.buy_orders[price]
                        logger.info(f"[合约网格] 买单成交 @ {filled_price:.6f} → 挂卖单 @ {sell_price:.6f}")
                    except Exception as e:
                        logger.error(f"[合约网格] 补挂卖单失败: {e}")
                else:
                    del self.buy_orders[price]

        # ── 卖单成交 ──
        # 如果连续阴跌 ≥ 阈值，不补挂买单（不接飞刀）
        drop_pause = self._consecutive_drops >= self._max_consecutive_drops
        if drop_pause:
            logger.info(f"[合约网格] 连续阴跌 {self._consecutive_drops} 根，暂停补挂买单")

        # 检查卖单成交
        for price in list(self.sell_orders.keys()):
            info = self.sell_orders[price]
            if info["order_id"] in pending_ids:
                continue
            try:
                order = self.client.get_order(info["order_id"])
            except Exception:
                continue
            if order["state"] != "filled":
                continue
            filled_contracts = int(float(order.get("sz", 0)))
            filled_price = float(order.get("avgPx", price))
            buy_price = info["buy_price"]
            del self.sell_orders[price]

            fee = filled_contracts * ct_val * (buy_price + filled_price) * config.FUTURES_FEE_RATE
            pnl = filled_contracts * ct_val * (filled_price - buy_price) - fee
            realized_pnl += pnl
            logger.info(f"[合约网格] 卖单成交 @ {filled_price:.6f}  净盈亏: {pnl:+.4f} USDT")
            notify.send_tg(
                f"📊 合约网格成交 [{self.symbol}]\n"
                f"买 {buy_price:.6f} → 卖 {filled_price:.6f}\n"
                f"盈亏: {pnl:+.4f} USDT"
            )

            # 连续阴跌中不补挂买单
            if drop_pause:
                logger.info(f"[合约网格] 连续阴跌中，跳过补挂买单 @ {self.symbol}")
                continue

            # SOL 限仓检查
            held = len(self.sell_orders)
            pending_buys = len(self.buy_orders)
            if (held + pending_buys) >= self._max_entries:
                logger.info(f"[合约网格] 已达最大持仓档位 {self._max_entries}（买入{pending_buys}+持仓{held}），跳过补挂")
                continue

            idx = self.grid_prices.index(price) if price in self.grid_prices else -1
            if idx > 0:
                buy_price_new = self.grid_prices[idx - 1]
                if buy_price_new not in self.buy_orders:
                    try:
                        oid = self.client.place_futures_order("buy", filled_contracts, order_type="limit", price=buy_price_new)
                        self.buy_orders[buy_price_new] = {"order_id": oid, "contracts": filled_contracts}
                        logger.info(f"[合约网格] 补挂买单 @ {buy_price_new:.6f}")
                    except Exception as e:
                        logger.error(f"[合约网格] 补挂买单失败: {e}")

        return realized_pnl

    def stop(self, keep_sells=False):
        self.running = False

        if keep_sells:
            # 只撤买单，保留限价卖单（趋势上行中自然成交）
            for info in list(self.buy_orders.values()):
                try:
                    self.client.cancel_order(info["order_id"])
                except Exception:
                    pass
            self.buy_orders.clear()
            logger.info(f"[合约网格] 保留 {len(self.sell_orders)} 个限价卖单（等待自然成交）")
            return 0

        logger.info("[合约网格] 撤销所有挂单...")

        # 收集已购入但还未卖出的持仓
        pending_positions = [
            {"contracts": info["contracts"], "buy_price": info["buy_price"]}
            for info in self.sell_orders.values()
        ]

        for info in list(self.buy_orders.values()):
            try:
                self.client.cancel_order(info["order_id"])
            except Exception:
                pass
        for info in list(self.sell_orders.values()):
            try:
                self.client.cancel_order(info["order_id"])
            except Exception:
                pass
        self.buy_orders.clear()
        self.sell_orders.clear()

        # 市价平掉持仓
        total_pnl = 0.0
        if pending_positions:
            try:
                ticker = self.client.get_ticker()
                market_price = float(ticker["last"])
                ct_val = self._get_ct_val()
                self.client.close_futures_position()
                for pos in pending_positions:
                    fee = pos["contracts"] * ct_val * (pos["buy_price"] + market_price) * config.FUTURES_FEE_RATE
                    pnl = pos["contracts"] * ct_val * (market_price - pos["buy_price"]) - fee
                    total_pnl += pnl
                logger.info(f"[合约网格平仓] 盈亏 {total_pnl:+.4f} USDT")
                # TG 通知
                try:
                    import notify
                    notify.send_tg(
                        f"📊 合约网格平仓 [{self.symbol}]\n"
                        f"原因: 策略停止（regime切换/止损）\n"
                        f"持仓数: {len(pending_positions)} | 盈亏: {total_pnl:+.4f} USDT\n"
                        f"市价: {market_price:.4f}"
                    )
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"[合约网格平仓] 失败: {e}")

        return total_pnl
