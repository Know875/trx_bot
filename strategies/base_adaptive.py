"""
TRX 自适应策略基类 — 现货/合约共享的状态机引擎

核心设计：
  IDLE → (ranging) → GRID_RUNNING → (vol burst / trend signal) → TREND_CONFIRM → TREND_RUNNING
       → (trend break) → IDLE

子类只需覆写交易接口：开仓/平仓/挂单/撤单/获取合约面值/计算数量
"""
import math
import logging

import config
import notify

logger = logging.getLogger("base_adaptive")

# 状态枚举
IDLE           = "idle"
GRID_RUNNING   = "grid"
TREND_CONFIRM  = "trend_confirm"
TREND_RUNNING  = "trend"


class BaseAdaptiveStrategy:
    """
    TRX 专属自适应策略基类。
    
    子类必须实现:
      - _place_order(side, price, size, order_type="limit") → order_id
      - _place_market_order(side, size) → order_id
      - _cancel_order(order_id)
      - _cancel_all_pending()
      - _get_ct_val() → float  (合约返回面值，现货返回 None 或 1)
      - _calc_trade_size(usdt, price) → int/float  (现货返回币数，合约返回张数)
      - _calc_pnl(side, size, entry, exit) → float
      - _place_algo_stop(side, size, price) → algo_id | None
      - _cancel_algo_orders()
      - _get_pending_order_ids() → set[str]
      - _get_order(order_id) → dict
    """
    
    def __init__(self, client, capital):
        self.client  = client
        self.capital = capital
        self.running = False
        self._is_dead_grid = False

        self._state  = IDLE
        self._regime = None

        # 网格
        self._grid_prices  = []
        self._buy_orders   = {}   # price → {order_id, size}
        self._sell_orders  = {}   # price → {order_id, size, buy_cost?}

        # 趋势
        self._trend_dir   = None
        self._entry_price = 0.0
        self._position    = 0
        self._tp1_done    = False
        self._stop_price  = 0.0
        self._tp1_price   = 0.0

        # 网格补单失败计数（避免刷屏）
        self._grid_fail = {"sell": 0, "buy": 0}
        self._tp2_price   = 0.0
        self._best_price  = 0.0
        self._cooldown    = 0

        # 确认等待
        self._confirm_ticks = 0
        self._confirm_dir   = None
        self._algo_id       = None

    # ══════════════════════════════════════════════════
    # 公开接口
    # ══════════════════════════════════════════════════

    def start(self, regime, current_price, capital=None, indicators=None):
        self.running = True
        self._regime = regime
        self._cooldown = 0
        if capital is not None:
            self.capital = capital
        self._on_start_hook()

        if regime == "ranging":
            self._start_grid(current_price, indicators)
        elif regime in ("trending_up", "trending_down"):
            direction = "up" if regime == "trending_up" else "down"
            self._begin_confirm(direction, current_price, indicators or {})

    def on_tick(self, current_price, indicators=None):
        if not self.running:
            return 0.0
        if self._cooldown > 0:
            self._cooldown -= 1
            return 0.0
        ind = indicators or {}
        pnl = 0.0
        if self._state == GRID_RUNNING:
            pnl = self._tick_grid(current_price, ind)
        elif self._state == TREND_CONFIRM:
            pnl = self._tick_confirm(current_price, ind)
        elif self._state == TREND_RUNNING:
            pnl = self._tick_trend(current_price, ind)
        elif self._state == IDLE:
            if self._regime == "ranging" and self._cooldown == 0:
                self._start_grid(current_price, ind)
        return pnl

    def stop(self):
        self.running = False
        self._regime = None
        price = self._get_current_price()
        pnl = 0.0
        if self._state == GRID_RUNNING:
            pnl = self._cancel_grid(price)
        elif self._state == TREND_RUNNING and self._position > 0:
            pnl = self._close_all(price, "策略停止")
        self._state = IDLE
        logger.info("自适应策略停止")
        return pnl

    # ══════════════════════════════════════════════════
    # 子类钩子（可覆写）
    # ══════════════════════════════════════════════════

    def _on_start_hook(self):
        """启动时额外操作（如设置杠杆）"""
        pass

    def _allow_short(self) -> bool:
        """是否支持做空（现货返回 False）"""
        return False

    def _grid_upper_label(self) -> str:
        return "卖"

    def _grid_lower_label(self) -> str:
        return "买"

    def _log_grid_entry(self, side: str, size, price: float):
        logger.info(f"[网格] 挂{side}单 {size} @ {price:.6f}")

    def _log_grid_fill(self, side: str, size, price: float):
        logger.info(f"[网格] {side}成交 @ {price:.6f}")

    def _log_grid_pnl(self, size, buy_cost: float, sell_price: float, pnl: float):
        logger.info(f"[网格] 卖出成交 @ {sell_price:.6f}  盈亏 {pnl:+.4f} USDT")

    def _notify_grid_pnl(self, buy_cost: float, sell_price: float, pnl: float):
        notify.send_tg(
            f"💰 网格成交 [TRX]\n"
            f"买 {buy_cost:.6f} → 卖 {sell_price:.6f}\n"
            f"盈亏: {pnl:+.4f} USDT"
        )

    def _notify_trend_open(self, direction: str, size, price: float, stop: float, tp1: float, tag: str = ""):
        msg = f"[趋势] 开多 {size} @ {price:.6f}{tag}  止损={stop:.6f}  T1={tp1:.6f}"
        logger.info(msg)

    def _notify_trend_tp1(self, size, price: float, gain: float):
        notify.send_tg(
            f"📈 趋势T1止盈 [TRX]\n"
            f"入场 {self._entry_price:.6f} → 出场 {price:.6f}\n"
            f"平仓{size}  盈亏: {gain:+.4f} USDT"
        )

    def _notify_trend_close(self, reason: str, size, price: float, gain: float):
        emoji = "🎯" if "止盈" in reason else "🛑"
        notify.send_tg(
            f"{emoji} 趋势{reason} [TRX]\n"
            f"入场 {self._entry_price:.6f} → 出场 {price:.6f}\n"
            f"盈亏: {gain:+.4f} USDT"
        )

    # ══════════════════════════════════════════════════
    # 抽象接口（子类必须实现）
    # ══════════════════════════════════════════════════

    def _place_order(self, side, price, size, order_type="limit"):
        raise NotImplementedError

    def _place_market_order(self, side, size):
        raise NotImplementedError

    def _cancel_order(self, order_id):
        raise NotImplementedError

    def _cancel_all_pending(self):
        raise NotImplementedError

    def _get_ct_val(self):
        return 1.0

    def _calc_trade_size(self, usdt, price):
        raise NotImplementedError

    def _calc_pnl(self, size, entry, exit_price, side="long"):
        raise NotImplementedError

    def _place_algo_stop(self, size, price):
        return None

    def _cancel_algo_orders(self):
        pass

    def _get_pending_order_ids(self):
        raise NotImplementedError

    def _get_order(self, order_id):
        raise NotImplementedError

    def _get_current_price(self):
        try:
            ticker = self.client.get_ticker()
            return float(ticker["last"])
        except Exception:
            return 0.0

    # ══════════════════════════════════════════════════
    # 网格逻辑
    # ══════════════════════════════════════════════════

    def _start_grid(self, current_price, indicators=None):
        from strategies.trx_utils import is_asia_peak, grid_count
        
        ind = indicators or {}
        vwap = ind.get("vwap", 0)
        mid = vwap if vwap > 0 else current_price

        bb_width = ind.get("bb_width", 0.02)
        atr_pct = ind.get("atr_pct", 0.01)
        self._is_dead_grid = (bb_width < config.TRX_DEAD_RANGE_BB_WIDTH and
                              atr_pct < config.TRX_DEAD_RANGE_ATR_PCT)

        grid_range = config.TRX_NARROW_GRID_RANGE_PCT if self._is_dead_grid else config.TRX_GRID_RANGE_PCT
        gc = grid_count()
        capital = self.capital * self._grid_position_pct()

        lower = mid * (1 - grid_range / 2)
        upper = mid * (1 + grid_range / 2)

        step_pct = (upper - lower) / lower / gc
        if step_pct < config.TRX_GRID_MIN_PROFIT_PCT:
            logger.warning(f"网格间距 {step_pct:.4%} < {config.TRX_GRID_MIN_PROFIT_PCT:.4%}")
            self._cooldown = 3
            return

        step = (upper - lower) / gc
        self._grid_prices = [round(lower + i * step, 6) for i in range(gc + 1)]
        self._buy_orders  = {}
        self._sell_orders = {}

        per_level = capital / gc
        total_buy = total_sell = 0

        for price in self._grid_prices:
            if price < current_price:
                size = self._calc_trade_size(per_level, price)
                if size <= 0:
                    continue
                try:
                    oid = self._place_order("buy", price, size)
                    self._buy_orders[price] = {"order_id": oid, "size": size}
                    self._log_grid_entry("买", size, price)
                    total_buy += size
                except Exception as e:
                    logger.error(f"[网格] 挂买单失败 @ {price:.6f}: {e}")
            elif price > current_price:
                size = self._calc_trade_size(per_level, price)
                if size <= 0:
                    continue
                if not self._allow_short() and not self._can_open_sell_at(price, size):
                    continue
                try:
                    oid = self._place_order("sell", price, size)
                    self._sell_orders[price] = {"order_id": oid, "size": size, "buy_cost": price}
                    self._log_grid_entry("卖", size, price)
                    total_sell += size
                except Exception as e:
                    logger.error(f"[网格] 挂卖单失败 @ {price:.6f}: {e}")

        if self._buy_orders or self._sell_orders:
            self._state = GRID_RUNNING
            tag = "死盘窄距" if self._is_dead_grid else ("亚洲高峰" if is_asia_peak() else "非高峰")
            logger.info(f"[网格] {tag} {lower:.6f}~{upper:.6f} 锚点{mid:.6f} "
                        f"买单{len(self._buy_orders)}个({total_buy}) "
                        f"卖单{len(self._sell_orders)}个({total_sell})")
        else:
            logger.error("[网格] 所有挂单失败，回到 IDLE")

    def _grid_position_pct(self):
        return config.TRX_GRID_POSITION_PCT

    def _can_open_sell_at(self, price, size):
        """现货检查余额是否足够"""
        try:
            available = self.client.get_spot_position("TRX")
        except Exception:
            return True
        existing = sum(self._sell_orders[o]["size"] for o in self._sell_orders)
        if size + existing > available:
            remaining = max(available - existing, 0)
            min_sz = config.COIN_CONFIG["TRX"].get("min_order_size", 1)
            if remaining < min_sz:
                logger.info(f"[网格] 余额不足，跳过卖单 @ {price:.6f}")
                return False
        return True

    def _tick_grid(self, current_price, ind):
        realized_pnl = 0.0

        # ── 行情切换检测 ──
        pnl, switched = self._check_grid_to_trend(current_price, ind)
        if switched:
            return pnl

        # ── 网格漂移检测 ──
        if self._buy_orders or self._sell_orders:
            grid_prices = list(self._buy_orders.keys()) + list(self._sell_orders.keys())
            if grid_prices:
                grid_center = (max(grid_prices) + min(grid_prices)) / 2
                grid_range  = (max(grid_prices) - min(grid_prices)) / 2
                drift = abs(current_price - grid_center) / grid_center
                if drift > grid_range / grid_center * 1.5:
                    logger.info(f"[网格] 价格漂移 {drift:.4%}，重新锚定")
                    self._cancel_grid(current_price)
                    self._start_grid(current_price, ind)
                    return 0.0

        # ── 批量查询挂单 ──
        try:
            pending_ids = self._get_pending_order_ids()
        except Exception as e:
            logger.warning(f"[网格] 批量查询挂单失败: {e}")
            return realized_pnl

        # ── 买单成交 ──
        for price in list(self._buy_orders.keys()):
            info = self._buy_orders[price]
            if info["order_id"] in pending_ids:
                continue
            try:
                order = self._get_order(info["order_id"])
            except Exception:
                continue
            if order["state"] != "filled":
                continue
            filled_size = self._get_filled_size(order)
            filled_price = float(order["avgPx"])
            idx = self._grid_prices.index(price) if price in self._grid_prices else -1
            if idx >= 0 and idx + 1 < len(self._grid_prices):
                sell_price = self._grid_prices[idx + 1]
                if sell_price not in self._sell_orders:
                    try:
                        oid = self._place_order("sell", sell_price, filled_size)
                        self._sell_orders[sell_price] = {"order_id": oid, "size": filled_size, "buy_cost": filled_price}
                        del self._buy_orders[price]
                        self._log_grid_fill("买入", filled_size, filled_price)
                        self._grid_fail["sell"] = 0
                    except Exception as e:
                        self._grid_fail["sell"] += 1
                        cnt = self._grid_fail["sell"]
                        if cnt == 1 or cnt % 30 == 0:
                            logger.warning(f"[网格] 补挂卖单连续失败 {cnt} 次: {e}")
                        else:
                            logger.debug(f"[网格] 补挂卖单失败 ({cnt}): {e}")
                else:
                    del self._buy_orders[price]
            else:
                del self._buy_orders[price]

        # ── 卖单成交 ──
        for price in list(self._sell_orders.keys()):
            info = self._sell_orders[price]
            if info["order_id"] in pending_ids:
                continue
            try:
                order = self._get_order(info["order_id"])
            except Exception:
                continue
            if order["state"] != "filled":
                continue
            filled_size = self._get_filled_size(order)
            filled_price = float(order["avgPx"])
            buy_cost = info.get("buy_cost", 0)
            is_initial = self._is_initial_sell(buy_cost, price)

            if is_initial:
                del self._sell_orders[price]
                self._log_grid_fill(f"{self._grid_upper_label()}初始成交", filled_size, filled_price)
                self._on_initial_sell_fill(price, filled_size)
            else:
                del self._sell_orders[price]
                pnl = self._calc_pnl(filled_size, buy_cost, filled_price, "long")
                realized_pnl += pnl
                self._log_grid_pnl(filled_size, buy_cost, filled_price, pnl)
                self._notify_grid_pnl(buy_cost, filled_price, pnl)
                self._on_grid_sell_fill(price, filled_size)
        return realized_pnl

    def _get_filled_size(self, order):
        return float(order["sz"])

    def _is_initial_sell(self, buy_cost, price):
        return buy_cost == price

    def _on_initial_sell_fill(self, price, size):
        """现货初始化卖单成交后不补买单（因没有买过）"""
        idx = self._grid_prices.index(price) if price in self._grid_prices else -1
        if idx > 0:
            buy_price = self._grid_prices[idx - 1]
            if buy_price not in self._buy_orders:
                try:
                    oid = self._place_order("buy", buy_price, size)
                    self._buy_orders[buy_price] = {"order_id": oid, "size": size}
                    logger.info(f"[网格] 卖单成交→补挂买单 @ {buy_price:.6f}")
                    self._grid_fail["buy"] = 0
                except Exception as e:
                    self._grid_fail["buy"] += 1
                    cnt = self._grid_fail["buy"]
                    if cnt == 1 or cnt % 30 == 0:
                        logger.warning(f"[网格] 补挂买单连续失败 {cnt} 次: {e}")
                    else:
                        logger.debug(f"[网格] 补挂买单失败 ({cnt}): {e}")

    def _on_grid_sell_fill(self, price, size):
        idx = self._grid_prices.index(price) if price in self._grid_prices else -1
        if idx > 0:
            buy_price = self._grid_prices[idx - 1]
            if buy_price not in self._buy_orders:
                try:
                    oid = self._place_order("buy", buy_price, size)
                    self._buy_orders[buy_price] = {"order_id": oid, "size": size}
                    logger.info(f"[网格] 补挂买单 @ {buy_price:.6f}")
                    self._grid_fail["buy"] = 0
                except Exception as e:
                    self._grid_fail["buy"] += 1
                    cnt = self._grid_fail["buy"]
                    if cnt == 1 or cnt % 30 == 0:
                        logger.warning(f"[网格] 补挂买单连续失败 {cnt} 次: {e}")
                    else:
                        logger.debug(f"[网格] 补挂买单失败 ({cnt}): {e}")

    def _check_grid_to_trend(self, current_price, ind):
        """成交量爆发 / 趋势切换检测 → 返回 (pnl, is_switched)"""
        vol_ratio = ind.get("vol_ratio", 1.0)
        bb_width  = ind.get("bb_width", 0.02)
        vol_threshold = config.TRX_DEAD_VOL_BURST_RATIO if self._is_dead_grid else config.TRX_VOL_BURST_RATIO

        # 成交量爆发
        if vol_ratio > vol_threshold and bb_width > 0.020:
            prev_bb = ind.get("bb_width_prev", bb_width)
            expand = (bb_width - prev_bb) / max(prev_bb, 0.001)
            if expand > config.TRX_VOL_BURST_BB_EXPAND:
                ema_diff = (ind.get("ema20", 0) - ind.get("ema60", 1)) / ind.get("ema60", 1)
                if abs(ema_diff) > config.TRX_EMA_DIFF_GRID_TO_TREND:
                    direction = "up" if ema_diff > 0 else "down"
                    logger.info(f"[网格→趋势] 成交量爆发! vol={vol_ratio:.1f}x 方向={direction}")
                    pnl = self._cancel_grid(current_price)
                    self._begin_confirm(direction, current_price, ind)
                    return pnl, True

        # ADX 趋势
        adx = ind.get("adx", 0)
        if adx > config.TRX_ADX_TREND_MIN:
            ema_diff = (ind.get("ema20", 0) - ind.get("ema60", 1)) / ind.get("ema60", 1)
            if abs(ema_diff) > config.TRX_EMA_DIFF_GRID_TO_TREND and vol_ratio > config.TRX_VOL_CONFIRM_RATIO:
                direction = "up" if ema_diff > 0 else "down"
                logger.info(f"[网格→趋势确认] ADX={adx:.1f} 偏离={ema_diff:.4%}")
                pnl = self._cancel_grid(current_price)
                self._begin_confirm(direction, current_price, ind)
                return pnl, True
        return 0.0, False

    def _cancel_grid(self, current_price=None):
        if current_price is None or current_price <= 0:
            current_price = self._get_current_price()
        self._cancel_all_pending()
        logger.info("[网格] 已撤销所有挂单")
        total_pnl = self._close_pending_positions(current_price)
        self._buy_orders.clear()
        self._sell_orders.clear()
        return total_pnl

    def _close_pending_positions(self, price):
        return 0.0

    # ══════════════════════════════════════════════════
    # 趋势确认
    # ══════════════════════════════════════════════════

    def _begin_confirm(self, direction, price, ind):
        if not self._allow_short() and direction != "up":
            logger.info(f"[确认等待] 不支持做空，回到 IDLE")
            self._state = IDLE
            self._cooldown = config.TRX_COOLDOWN_TICKS
            return
        self._confirm_dir = direction
        self._confirm_ticks = 0
        self._total_confirm = config.TRX_BREAKOUT_CONFIRM_TICKS if 0.33 < price < 0.35 else config.TRX_UNIVERSAL_CONFIRM_TICKS
        self._state = TREND_CONFIRM
        logger.info(f"[确认等待] 方向={direction} 需{self._total_confirm}tick")

    def _tick_confirm(self, current_price, ind):
        self._confirm_ticks += 1
        adx      = ind.get("adx", 0)
        rsi      = ind.get("rsi", 50)
        vol_r    = ind.get("vol_ratio", 1.0)
        direction = self._confirm_dir

        # 只对允许的方向计算 ema_diff
        ema12, ema26 = ind.get("ema12", 1), ind.get("ema26", 1)
        ema_diff = (ema12 - ema26) / max(ema26, 1e-10)

        if direction == "up":
            still_valid = adx > 22 and ema_diff > config.TRX_EMA_DIFF_CONFIRM_MIN
            if not still_valid:
                logger.info(f"[确认等待] 信号消失 (ADX={adx:.1f})，回到 IDLE")
                self._state = IDLE
                self._cooldown = config.TRX_COOLDOWN_TICKS
                return 0.0
            if self._confirm_ticks >= self._total_confirm:
                if rsi > config.TRX_RSI_ENTRY_MAX:
                    logger.info(f"[确认等待] RSI={rsi:.1f} 过高，放弃追多")
                    self._state = IDLE
                    self._cooldown = config.TRX_COOLDOWN_TICKS
                    return 0.0
                self._open_trend(direction, current_price, ind,
                                 half_size=(vol_r > config.TRX_ABNORMAL_VOL_RATIO))
        elif direction == "down" and self._allow_short():
            still_valid = adx > 22 and ema_diff < -config.TRX_EMA_DIFF_CONFIRM_MIN
            if not still_valid:
                logger.info(f"[确认等待] 信号消失，回到 IDLE")
                self._state = IDLE
                self._cooldown = config.TRX_COOLDOWN_TICKS
                return 0.0
            if self._confirm_ticks >= self._total_confirm:
                if rsi < config.TRX_RSI_ENTRY_MIN:
                    logger.info(f"[确认等待] RSI={rsi:.1f} 过低，放弃追空")
                    self._state = IDLE
                    self._cooldown = config.TRX_COOLDOWN_TICKS
                    return 0.0
                self._open_trend(direction, current_price, ind,
                                 half_size=(vol_r > config.TRX_ABNORMAL_VOL_RATIO))
        else:
            if self._confirm_ticks >= self._total_confirm:
                self._state = IDLE
                self._cooldown = config.TRX_COOLDOWN_TICKS
        return 0.0

    # ══════════════════════════════════════════════════
    # 趋势开仓
    # ══════════════════════════════════════════════════

    def _trend_position_pct(self):
        return config.TRX_TREND_POSITION_PCT

    def _open_trend(self, direction, price, ind, half_size=False):
        atr = ind.get("atr", price * 0.005)
        pos_pct = self._trend_position_pct() * (0.5 if half_size else 1.0)
        usdt = self.capital * pos_pct
        size = self._calc_trade_size(usdt, price)
        if size <= 0:
            logger.warning("[趋势] 资金不足，无法开仓")
            self._state = IDLE
            return
        side = "buy" if direction == "up" else "sell"
        try:
            self._place_market_order(side, size)
        except Exception as e:
            logger.error(f"[趋势] 开仓失败: {e}")
            self._state = IDLE
            return

        mult_sl  = config.TRX_TREND_ATR_STOP_MULT
        mult_tp1 = config.TRX_TREND_ATR_TP1_MULT
        mult_tp2 = config.TRX_TREND_ATR_TP2_MULT

        self._trend_dir   = direction
        self._entry_price = price
        self._position    = size
        self._tp1_done    = False
        self._best_price  = price
        if direction == "up":
            self._stop_price = price - atr * mult_sl
            self._tp1_price  = price + atr * mult_tp1
            self._tp2_price  = price + atr * mult_tp2
        else:
            self._stop_price = price + atr * mult_sl
            self._tp1_price  = price - atr * mult_tp1
            self._tp2_price  = price - atr * mult_tp2
        self._state = TREND_RUNNING

        warn = "（放量异常，半仓）" if half_size else ""
        self._notify_trend_open(direction, size, price, self._stop_price, self._tp1_price, warn)

        try:
            self._algo_id = self._place_algo_stop(size, self._stop_price)
            if self._algo_id:
                logger.info(f"[趋势] 交易所止损单已挂 @ {self._stop_price:.6f}")
        except Exception as e:
            logger.warning(f"[趋势] 交易所止损单挂单失败: {e}")

    # ══════════════════════════════════════════════════
    # 趋势持仓管理
    # ══════════════════════════════════════════════════

    def _tick_trend(self, current_price, ind):
        if self._position <= 0:
            self._state = IDLE
            return 0.0
        pnl = 0.0
        d = self._trend_dir
        if (d == "up" and current_price > self._best_price) or \
           (d == "down" and current_price < self._best_price):
            self._best_price = current_price

        # T1 部分止盈
        if not self._tp1_done:
            hit = (d == "up" and current_price >= self._tp1_price) or \
                  (d == "down" and current_price <= self._tp1_price)
            if hit:
                partial = self._calc_partial(self._position, config.TRX_TREND_TP1_PCT)
                if partial > 0:
                    close_side = "sell" if d == "up" else "buy"
                    try:
                        self._place_market_order(close_side, partial)
                        gain = self._calc_pnl(partial, self._entry_price, current_price, d)
                        pnl += gain
                        self._position -= partial
                        self._tp1_done = True
                        logger.info(f"[趋势] T1 部分止盈 {partial} @ {current_price:.6f}  +{gain:.4f} USDT")
                        self._notify_trend_tp1(partial, current_price, gain)
                    except Exception as e:
                        logger.error(f"[趋势] T1 止盈失败: {e}")
                else:
                    self._tp1_done = True
                return pnl

        trailing_dd = abs(self._best_price - current_price) / max(self._best_price, 1e-10)
        stop_hit = (d == "up" and current_price <= self._stop_price) or \
                   (d == "down" and current_price >= self._stop_price)
        tp2_hit = (d == "up" and current_price >= self._tp2_price) or \
                  (d == "down" and current_price <= self._tp2_price)

        if trailing_dd >= config.TRX_TREND_TRAILING_PCT or stop_hit or tp2_hit:
            reason = "T2 止盈" if tp2_hit else ("移动止损" if trailing_dd >= config.TRX_TREND_TRAILING_PCT else "止损")
            pnl += self._close_all(current_price, reason)
        return pnl

    def _close_all(self, price, reason="平仓"):
        if self._position <= 0:
            return 0.0
        self._cancel_algo_orders()
        try:
            self._place_market_order("sell" if self._trend_dir == "up" else "buy", self._position)
        except Exception as e:
            logger.error(f"[趋势] 平仓失败: {e}")
            return 0.0
        gain = self._calc_pnl(self._position, self._entry_price, price, self._trend_dir)
        logger.info(f"[趋势] {reason} 平仓 {self._position} @ {price:.6f}  {gain:+.4f} USDT")
        self._notify_trend_close(reason, self._position, price, gain)
        self._position = self._entry_price = self._stop_price = self._tp1_price = self._tp2_price = self._best_price = 0.0
        self._trend_dir = None
        self._tp1_done = False
        self._state = IDLE
        self._cooldown = config.TRX_COOLDOWN_TICKS
        return gain

    # ══════════════════════════════════════════════════
    # 工具
    # ══════════════════════════════════════════════════

    def _calc_partial(self, total, ratio):
        return int(total * ratio)
