"""
合约趋势策略（支持做多/做空）
- trending_up:   做多（buy 开仓）
- trending_down: 做空（sell 开仓）—— 与现货策略的关键区别
- 止盈/止损/移动止损逻辑与现货趋势策略相同
- 仓位以合约张数计，通过 ctVal 换算资金量
"""
import math
import logging
import config
import notify

logger = logging.getLogger("futures_trend")

COOLDOWN_TICKS = 5


class FuturesTrendStrategy:
    def __init__(self, client, capital, ccy=""):
        self.client  = client
        self.capital = capital
        self.ccy = ccy

        self.position    = 0      # 合约张数（>0 表示有持仓）
        self.pos_side    = None   # "long" 或 "short"
        self.entry_price = 0.0
        self.running     = False
        self.regime      = None
        self._cooldown   = 0

        self._stop_price  = 0.0
        self._tp_price    = 0.0
        self._best_price  = 0.0   # long: 最高价；short: 最低价（移动止损用）
        self._ct_val      = None
        self._can_reenter = True  # 止损后禁止 on_tick 自动再进场，等待下次 start()

    # ── 内部工具 ──────────────────────────────────────────────

    def _get_ct_val(self):
        if self._ct_val is None:
            self._ct_val = self.client.get_ct_val()
            if self._ct_val <= 0:
                raise RuntimeError(f"无效合约面值: {self._ct_val}，拒绝开仓")
        return self._ct_val

    def _calc_contracts(self, price):
        ct_val   = self._get_ct_val()
        usdt     = self.capital * config.TREND_POSITION_PCT * config.get_leverage(self.ccy)
        contracts = usdt / (price * ct_val)
        return max(1, math.floor(contracts))

    def _rsi_ob(self):
        base = self.ccy.split("-")[0] if "-" in self.ccy else self.ccy
        return getattr(config, f"{base}_TREND_RSI_OB", config.TREND_RSI_OB)

    def _rsi_os(self):
        base = self.ccy.split("-")[0] if "-" in self.ccy else self.ccy
        return getattr(config, f"{base}_TREND_RSI_OS", config.TREND_RSI_OS)

    # ── 公开接口 ──────────────────────────────────────────────

    def start(self, regime, current_price, capital=None, indicators=None):
        self.running = True
        self.regime  = regime
        self._cooldown = 0
        self._can_reenter = True  # 每次 start() 重新允许在本 regime 周期内等待入场
        if capital is not None:
            self.capital = capital

        rsi = (indicators or {}).get("rsi", 50)
        atr = (indicators or {}).get("atr")

        if regime == "trending_up":
            if rsi >= self._rsi_ob():
                logger.info(f"RSI={rsi:.1f} 超买，暂不追多入场，等待回调")
                return
            self._open_position("long", current_price, atr)
        elif regime == "trending_down":
            if rsi <= self._rsi_os():
                logger.info(f"RSI={rsi:.1f} 超卖，暂不追空入场，等待反弹")
                return
            self._open_position("short", current_price, atr)

    def on_tick(self, current_price, indicators=None):
        if not self.running:
            return 0.0
        if self._cooldown > 0:
            self._cooldown -= 1
            return 0.0

        if self.position <= 0:
            if not self._can_reenter:
                # 本 regime 周期已止损，等下次 start() 重新评估后才允许入场
                return 0.0
            rsi = (indicators or {}).get("rsi", 50)
            atr = (indicators or {}).get("atr")
            if self.regime == "trending_up" and rsi < self._rsi_ob():
                self._open_position("long", current_price, atr)
            elif self.regime == "trending_down" and rsi > self._rsi_os():
                self._open_position("short", current_price, atr)
            return 0.0

        # 方案A：阶梯移动止损 —— 浮盈小用紧止损，浮盈大给更多空间
        profit_pct = 0.0
        if self.pos_side == "long":
            if current_price > self._best_price:
                self._best_price = current_price
            profit_pct = (self._best_price - self.entry_price) / self.entry_price
            trailing_dd = (self._best_price - current_price) / self._best_price
        else:
            if current_price < self._best_price:
                self._best_price = current_price
            profit_pct = (self.entry_price - self._best_price) / self.entry_price
            trailing_dd = (current_price - self._best_price) / self._best_price

        if profit_pct < 0.01:
            trail = 0.005
        elif profit_pct < 0.03:
            trail = 0.01
        elif profit_pct < 0.05:
            trail = 0.015
        else:
            trail = 0.025

        if trailing_dd >= trail:
            logger.info(
                f"移动止损触发: 最优={self._best_price:.6f} 当前={current_price:.6f} "
                f"回撤={trailing_dd:.4%} (浮盈={profit_pct:.2%} trail={trail:.1%})"
            )
            return self._close_position(current_price)

        if self.pos_side == "long":
            if current_price >= self._tp_price:
                logger.info(f"多单止盈 @ {current_price:.6f}（目标={self._tp_price:.6f}）")
                return self._close_position(current_price)
            if current_price <= self._stop_price:
                logger.info(f"多单止损 @ {current_price:.6f}（止损={self._stop_price:.6f}）")
                return self._close_position(current_price)
        else:
            if current_price <= self._tp_price:
                logger.info(f"空单止盈 @ {current_price:.6f}（目标={self._tp_price:.6f}）")
                return self._close_position(current_price)
            if current_price >= self._stop_price:
                logger.info(f"空单止损 @ {current_price:.6f}（止损={self._stop_price:.6f}）")
                return self._close_position(current_price)

        return 0.0

    def stop(self):
        self.running = False
        self.regime  = None
        pnl = 0.0
        if self.position > 0:
            try:
                ticker = self.client.get_ticker()
                price  = float(ticker["last"])
                pnl = self._close_position(price)
            except Exception as e:
                logger.error(f"合约趋势策略停止平仓失败: {e}")
                self.position = 0
        logger.info("合约趋势策略停止")
        return pnl

    # ── 私有开平仓 ────────────────────────────────────────────

    def _open_position(self, side, price, atr=None):
        contracts = self._calc_contracts(price)
        if contracts <= 0:
            logger.warning("合约数不足，无法开仓")
            return

        order_side = "buy" if side == "long" else "sell"
        try:
            self.client.set_leverage()
            self.client.place_futures_order(order_side, contracts, order_type="market")
            self.position    = contracts
            self.pos_side    = side
            self.entry_price = price
            self._best_price = price

            if atr and atr > 0:
                mult_sl = config.TREND_ATR_STOP_MULT
                mult_tp = config.TREND_ATR_TP_MULT
                if side == "long":
                    self._stop_price = price - atr * mult_sl
                    self._tp_price   = price + atr * mult_tp
                else:
                    self._stop_price = price + atr * mult_sl
                    self._tp_price   = price - atr * mult_tp
                mode_str = f"ATR={atr:.6f}"
            else:
                if side == "long":
                    self._stop_price = price * (1 - config.TREND_STOP_LOSS_PCT)
                    self._tp_price   = price * (1 + config.TREND_TAKE_PROFIT_PCT)
                else:
                    self._stop_price = price * (1 + config.TREND_STOP_LOSS_PCT)
                    self._tp_price   = price * (1 - config.TREND_TAKE_PROFIT_PCT)
                mode_str = "固定比例"

            direction = "多" if side == "long" else "空"
            logger.info(
                f"合约开{direction}: {contracts}张 @ {price:.6f}  "
                f"止损={self._stop_price:.6f}  止盈={self._tp_price:.6f}  ({mode_str})"
            )
            notify.send_tg(
                f"{'🚀' if side == 'long' else '🔻'} 合约开{direction} [{self.client.symbol}]\n"
                f"价格: {price:.6f}  数量: {contracts}张\n"
                f"止损: {self._stop_price:.6f}  止盈: {self._tp_price:.6f}"
            )
        except Exception as e:
            logger.error(f"合约开仓失败: {e}")

    def _close_position(self, price):
        if self.position <= 0:
            return 0.0
        try:
            self.client.close_futures_position()
            ct_val = self._get_ct_val()
            fee = self.position * ct_val * (self.entry_price + price) * config.FUTURES_FEE_RATE
            if self.pos_side == "long":
                pnl = self.position * ct_val * (price - self.entry_price) - fee
            else:
                pnl = self.position * ct_val * (self.entry_price - price) - fee
            direction = "多" if self.pos_side == "long" else "空"
            logger.info(
                f"合约平{direction}: "
                f"{self.position}张 @ {price:.6f}  盈亏: {pnl:+.4f} USDT"
            )
            emoji = "💰" if pnl >= 0 else "🔴"
            notify.send_tg(
                f"{emoji} 合约平{direction} [{self.client.symbol}]\n"
                f"价格: {price:.6f}  数量: {self.position}张\n"
                f"盈亏: {pnl:+.4f} USDT"
            )
            self.position     = 0
            self.pos_side     = None
            self.entry_price  = 0.0
            self._stop_price  = 0.0
            self._tp_price    = 0.0
            self._best_price  = 0.0
            self._cooldown    = COOLDOWN_TICKS
            self._can_reenter = False  # 止损/止盈平仓后禁止 on_tick 自动再进场
            return pnl
        except Exception as e:
            logger.error(f"合约平仓失败: {e}")
            return 0.0
