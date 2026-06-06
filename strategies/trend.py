"""
趋势策略：适用于单边行情
- 上涨趋势：EMA20 > EMA60 时做多
- 下跌趋势：不做空（现货），空仓等待
- 止盈/止损优先使用 ATR 动态计算；无 ATR 时退回固定比例
- 移动止损：价格从最高点回撤 TREND_TRAILING_PCT 时平仓，让利润奔跑
- RSI 超买过滤：RSI > TREND_RSI_OB 时不追高入场
"""
import math
import logging
import config
import notify

logger = logging.getLogger("trend")

COOLDOWN_TICKS = 5


class TrendStrategy:
    def __init__(self, client, capital, size_decimals=0, ccy=""):
        self.client = client
        self.capital = capital
        self._size_dec = size_decimals
        self.ccy = ccy

        self.position = 0.0
        self.entry_price = 0.0
        self.order_id = None
        self.running = False
        self.regime = None
        self._cooldown = 0

        # 动态止损/止盈（ATR 模式）
        self._stop_price = 0.0
        self._tp_price = 0.0
        # 移动止损
        self._highest_price = 0.0
        self._can_reenter = True  # 止损后禁止 on_tick 自动再进场，等待下次 start()

    def _rsi_ob(self):
        base = self.ccy.split("-")[0] if "-" in self.ccy else self.ccy
        return getattr(config, f"{base}_TREND_RSI_OB", config.TREND_RSI_OB)

    def start(self, regime, current_price, capital=None, indicators=None):
        self.running = True
        self.regime = regime
        self._cooldown = 0
        self._can_reenter = True  # 每次 start() 重新允许在本 regime 周期内等待入场
        if capital is not None:
            self.capital = capital
        if regime == "trending_up":
            rsi = (indicators or {}).get("rsi", 50)
            if rsi >= self._rsi_ob():
                logger.info(f"RSI={rsi:.1f} 超买，暂不追高入场，等待回调")
                return
            atr = (indicators or {}).get("atr")
            self._open_long(current_price, atr)
        else:
            logger.info("趋势向下，现货不做空，等待")

    def _open_long(self, price, atr=None):
        usdt_to_use = self.capital * config.TREND_POSITION_PCT
        raw = usdt_to_use / price
        if self._size_dec == 0:
            size = int(raw)
        else:
            factor = 10 ** self._size_dec
            size = math.floor(raw * factor) / factor
        if size <= 0:
            logger.warning("资金不足，无法开仓")
            return
        try:
            order_id = self.client.place_order("buy", price, size, order_type="market")
            self.order_id = order_id
            self.entry_price = price
            self.position = size
            self._highest_price = price

            if atr and atr > 0:
                self._stop_price = price - atr * config.TREND_ATR_STOP_MULT
                self._tp_price   = price + atr * config.TREND_ATR_TP_MULT
                logger.info(
                    f"趋势做多: {size} {self.client.symbol.split('-')[0]} @ {price}  "
                    f"止损={self._stop_price:.5f}  止盈={self._tp_price:.5f}  "
                    f"(ATR={atr:.5f})"
                )
            else:
                self._stop_price = price * (1 - config.TREND_STOP_LOSS_PCT)
                self._tp_price   = price * (1 + config.TREND_TAKE_PROFIT_PCT)
                logger.info(
                    f"趋势做多: {size} {self.client.symbol.split('-')[0]} @ {price}  "
                    f"止损={self._stop_price:.5f}  止盈={self._tp_price:.5f}  (固定比例)"
                )
            notify.send_tg(
                f"🚀 趋势开多 [{self.client.symbol}]\n"
                f"价格: {price:.6f}  数量: {size}\n"
                f"止损: {self._stop_price:.5f}  止盈: {self._tp_price:.5f}"
            )
        except Exception as e:
            logger.error(f"开仓失败: {e}")

    def on_tick(self, current_price, indicators=None):
        """检查持仓，触发止盈/止损/移动止损；空仓冷却结束后自动重新入场"""
        if not self.running:
            return 0.0

        if self._cooldown > 0:
            self._cooldown -= 1
            return 0.0

        if self.position <= 0:
            if not self._can_reenter:
                return 0.0
            if self.regime == "trending_up":
                rsi = (indicators or {}).get("rsi", 50)
                if rsi < self._rsi_ob():
                    atr = (indicators or {}).get("atr")
                    self._open_long(current_price, atr)
                else:
                    logger.debug(f"RSI={rsi:.1f} 超买，等待回调再入场")
            return 0.0

        # 更新移动止损最高点
        if current_price > self._highest_price:
            self._highest_price = current_price

        # 方案A：阶梯移动止损 —— 浮盈小用紧止损，浮盈大给更多空间
        profit_pct = (self._highest_price - self.entry_price) / self.entry_price
        if profit_pct < 0.01:
            trail = 0.005   # 0.5%  浮盈<1% 紧咬
        elif profit_pct < 0.03:
            trail = 0.01    # 1.0%  浮盈1-3%
        elif profit_pct < 0.05:
            trail = 0.015   # 1.5%  浮盈3-5%
        else:
            trail = 0.025   # 2.5%  浮盈>5% 让利润跑

        trailing_drawdown = (self._highest_price - current_price) / self._highest_price
        if trailing_drawdown >= trail:
            logger.info(
                f"移动止损触发: 最高={self._highest_price:.5f} → 当前={current_price:.5f} "
                f"回撤={trailing_drawdown:.4%} (浮盈={profit_pct:.2%} trail={trail:.1%})"
            )
            return self._close_position(current_price)

        # 固定止盈
        if current_price >= self._tp_price:
            logger.info(f"止盈触发 @ {current_price:.5f}（目标={self._tp_price:.5f}）")
            return self._close_position(current_price)

        # 固定止损
        if current_price <= self._stop_price:
            logger.info(f"止损触发 @ {current_price:.5f}（止损线={self._stop_price:.5f}）")
            return self._close_position(current_price)

        return 0.0

    def _close_position(self, price):
        if self.position <= 0:
            return 0.0
        try:
            self.client.place_order("sell", price, self.position, order_type="market")
            pnl = self.position * (price - self.entry_price)
            logger.info(f"平仓: {self.position} {self.client.symbol.split('-')[0]} @ {price}，盈亏: {pnl:.4f} USDT")
            emoji = "💰" if pnl >= 0 else "🔴"
            notify.send_tg(
                f"{emoji} 趋势平仓 [{self.client.symbol}]\n"
                f"价格: {price:.6f}  数量: {self.position}\n"
                f"盈亏: {pnl:+.4f} USDT"
            )
            self.position = 0.0
            self.entry_price = 0.0
            self._stop_price = 0.0
            self._tp_price = 0.0
            self._highest_price = 0.0
            self._cooldown = COOLDOWN_TICKS
            self._can_reenter = False  # 止损/止盈后禁止 on_tick 自动再进场，等下次 start()
            return pnl
        except Exception as e:
            logger.error(f"平仓失败: {e}")
            # 余额不足说明仓位已不在，主动核实并同步状态
            if "insufficient" in str(e).lower():
                try:
                    base = self.client.symbol.split("-")[0]
                    actual = self.client.get_spot_position(base)
                    if actual < self.position * 0.5:
                        logger.warning(f"交易所余额 {actual} < 预期 {self.position}，同步清零内部仓位")
                        self.position = 0.0
                        self.entry_price = 0.0
                        self._stop_price = 0.0
                        self._tp_price = 0.0
                        self._highest_price = 0.0
                        self._cooldown = COOLDOWN_TICKS
                except Exception as e2:
                    logger.error(f"核实仓位失败: {e2}")
            return 0.0

    def stop(self, keep_sells=False):
        self.running = False
        self.regime  = None
        pnl = 0.0
        if self.position > 0:
            try:
                ticker = self.client.get_ticker()
                price  = float(ticker["last"])
                pnl = self._close_position(price)
            except Exception as e:
                logger.error(f"趋势策略停止平仓失败: {e}（残仓将由下次启动清理）")
        logger.info("趋势策略停止")
        return pnl
