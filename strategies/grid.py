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
        # 币种标签，与 config.COIN_CONFIG 的键对应 ("TRX", "ETH", "SOL")
        self._coin = symbol.split("-")[0] if symbol else ""

        self.grid_prices = []
        self.orders = {}        # price -> {order_id, side, size}
        self.pending_sells = {} # sell_price -> buy_price（用于结算时计算真实盈亏）
        self.running = False

        # ── 最大持仓档位（SOL 限仓防过重暴露）──────────────────
        self._max_entries = getattr(config, "SOL_SPOT_MAX_GRID_ENTRIES", 99) if "SOL" in symbol else 99

        # ── 只卖模式：仓位超限时只挂卖单不挂买单，卖单成交后不补买 ──
        self._sell_only_mode = False

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
        """初始化挂单：价格在区间内挂买单，超限则挂卖单（只卖模式）。
        价格漂移超区间自动重锚。"""
        logger.info(f"启动网格，当前价格: {current_price} 区间: {self.lower:.5f}~{self.upper:.5f}")

        # ── 漂移重锚：价格超出网格区间 → 以当前价重新居中 ──
        grid_range_pct = (self.upper - self.lower) / ((self.lower + self.upper) / 2)
        if current_price < self.lower * 0.95 or current_price > self.upper * 1.05:
            mid = current_price
            new_lower = mid * (1 - grid_range_pct / 2)
            new_upper = mid * (1 + grid_range_pct / 2)
            logger.info(f"[网格重锚] 价格 ${current_price:.2f} 超出区间 "
                        f"({self.lower:.2f}~{self.upper:.2f})，重锚到 "
                        f"{new_lower:.2f}~{new_upper:.2f}")
            self.lower = new_lower
            self.upper = new_upper
            self._build_grid()

        # ── 仓位上限检查（在重锚后执行，用最新价格算市值）──
        buy_capped = False
        self._sell_only_mode = False
        current_qty = 0.0
        pos_cap_value = self.capital * 2  # 默认不限制
        if self._coin:
            cap_pct = config.POSITION_CAP_PCT.get(self._coin, 1.5)
            pos_cap_value = cap_pct * config.COIN_CONFIG.get(self._coin, {}).get("initial_capital", 5000)
            try:
                base = self._coin.split("_")[0] if "_" in self._coin else self._coin
                current_qty = self.client.get_spot_position(base)
                val = current_qty * current_price
                if val >= pos_cap_value * 0.7:
                    buy_capped = True
                    self._sell_only_mode = True
                    logger.warning(f"[仓位上限] {self._coin} 持仓 ${val:.0f} (={current_qty:.4f}×${current_price:.2f}) "
                                  f">= 上限 ${pos_cap_value:.0f}×70%，只卖不买")
            except Exception:
                pass

        placed_sells = 0
        placed_buys = 0

        for price in self.grid_prices:
            if price < current_price:
                # ── 买单：仅当未超限且未达最大档位 ──
                if buy_capped:
                    continue
                if placed_buys >= self._max_entries:
                    logger.info(f"[网格] 初始买单达最大档位 {self._max_entries}")
                    break
                size = self._order_size(price)
                if size <= 0:
                    continue
                try:
                    order_id = self.client.place_order("buy", price, size)
                    self.orders[price] = {"order_id": order_id, "side": "buy", "size": size}
                    placed_buys += 1
                    logger.info(f"挂单 buy {size} @ {price}")
                except Exception as e:
                    logger.error(f"挂单失败 buy @ {price}: {e}")

            elif price > current_price:
                # ── 卖单：只在超限时主动挂（正常模式卖单由买单成交触发）──
                if not buy_capped:
                    continue
                if placed_sells >= self._max_entries:
                    logger.info(f"[网格] 初始卖单达最大档位 {self._max_entries}")
                    break
                # 每个卖单档位卖出 (超限部分 / 档位数) 的量
                levels_above = sum(1 for p in self.grid_prices if p > current_price)
                levels_above = max(levels_above, 1)
                # 目标降到上限的50%，留安全边际
                target_qty = (pos_cap_value * 0.5) / current_price
                excess = max(0, current_qty - target_qty)
                sell_per_level = excess / levels_above
                sell_per_level = self._fee_adjusted_size(sell_per_level)
                min_size = config.COIN_CONFIG.get(self._coin, {}).get("min_order_size", 0.001)
                if sell_per_level < min_size:
                    logger.info(f"[仓位上限] 每档卖出 {sell_per_level:.6f} < min {min_size}，不再挂卖单")
                    break
                try:
                    order_id = self.client.place_order("sell", price, sell_per_level)
                    self.orders[price] = {"order_id": order_id, "side": "sell", "size": sell_per_level}
                    self.pending_sells[price] = -1.0  # 标记: 只卖模式卖单(用OKX均价结算盈亏)
                    placed_sells += 1
                    logger.info(f"挂单 sell {sell_per_level} @ {price}（仓位超限减仓，目标≤${pos_cap_value*0.5:.0f}）")
                except Exception as e:
                    logger.error(f"挂单失败 sell @ {price}: {e}")

        if self.orders:
            self.running = True
            parts = []
            if placed_buys:
                parts.append(f"买{placed_buys}")
            if placed_sells:
                parts.append(f"卖{placed_sells}(减仓)")
            label = "+".join(parts) if parts else "0"
            logger.info(f"[网格] 启动成功: {label}挂单 "
                        f"(区间 {self.lower:.2f}~{self.upper:.2f}，仓位上限{'已触发' if buy_capped else '正常'})")
        else:
            logger.error("网格所有挂单均失败，策略未启动")

    def on_tick(self):
        """
        检查成交情况，成交后在对面挂反向单。
        只卖模式：卖单成交后不补买；仓位降到上限50%以下自动恢复正常。
        返回本次循环的盈亏（已实现）
        """
        realized_pnl = 0.0
        if not self.running:
            return realized_pnl

        # ── 只卖模式自动退出检查 ──
        if self._sell_only_mode and self._coin:
            try:
                base = self._coin.split("_")[0] if "_" in self._coin else self._coin
                qty = self.client.get_spot_position(base)
                ticker = self.client.get_ticker()
                price = float(ticker["last"])
                cap_pct = config.POSITION_CAP_PCT.get(self._coin, 1.5)
                cap_val = cap_pct * config.COIN_CONFIG.get(self._coin, {}).get("initial_capital", 5000)
                if qty * price < cap_val * 0.5:
                    self._sell_only_mode = False
                    logger.info(f"[仓位上限] {self._coin} 持仓 ${qty*price:.0f} < 上限${cap_val:.0f}×50%，恢复正常网格")
                    # 清掉剩余卖单，下次tick自然会重建买单
                    for p, info in list(self.orders.items()):
                        if info["side"] == "sell":
                            try:
                                self.client.cancel_order(info["order_id"])
                            except Exception:
                                pass
                            del self.orders[p]
                    self.pending_sells.clear()
            except Exception:
                pass

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
                    # 只卖模式卖单(buy_cost<=0): 用OKX持仓均价计算盈亏
                    if buy_cost <= 0 and self._sell_only_mode:
                        try:
                            base = self._coin.split("_")[0] if "_" in self._coin else self._coin
                            buy_cost = self.client.get_avg_cost(self.symbol) or filled_price
                        except Exception:
                            buy_cost = filled_price
                    fee = filled_size * (buy_cost + filled_price) * config.SPOT_FEE_RATE
                    pnl = filled_size * (filled_price - buy_cost) - fee
                    realized_pnl += pnl
                    tag = "(减仓)" if self._sell_only_mode else ""
                    logger.info(f"网格盈亏{tag}: {pnl:+.4f} USDT（买 {buy_cost:.6f} → 卖 {filled_price:.6f}）")
                    notify.send_tg(
                        f"💰 网格成交{tag} [{self.symbol}]\n"
                        f"买 {buy_cost:.6f} → 卖 {filled_price:.6f}\n"
                        f"盈亏: {pnl:+.4f} USDT"
                    )

                # 在下方一格补挂买单（只卖模式下不补买；仓位上限检查 + SOL 限仓检查）
                if self._sell_only_mode:
                    continue  # 只卖模式：卖完不补买
                active_buys = sum(1 for info in self.orders.values()
                                  if isinstance(info, dict) and info.get("side") == "buy")
                held = len(self.pending_sells)
                
                # 仓位市值上限检查
                pos_capped = False
                if self._coin:
                    cap_pct = config.POSITION_CAP_PCT.get(self._coin, 1.5)
                    pos_cap_value = cap_pct * config.COIN_CONFIG.get(self._coin, {}).get("initial_capital", 5000)
                    try:
                        base = self._coin.split("_")[0] if "_" in self._coin else self._coin
                        qty = self.client.get_spot_position(base)
                        val = qty * filled_price
                        pos_capped = val >= pos_cap_value * 0.7
                    except Exception:
                        pass
                
                if idx > 0 and (active_buys + held) < self._max_entries and not pos_capped:
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
                buy_cost = pos["buy_cost"]
                # 只卖模式标记(-1): 用OKX持仓均价
                if buy_cost <= 0:
                    try:
                        base = self._coin.split("_")[0] if "_" in self._coin else self._coin
                        buy_cost = self.client.get_avg_cost(self.symbol) or market_price
                    except Exception:
                        buy_cost = market_price
                self.client.place_order("sell", market_price, pos["size"], order_type="market")
                fee = pos["size"] * (buy_cost + market_price) * config.SPOT_FEE_RATE
                pnl = pos["size"] * (market_price - buy_cost) - fee
                total_pnl += pnl
                logger.info(
                    f"[网格平仓] 卖出 {pos['size']} {self.symbol} @ {market_price:.4f} "
                    f"买成本 {pos['buy_cost']:.4f} 盈亏 {pnl:+.4f} USDT"
                )
            except Exception as e:
                logger.error(f"[网格平仓] 市价平仓失败: {e}")

        return total_pnl
