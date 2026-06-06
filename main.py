"""
TRX / ETH / SOL 量化交易机器人
每个币种在独立线程中运行，日志写入 bot_{CCY}.log
"""
import time
import logging
import threading
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from okx_client import OKXClient
from market_analyzer import parse_candles, detect_regime, get_range_bounds
from strategies.grid import GridStrategy
from strategies.trend import TrendStrategy
from strategies.futures_trend import FuturesTrendStrategy
from strategies.futures_grid import FuturesGridStrategy
from strategies.trx_adaptive import TRXAdaptiveStrategy
from strategies.trx_adaptive_futures import TRXAdaptiveFuturesStrategy
from tracker import Tracker
from account_guard import Guard
from strategy_report import StrategyReporter
from strategy_guard import StrategyGuard
from notify import send_tg
from ai_advisor import get_ai_regime_hint
from macro import MacroIntelligence, get_macro_adjustment

_macro = MacroIntelligence()
GLOBAL_MACRO = {"risk_score": 0.5, "position_multiplier": 1.0}
MACRO_CHECK_INTERVAL = 300  # 每5分钟更新宏观分析

def _update_macro():
    global GLOBAL_MACRO
    try:
        result = _macro.analyze()
        if result:
            GLOBAL_MACRO = result
    except Exception:
        pass

CHECK_INTERVAL  = 60    # 每 60 秒执行策略
REGIME_INTERVAL = 900   # 每 15 分钟重新识别行情
SWITCH_COOLDOWN = 1800  # 策略切换冷却 30 分钟（避免频繁切换→反复市价平仓）

# 全局防护（本金口径统一来自 config.COIN_CONFIG 合计）
_total_capital = sum(c.get("initial_capital", 0) for c in config.COIN_CONFIG.values())
_guard = Guard(total_capital=_total_capital)
_reporter = StrategyReporter()
_sguard = StrategyGuard()
TREND_REENTRY_COOLDOWN = 900     # 趋势止损后至少等 15 分钟才能重新入场
MAX_CONSECUTIVE_TREND_LOSSES = 3 # 连续止损 N 次后，禁止趋势策略，强制切到网格
V_BOTTOM_CHECK_PRICE_PCT = 0.03  # 价格反向波动 >3% 时，强制重新识别行情（防 V 底追空）

# 现货币种对应的 base currency（用于查询持仓）
_SPOT_BASE_CCY = {"TRX": "TRX", "ETH": "ETH", "SOL": "SOL"}


def _floating_pnl(ccy: str, client, is_futures: bool, price: float = 0.0) -> float:
    """查询当前持仓的未实现盈亏（USDT）。失败返回 0，绝不因此中断交易。
    用于把"持仓浮亏"纳入回撤风控，避免风控只看已实现盈亏而对在途亏损失明。"""
    try:
        if is_futures:
            pos = client.get_futures_position()
            if pos and float(pos.get("pos", 0)) != 0:
                return float(pos.get("upl") or 0)
            return 0.0
        base = _SPOT_BASE_CCY.get(ccy)
        if not base:
            return 0.0
        total = client.get_spot_position(base)
        if total <= 0:
            return 0.0
        detail = client.get_currency_details(base)
        avg = float(detail.get("avgPx") or 0)
        if avg <= 0:
            return 0.0
        if price <= 0:
            price = float(client.get_ticker()["last"])
        return total * (price - avg)
    except Exception:
        return 0.0


def _strat_gate(ccy: str, sname: str):
    """返回某具体策略的 (是否允许开仓, 仓位乘数)。
    被 StrategyGuard 暂停/禁用 → (False, 0)；试运行/减半 → (True, <1)。
    用于在「按 regime 选策略」时真正落实策略级熔断与仓位缩放。
    """
    try:
        r = _sguard.check(ccy, sname)
        return r.get("allowed", True), r.get("position_mult", 1.0)
    except Exception:
        return True, 1.0


def _startup_cleanup(client: OKXClient, ccy: str, logger: logging.Logger, tracker=None):
    """
    启动前清理：撤所有挂单 + 撤所有 algo 止损单。
    检测到现货持仓时，不市价砸盘——挂限价卖单（均价+最低利润），
    让新网格接管或自然成交。避免因重启时机导致巨额滑点亏损。
    """
    logger.info(f"[启动清理] 开始检查 {ccy} 残留挂单和持仓...")
    try:
        client.cancel_algo_orders()
    except Exception as e:
        logger.warning(f"[启动清理] 撤 algo 单失败: {e}")
    try:
        client.cancel_all_orders()
        logger.info("[启动清理] 所有限价挂单已撤销")
    except Exception as e:
        logger.warning(f"[启动清理] 撤限价单失败: {e}")

    # 现货：残留持仓不市价砸，挂限价卖单保本出售
    base = _SPOT_BASE_CCY.get(ccy)
    if base:
        try:
            holding = client.get_spot_position(base)
            ticker  = client.get_ticker()
            price   = float(ticker["last"])
            min_sz  = config.COIN_CONFIG[ccy].get("min_order_size", 1)
            if holding >= min_sz:
                avg_cost = 0.0
                try:
                    avg_cost = client.get_avg_cost()
                except Exception as e:
                    logger.warning(f"[启动清理] 查询买入均价失败: {e}")

                # 限价卖出：成本+0.5% 或 当前价+0.2%，取较高者保本优先
                limit_sell = max(avg_cost * 1.005, price * 1.002) if avg_cost > 0 else price * 1.005
                limit_sell = round(limit_sell, 2)
                logger.warning(
                    f"[启动清理] 检测到残留 {base} 持仓 {holding}（均价 {avg_cost:.4f}），"
                    f"挂限价卖单 @ {limit_sell}，不市价砸盘"
                )
                try:
                    client.place_order("sell", limit_sell, holding)
                    logger.info(f"[启动清理] 已挂限价卖单 {holding} {base} @ {limit_sell}")
                    # 不立即计入盈亏，等成交后由网格 on_tick 或下次清理结算
                    if tracker and avg_cost > 0:
                        logger.info(
                            f"[启动清理] 持仓成本 {avg_cost:.6f} 限价 {limit_sell:.6f}"
                            f" 预期盈亏 {holding * (limit_sell - avg_cost):+.4f} USDT（待成交）"
                        )
                except Exception as e:
                    logger.error(f"[启动清理] 挂限价卖单失败: {e}")
        except Exception as e:
            logger.error(f"[启动清理] 残留持仓检查/平仓失败: {e}")

    logger.info(f"[启动清理] {ccy} 清理完成")


def _make_file_logger(name: str, log_path: Path, also_route: list[str] | None = None) -> logging.Logger:
    """
    创建绑定到文件的 logger（支持自动轮转，每个文件最大10MB，保留3个备份）。
    also_route: 额外的 logger 名列表，它们的日志也写入同一个文件（用于策略子模块）。
    """
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(str(log_path), maxBytes=10*1024*1024, backupCount=3)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        logger.addHandler(fh)

    for extra_name in (also_route or []):
        extra = logging.getLogger(extra_name)
        extra.setLevel(logging.INFO)
        extra.propagate = False
        # 避免重复 handler
        if not any(isinstance(h, logging.FileHandler) and h.baseFilename == fh.baseFilename
                   for h in extra.handlers):
            extra.addHandler(fh)

    return logger


def run_coin(ccy: str, stop_event: threading.Event):
    coin_cfg = config.COIN_CONFIG[ccy]
    symbol          = coin_cfg["symbol"]
    initial_capital = coin_cfg["initial_capital"]
    size_dec        = coin_cfg["size_decimals"]

    log_path = Path(__file__).parent / f"bot_{ccy}.log"
    # 把策略子模块日志路由到各币种文件
    # trx_adaptive 是 TRX 独有 logger，可安全路由；trend 是共享 logger 不路由（避免交叉）
    extra_loggers = ["trx_adaptive", "base_adaptive"] if ccy == "TRX" else []
    logger   = _make_file_logger(f"bot.{ccy}", log_path, also_route=extra_loggers)
    logger.info(f"=== {ccy} 策略线程启动 ===")

    client  = OKXClient(symbol=symbol)
    tracker = Tracker(initial_capital, ccy=ccy)

    _startup_cleanup(client, ccy, logger, tracker=tracker)

    # TRX 使用专属自适应策略（状态机内部管理网格/趋势切换）
    is_trx = (ccy == "TRX")
    trx_strat = TRXAdaptiveStrategy(client, initial_capital, size_decimals=size_dec) if is_trx else None

    current_strategy   = None
    current_regime     = None
    last_regime_check  = 0
    last_switch_time   = 0
    indicators         = {}
    # 趋势连续止损追踪（防 V 底/顶死亡螺旋）
    consecutive_trend_losses = 0
    last_trend_loss_time     = 0

    while not stop_event.is_set():
        now = time.time()

        # ── 全局熔断检查 ──────────────────────────────────
        guard_result = _guard.check()
        coin_result = _guard.coin_check(ccy)
        if guard_result["status"] == "halt":
            logger.warning(f"⚠️ 全局熔断: {guard_result['reason']} — 跳过开仓")
            stop_event.wait(CHECK_INTERVAL)
            continue
        if not coin_result["allowed"]:
            logger.info(f"⚠️ {ccy} 冷却: {coin_result['reason']} — 跳过")
            stop_event.wait(CHECK_INTERVAL)
            continue
        if guard_result["status"] == "protect":
            logger.info(f"🛡️ 保护模式: {guard_result['reason']}")

        # ── 策略级熔断 ──────────────────────────────────
        # 检查本币所有策略，全封则跳过（活一个就继续）
        all_blocked = False
        for sname in StrategyGuard._get_coin_strategies(ccy):
            sr = _sguard.check(ccy, sname)
            if sr["mode"] != "normal" and not sr["allowed"]:
                logger.info(f"  🚫 策略熔断 {ccy}.{sname}: {sr['reason']}")
            else:
                all_blocked = False  # 至少一个可用
                break
        else:
            if StrategyGuard._get_coin_strategies(ccy):
                all_blocked = True
        if all_blocked:
            logger.warning(f"⚠️ {ccy} 所有策略已熔断 — 跳过")
            stop_event.wait(CHECK_INTERVAL)
            continue

        # ── 定期重新识别行情 ──────────────────────────────────
        if now - last_regime_check > REGIME_INTERVAL:
            try:
                raw = client.get_candles()
                df  = parse_candles(raw)
                new_regime, indicators = detect_regime(df, symbol=ccy)
                ticker  = client.get_ticker()
                price   = float(ticker["last"])
                balance = client.get_balance("USDT")

                logger.info(
                    f"行情: {new_regime} | 价格: {price} | "
                    f"RSI={indicators.get('rsi', 0):.1f} | 余额: {balance:.2f} USDT"
                )

                # AI 二次验证（只在trending信号时调用，节省75% API调用）
                if indicators and new_regime in ("trending_up", "trending_down"):
                    ai_hint = get_ai_regime_hint(indicators, new_regime, symbol=symbol)
                    if (ai_hint
                            and ai_hint["regime"] != new_regime
                            and ai_hint["confidence"] >= config.AI_CONFIDENCE_THRESHOLD):
                        logger.info(
                            f"AI 覆盖: {new_regime} → {ai_hint['regime']} "
                            f"(置信度={ai_hint['confidence']:.0%})"
                        )
                        new_regime = ai_hint["regime"]

                logger.info(tracker.summary())

                # 同步策略贡献报表 + 账户守护者
                for rec in tracker.records:
                    key = f"spot_{ccy}:{rec['strategy']}:{rec.get('time','')}"
                    if not hasattr(run_coin, f"_rep_{ccy}"):
                        setattr(run_coin, f"_rep_{ccy}", set())
                    reported = getattr(run_coin, f"_rep_{ccy}")
                    if key not in reported:
                        _reporter.record(ccy, rec["strategy"], rec["pnl"], rec.get("fee", 0))
                        _guard.add_trade(ccy, rec["pnl"])
                        reported.add(key)

                # 风控
                dd = tracker.drawdown()
                if dd >= config.MAX_DRAWDOWN_PCT:
                    logger.warning(f"{ccy} 回撤 {dd:.2%} 超阈值，暂停交易 {SWITCH_COOLDOWN}s")
                    if is_trx:
                        pnl = trx_strat.stop()
                        if pnl != 0:
                            tracker.record(pnl, "trx_adaptive")
                    elif current_strategy:
                        _name, _strat = current_strategy
                        _pnl = _strat.stop()
                        if _pnl:
                            tracker.record(_pnl, _name)
                    current_strategy = None
                    current_regime = None
                    consecutive_trend_losses = 0
                    tracker.reset_drawdown_reference()
                    stop_event.wait(SWITCH_COOLDOWN)
                    if stop_event.is_set():
                        break
                    last_regime_check = 0  # 强制下一轮重检
                    continue  # 回到 while 循环顶部

                if is_trx:
                    # TRX 专属：行情变化时通知自适应策略重新评估（带冷却）
                    if new_regime != current_regime:
                        if now - last_switch_time < SWITCH_COOLDOWN:
                            pass  # 冷却期内不切换
                        else:
                            logger.info(f"TRX 行情切换: {current_regime} → {new_regime}")
                            if trx_strat.running:
                                pnl = trx_strat.stop()
                                if pnl != 0:
                                    tracker.record(pnl, "trx_adaptive")
                                    logger.info(f"盈亏: {pnl:+.4f} | 累计: {tracker.realized_pnl:+.4f} USDT")
                            last_switch_time = now
                            current_regime = new_regime
                            _sg_allowed, _sg_mult = _strat_gate(ccy, "trx_adaptive")
                            if new_regime in ("ranging", "trending_up", "trending_down") and _sg_allowed:
                                trx_strat.start(new_regime, price,
                                                capital=initial_capital * _sg_mult, indicators=indicators)
                                logger.info(f"TRX 自适应策略启动: {new_regime}（仓位×{_sg_mult:.0%}）")
                            else:
                                logger.info("TRX 行情不明朗或策略受限，空仓等待")
                    elif new_regime in ("ranging", "trending_up", "trending_down") and not trx_strat.running:
                        # 行情未变但策略意外停止（如网格全成交后 IDLE），重新启动
                        _sg_allowed, _sg_mult = _strat_gate(ccy, "trx_adaptive")
                        if _sg_allowed:
                            logger.info(f"TRX 策略未运行，重新启动: {new_regime}")
                            trx_strat.start(new_regime, price,
                                            capital=initial_capital * _sg_mult, indicators=indicators)
                else:
                    # 行情切换策略（ETH / SOL）— 带冷却，避免频繁切换市价平仓
                    if new_regime != current_regime:
                        if now - last_switch_time < SWITCH_COOLDOWN:
                            # 冷却期内：不切换，继续用原策略
                            pass
                        else:
                            logger.info(f"行情切换: {current_regime} → {new_regime}")
                            if current_strategy:
                                _name, _strat = current_strategy
                                # 切换至 trending_up 时保留限价卖单（价格上行中自然会成交）
                                keep_sells = (current_regime == "ranging" and new_regime == "trending_up")
                                _pnl = _strat.stop(keep_sells=keep_sells)
                                if _pnl:
                                    tracker.record(_pnl, _name)
                                    logger.info(f"盈亏: {_pnl:+.4f} | 累计: {tracker.realized_pnl:+.4f} USDT")
                                current_strategy = None

                            last_switch_time = now
                            current_regime = new_regime

                            _grid_allowed, _grid_mult = _strat_gate(ccy, "grid")
                            _trend_allowed, _trend_mult = _strat_gate(ccy, "trend")

                            if new_regime == "ranging" and not _grid_allowed:
                                logger.info(f"⚠️ {ccy} 网格策略受限（StrategyGuard），空仓等待")
                            elif new_regime in ("trending_up", "trending_down") and not _trend_allowed:
                                logger.info(f"⚠️ {ccy} 趋势策略受限（StrategyGuard），空仓等待")
                            elif new_regime == "ranging":
                                # 按币种读取网格参数（回测调优）
                                coin_base = ccy.split("-")[0]
                                grid_count = getattr(config, f"{coin_base}_SPOT_GRID_COUNT", config.GRID_COUNT)
                                grid_range = getattr(config, f"{coin_base}_SPOT_GRID_RANGE_PCT", config.GRID_RANGE_PCT)

                                # 宏观风险调整：高风险→网格更宽档更少，低风险→正常
                                m_adj = get_macro_adjustment(GLOBAL_MACRO)
                                grid_range *= m_adj["grid_width_mult"]
                                grid_count = max(2, int(grid_count * m_adj["max_positions_mult"]))
                                cap_mult = m_adj["position_multiplier"] * _grid_mult
                                if m_adj["macro_risk"] > 0.65:
                                    logger.info(
                                        f"⚠️ 宏观风险 {m_adj['macro_risk']:.0%}，"
                                        f"网格加宽×{m_adj['grid_width_mult']:.1f}，"
                                        f"仓位×{cap_mult:.0%}，"
                                        f"警告: {m_adj.get('warnings', [])}"
                                    )

                                lower, upper = get_range_bounds(df, grid_range)
                                try:
                                    strat = GridStrategy(
                                        client=client,
                                        lower=lower,
                                        upper=upper,
                                        grid_count=grid_count,
                                        capital=initial_capital * max(0.5, 0.9 * cap_mult),
                                        size_decimals=size_dec,
                                        symbol=ccy,
                                    )
                                    strat.start(price)
                                    current_strategy = ("grid", strat)
                                    logger.info(f"启动网格: {lower:.6f} ~ {upper:.6f}（仓位×{_grid_mult:.0%}）")
                                except ValueError as e:
                                    logger.warning(f"网格初始化失败: {e}")

                            elif new_regime in ("trending_up", "trending_down"):
                                strat = TrendStrategy(
                                    client=client,
                                    capital=initial_capital * _trend_mult,
                                    size_decimals=size_dec,
                                    ccy=ccy,
                                )
                                strat.start(new_regime, price, indicators=indicators)
                                current_strategy = ("trend", strat)
                                logger.info(f"启动趋势策略: {new_regime}（仓位×{_trend_mult:.0%}）")

                            else:
                                logger.info("行情不明朗，空仓等待")

                    elif new_regime in ("trending_up", "trending_down") and current_strategy:
                        name, strat = current_strategy
                        if name == "trend" and strat.running and not strat._can_reenter:
                            # ── 趋势止损后重新入场 ──
                            now_ts = time.time()
                            # 1) 冷却检查：止损后至少等 TREND_REENTRY_COOLDOWN 秒
                            if now_ts - last_trend_loss_time < TREND_REENTRY_COOLDOWN:
                                remaining = int(TREND_REENTRY_COOLDOWN - (now_ts - last_trend_loss_time))
                                logger.info(f"趋势冷却中（还剩 {remaining}s），跳过重新入场")
                                continue
                            # 2) 连续亏损检查：连亏 N 次 → 禁止趋势，切到网格
                            if consecutive_trend_losses >= MAX_CONSECUTIVE_TREND_LOSSES:
                                logger.warning(
                                    f"趋势连续止损 {consecutive_trend_losses} 次，禁止趋势策略，"
                                    f"切到网格（冷却 {SWITCH_COOLDOWN}s）"
                                )
                                # 切到网格（与 ranging 主分支一致地构造）
                                strat.running = False
                                _fb_allowed, _fb_mult = _strat_gate(ccy, "grid")
                                if not _fb_allowed:
                                    logger.info(f"⚠️ {ccy} 网格策略受限，连亏后空仓冷却")
                                    current_strategy = None
                                    consecutive_trend_losses = 0
                                    last_switch_time = now_ts
                                    continue
                                coin_base = ccy.split("-")[0]
                                grid_count = getattr(config, f"{coin_base}_SPOT_GRID_COUNT", config.GRID_COUNT)
                                grid_range = getattr(config, f"{coin_base}_SPOT_GRID_RANGE_PCT", config.GRID_RANGE_PCT)
                                m_adj = get_macro_adjustment(GLOBAL_MACRO)
                                grid_range *= m_adj["grid_width_mult"]
                                grid_count = max(2, int(grid_count * m_adj["max_positions_mult"]))
                                cap_mult = m_adj["position_multiplier"] * _fb_mult
                                lower, upper = get_range_bounds(df, grid_range)
                                try:
                                    grid_strat = GridStrategy(
                                        client=client, lower=lower, upper=upper,
                                        grid_count=grid_count,
                                        capital=initial_capital * max(0.5, 0.9 * cap_mult),
                                        size_decimals=size_dec, symbol=ccy,
                                    )
                                    grid_strat.start(price)
                                    current_strategy = ("grid", grid_strat)
                                except ValueError as e:
                                    logger.warning(f"网格初始化失败: {e}")
                                    current_strategy = None
                                consecutive_trend_losses = 0
                                last_switch_time = now_ts
                                continue
                            # 3) V 底检测：止损后价格反向 >3% → 行情可能已反转，强制重检
                            entry_price = getattr(strat, 'entry_price', None)
                            if entry_price and price > 0:
                                if (new_regime == "trending_down" and price > entry_price * (1 + V_BOTTOM_CHECK_PRICE_PCT)) or \
                                   (new_regime == "trending_up" and price < entry_price * (1 - V_BOTTOM_CHECK_PRICE_PCT)):
                                    logger.warning(
                                        f"⚠️ 疑似 V 底/顶：开仓价 {entry_price:.4f} 现价 {price:.4f}，"
                                        f"反向 {abs(price/entry_price-1)*100:.1f}%，强制重新识别行情"
                                    )
                                    last_regime_check = 0  # 下一轮强制重检行情
                                    continue
                            # 4) 通过所有检查 → 重新入场
                            logger.info(f"止损后行情仍为 {new_regime}，重新评估入场")
                            strat.start(new_regime, price, indicators=indicators)
                            consecutive_trend_losses += 1  # 假设这次是止损（会在 on_tick 中重置）
                            last_trend_loss_time = now_ts

            except Exception as e:
                logger.error(f"行情检查异常: {e}")

            last_regime_check = time.time()

        # ── 执行策略 ──────────────────────────────────────────
        try:
            ticker = client.get_ticker()
            price  = float(ticker["last"])

            if is_trx and trx_strat.running:
                pnl = trx_strat.on_tick(price, indicators=indicators)
                if pnl != 0:
                    tracker.record(pnl, "trx_adaptive")
                    logger.info(f"盈亏: {pnl:+.4f} | 累计: {tracker.realized_pnl:+.4f} USDT")
            elif not is_trx and current_strategy:
                name, strat = current_strategy
                pnl = strat.on_tick() if name == "grid" else strat.on_tick(price, indicators=indicators)
                if pnl != 0:
                    tracker.record(pnl, name)
                    logger.info(f"盈亏: {pnl:+.4f} | 累计: {tracker.realized_pnl:+.4f} USDT")
                    # 趋势盈利 → 重置连续亏损计数器
                    if pnl > 0 and name in ("trend",):
                        if consecutive_trend_losses > 0:
                            logger.info(f"趋势盈利 {pnl:+.4f}，重置连续亏损计数（之前 {consecutive_trend_losses} 次）")
                        consecutive_trend_losses = 0

            # ── 每 tick 轻量风控（含持仓浮亏，避免对在途亏损失明）──
            floating = _floating_pnl(ccy, client, is_futures=False, price=price)
            dd = tracker.effective_drawdown(floating)
            if dd >= config.MAX_DRAWDOWN_PCT:
                logger.warning(f"{ccy} 实时回撤 {dd:.2%}（含浮亏 {floating:+.2f}）超阈值，停止当前策略并冷却")
                if is_trx:
                    pnl = trx_strat.stop()
                    if pnl != 0:
                        tracker.record(pnl, "trx_adaptive")
                        logger.info(f"盈亏: {pnl:+.4f} | 累计: {tracker.realized_pnl:+.4f} USDT")
                elif current_strategy:
                    _name, _strat = current_strategy
                    _pnl = _strat.stop()
                    if _pnl:
                        tracker.record(_pnl, _name)
                        logger.info(f"盈亏: {_pnl:+.4f} | 累计: {tracker.realized_pnl:+.4f} USDT")
                # ── 暂停等待而不是永久退出 ──
                current_strategy = None
                current_regime = None
                consecutive_trend_losses = 0
                tracker.reset_drawdown_reference()
                logger.info(f"⏸️ {ccy} 进入冷却期（{SWITCH_COOLDOWN}s），等待回撤恢复后再重新评估行情")
                # 重置 tracker 回撤基准（冷却后从当前权益重新开始）
                tracker.reset_drawdown_reference()
                # 等待冷却
                wait_start = time.time()
                while time.time() - wait_start < SWITCH_COOLDOWN and not stop_event.is_set():
                    stop_event.wait(30)
                if stop_event.is_set():
                    break
                logger.info(f"⏯️ {ccy} 冷却结束，重新开始行情评估")
                last_regime_check = 0  # 强制下一轮重新识别行情
                continue  # 跳回 while 循环顶部

        except Exception as e:
            logger.error(f"策略执行异常: {e}")

        stop_event.wait(CHECK_INTERVAL)

    logger.info(f"=== {ccy} 策略线程退出 ===")


def run_swap_coin(ccy: str, stop_event: threading.Event):
    """合约（永续）策略线程：支持做多/做空趋势策略"""
    coin_cfg        = config.COIN_CONFIG[ccy]
    symbol          = coin_cfg["symbol"]
    initial_capital = coin_cfg["initial_capital"]
    size_dec        = coin_cfg["size_decimals"]

    log_path = Path(__file__).parent / f"bot_{ccy}.log"
    # trx_adaptive_futures 是 TRX_SWAP 独有 logger；futures_trend 是共享的不路由
    extra_loggers = ["trx_adaptive_futures", "base_adaptive"] if ccy == "TRX_SWAP" else []
    logger   = _make_file_logger(f"bot.{ccy}", log_path, also_route=extra_loggers)
    logger.info(f"=== {ccy} 合约策略线程启动 ===")

    client   = OKXClient(symbol=symbol, flag=coin_cfg.get("flag"), market_flag=coin_cfg.get("market_flag"))
    tracker  = Tracker(initial_capital, ccy=ccy)
    should_stop = False
    consecutive_trend_losses = 0
    last_trend_loss_time     = 0

    # 合约：关闭残留持仓（退避重试，OKX故障时不退出线程）
    cleanup_ok = False
    for _attempt in range(5):
        try:
            pos = client.get_futures_position()
            if pos and float(pos.get("pos", 0)) != 0:
                logger.warning(f"[启动清理] 检测到 {ccy} 残留合约持仓，执行平仓（第{_attempt+1}次）")
                client.close_futures_position()
                logger.info(f"[启动清理] {ccy} 合约持仓已平")
            client.cancel_all_orders()
            client.cancel_algo_orders()
            cleanup_ok = True
            break
        except Exception as e:
            logger.error(f"[启动清理] {ccy} 合约清理失败（第{_attempt+1}次）: {e}")
            time.sleep(10 * (_attempt + 1))  # 10s, 20s, 30s, 40s 退避
    
    if not cleanup_ok:
        logger.warning(f"[启动清理] {ccy} 清理未完成，将在首次 tick 时重试（不退出线程）")

    # TRX_SWAP 使用专属合约自适应策略
    is_trx_swap  = (ccy == "TRX_SWAP")
    trx_swap_strat = TRXAdaptiveFuturesStrategy(client, initial_capital) if is_trx_swap else None

    current_strategy  = None
    current_regime    = None
    last_regime_check = 0
    last_switch_time  = 0
    indicators        = {}
    _check_count      = 0  # 跳过首次风控检查（余额未同步）

    while not stop_event.is_set():
        now = time.time()

        # ── 全局熔断检查 ──────────────────────────────────
        guard_result = _guard.check()
        coin_result = _guard.coin_check(ccy)
        if guard_result["status"] == "halt":
            logger.warning(f"⚠️ 全局熔断: {guard_result['reason']} — 跳过开仓")
            stop_event.wait(CHECK_INTERVAL)
            continue
        if not coin_result["allowed"]:
            logger.info(f"⚠️ {ccy} 冷却: {coin_result['reason']} — 跳过")
            stop_event.wait(CHECK_INTERVAL)
            continue
        if guard_result["status"] == "protect":
            logger.info(f"🛡️ 保护模式: {guard_result['reason']}")

        # ── 策略级熔断（合约） ──────────────────────────
        all_blocked = False
        for sname in StrategyGuard._get_coin_strategies(ccy):
            sr = _sguard.check(ccy, sname)
            if sr["mode"] != "normal" and not sr["allowed"]:
                logger.info(f"  🚫 策略熔断 {ccy}.{sname}: {sr['reason']}")
            else:
                all_blocked = False
                break
        else:
            if StrategyGuard._get_coin_strategies(ccy):
                all_blocked = True
        if all_blocked:
            logger.warning(f"⚠️ {ccy} 所有策略已熔断 — 跳过")
            stop_event.wait(CHECK_INTERVAL)
            continue

        if now - last_regime_check > REGIME_INTERVAL:
            try:
                raw        = client.get_candles()
                df         = parse_candles(raw)
                new_regime, indicators = detect_regime(df, symbol=ccy)
                ticker     = client.get_ticker()
                price      = float(ticker["last"])
                balance    = client.get_balance("USDT")

                logger.info(
                    f"行情: {new_regime} | 价格: {price} | "
                    f"RSI={indicators.get('rsi', 0):.1f} | 余额: {balance:.2f} USDT"
                )

                if indicators and new_regime in ("trending_up", "trending_down"):
                    ai_hint = get_ai_regime_hint(indicators, new_regime, symbol=symbol)
                    if (ai_hint
                            and ai_hint["regime"] != new_regime
                            and ai_hint["confidence"] >= config.AI_CONFIDENCE_THRESHOLD):
                        logger.info(
                            f"AI 覆盖: {new_regime} → {ai_hint['regime']} "
                            f"(置信度={ai_hint['confidence']:.0%})"
                        )
                        new_regime = ai_hint["regime"]

                logger.info(tracker.summary())

                # 同步策略贡献报表 + 账户守护者（合约）
                for rec in tracker.records:
                    key = f"swap_{ccy}:{rec['strategy']}:{rec.get('time','')}"
                    if not hasattr(run_swap_coin, f"_rep_{ccy}"):
                        setattr(run_swap_coin, f"_rep_{ccy}", set())
                    reported = getattr(run_swap_coin, f"_rep_{ccy}")
                    if key not in reported:
                        _reporter.record(ccy, rec["strategy"], rec["pnl"], rec.get("fee", 0))
                        _guard.add_trade(ccy, rec["pnl"])
                        reported.add(key)

                dd = tracker.drawdown()
                if dd >= config.MAX_DRAWDOWN_PCT:
                    logger.warning(f"{ccy} 回撤 {dd:.2%} 超阈值，暂停交易 {SWITCH_COOLDOWN}s")
                    if is_trx_swap:
                        pnl = trx_swap_strat.stop()
                        if pnl != 0:
                            tracker.record(pnl, "trx_adaptive_futures")
                    elif current_strategy:
                        _name, _strat = current_strategy
                        _pnl = _strat.stop()
                        if _pnl:
                            tracker.record(_pnl, _name)
                    current_strategy = None
                    current_regime = None
                    consecutive_trend_losses = 0
                    tracker.reset_drawdown_reference()
                    stop_event.wait(SWITCH_COOLDOWN)
                    if stop_event.is_set():
                        break
                    last_regime_check = 0
                    continue

                if is_trx_swap:
                    if new_regime != current_regime:
                        if now - last_switch_time < SWITCH_COOLDOWN:
                            pass  # 冷却期内不切换
                        else:
                            logger.info(f"TRX_SWAP 行情切换: {current_regime} → {new_regime}")
                            if trx_swap_strat.running:
                                pnl = trx_swap_strat.stop()
                                if pnl != 0:
                                    tracker.record(pnl, "trx_adaptive_futures")
                                    logger.info(f"盈亏: {pnl:+.4f} | 累计: {tracker.realized_pnl:+.4f} USDT")
                            last_switch_time = now
                            current_regime = new_regime
                            _sg_allowed, _sg_mult = _strat_gate(ccy, "trx_adaptive_futures")
                            if new_regime in ("ranging", "trending_up", "trending_down") and _sg_allowed:
                                trx_swap_strat.start(new_regime, price,
                                                     capital=initial_capital * _sg_mult, indicators=indicators)
                                logger.info(f"TRX_SWAP 自适应策略启动: {new_regime}（仓位×{_sg_mult:.0%}）")
                            else:
                                logger.info("TRX_SWAP 行情不明朗或策略受限，空仓等待")
                    elif new_regime in ("ranging", "trending_up", "trending_down") and not trx_swap_strat.running:
                        _sg_allowed, _sg_mult = _strat_gate(ccy, "trx_adaptive_futures")
                        if _sg_allowed:
                            logger.info(f"TRX_SWAP 策略未运行，重新启动: {new_regime}")
                            trx_swap_strat.start(new_regime, price,
                                                 capital=initial_capital * _sg_mult, indicators=indicators)
                else:
                    if new_regime != current_regime:
                        if now - last_switch_time < SWITCH_COOLDOWN:
                            pass  # 冷却期内不切换
                        else:
                            logger.info(f"行情切换: {current_regime} → {new_regime}")
                            if current_strategy:
                                _name, _strat = current_strategy
                                _pnl = _strat.stop()
                                if _pnl:
                                    tracker.record(_pnl, _name)
                                    logger.info(f"盈亏: {_pnl:+.4f} | 累计: {tracker.realized_pnl:+.4f} USDT")
                                current_strategy = None
                            last_switch_time = now
                            current_regime = new_regime

                            _fg_allowed, _fg_mult = _strat_gate(ccy, "futures_grid")
                            _ft_allowed, _ft_mult = _strat_gate(ccy, "futures_trend")

                            if new_regime == "ranging" and not _fg_allowed:
                                logger.info(f"⚠️ {ccy} 合约网格受限（StrategyGuard），空仓等待")
                            elif new_regime in ("trending_up", "trending_down") and not _ft_allowed:
                                logger.info(f"⚠️ {ccy} 合约趋势受限（StrategyGuard），空仓等待")
                            elif new_regime == "ranging":
                                lower, upper = 0, 0
                                try:
                                    from market_analyzer import get_range_bounds
                                    # 使用币种专属网格参数
                                    grid_count = getattr(config, f"{ccy[:-5]}_GRID_COUNT", config.GRID_COUNT)
                                    grid_range = getattr(config, f"{ccy[:-5]}_GRID_RANGE_PCT", config.GRID_RANGE_PCT)
                                    grid_pos   = getattr(config, f"{ccy[:-5]}_GRID_POSITION_PCT", 0.5)

                                    # 宏观风险调整
                                    m_adj = get_macro_adjustment(GLOBAL_MACRO)
                                    grid_range *= m_adj["grid_width_mult"]
                                    grid_count = max(2, int(grid_count * m_adj["max_positions_mult"]))
                                    cap_mult = m_adj["position_multiplier"] * _fg_mult
                                    if m_adj["macro_risk"] > 0.65:
                                        logger.info(
                                            f"⚠️ {ccy} 宏观风险 {m_adj['macro_risk']:.0%}，"
                                            f"网格加宽×{m_adj['grid_width_mult']:.1f}，"
                                            f"警告: {m_adj.get('warnings', [])}"
                                        )

                                    lower, upper = get_range_bounds(df, grid_range)
                                    strat = FuturesGridStrategy(
                                        client=client, lower=lower, upper=upper,
                                        grid_count=grid_count,
                                        capital=initial_capital * max(0.3, grid_pos * cap_mult),
                                        symbol=ccy,
                                    )
                                    strat.start(price)
                                    current_strategy = ("futures_grid", strat)
                                    logger.info(f"启动合约网格: {lower:.4f} ~ {upper:.4f}（仓位×{_fg_mult:.0%}）")
                                except ValueError as e:
                                    logger.warning(f"合约网格初始化失败: {e}")

                            elif new_regime in ("trending_up", "trending_down"):
                                # 资金费率检查：费率过高时不趋势交易
                                funding_ok = True
                                try:
                                    fr = client.get_funding_rate()
                                    rate = float(fr.get("fundingRate", 0))
                                    if new_regime == "trending_up" and rate > 0.0001:
                                        logger.warning(f"资金费率 {rate:.6%} 偏高，做多需付费，跳过趋势入场")
                                        funding_ok = False
                                    elif new_regime == "trending_down" and rate < -0.0001:
                                        logger.warning(f"资金费率 {rate:.6%} 偏低，做空需付费，跳过趋势入场")
                                        funding_ok = False
                                except Exception as e:
                                    logger.warning(f"查询资金费率失败: {e}")

                                if funding_ok:
                                    strat = FuturesTrendStrategy(client=client,
                                                                 capital=initial_capital * _ft_mult, ccy=ccy)
                                    strat.start(new_regime, price, indicators=indicators)
                                    current_strategy = ("futures_trend", strat)
                                    logger.info(f"启动合约趋势策略: {new_regime}（仓位×{_ft_mult:.0%}）")
                                else:
                                    logger.info("资金费率不利，空仓等待")
                            else:
                                logger.info("行情不明朗，空仓等待")
                    elif new_regime in ("trending_up", "trending_down") and current_strategy:
                        name, strat = current_strategy
                        if name == "futures_trend" and strat.running and not getattr(strat, '_can_reenter', True):
                            now_ts = time.time()
                            # 0) 资金费率检查
                            try:
                                fr = client.get_funding_rate()
                                rate = float(fr.get("fundingRate", 0))
                                if (new_regime == "trending_up" and rate > 0.0001) or \
                                   (new_regime == "trending_down" and rate < -0.0001):
                                    logger.warning(f"资金费率 {rate:.6%} 不利，跳过趋势重新入场")
                                    continue
                            except Exception:
                                pass
                            # 1) 冷却检查
                            if now_ts - last_trend_loss_time < TREND_REENTRY_COOLDOWN:
                                remaining = int(TREND_REENTRY_COOLDOWN - (now_ts - last_trend_loss_time))
                                logger.info(f"趋势冷却中（还剩 {remaining}s），跳过重新入场")
                                continue
                            # 2) 连续亏损检查
                            if consecutive_trend_losses >= MAX_CONSECUTIVE_TREND_LOSSES:
                                logger.warning(
                                    f"趋势连续止损 {consecutive_trend_losses} 次，禁止趋势策略，"
                                    f"切到合约网格（冷却 {SWITCH_COOLDOWN}s）"
                                )
                                strat.running = False
                                _fb_allowed, _fb_mult = _strat_gate(ccy, "futures_grid")
                                if not _fb_allowed:
                                    logger.info(f"⚠️ {ccy} 合约网格受限，连亏后空仓冷却")
                                    current_strategy = None
                                    consecutive_trend_losses = 0
                                    last_switch_time = now_ts
                                    continue
                                from market_analyzer import get_range_bounds
                                grid_count = getattr(config, f"{ccy[:-5]}_GRID_COUNT", config.GRID_COUNT)
                                grid_range = getattr(config, f"{ccy[:-5]}_GRID_RANGE_PCT", config.GRID_RANGE_PCT)
                                grid_pos   = getattr(config, f"{ccy[:-5]}_GRID_POSITION_PCT", 0.5)
                                m_adj = get_macro_adjustment(GLOBAL_MACRO)
                                grid_range *= m_adj["grid_width_mult"]
                                grid_count = max(2, int(grid_count * m_adj["max_positions_mult"]))
                                cap_mult = m_adj["position_multiplier"] * _fb_mult
                                try:
                                    lower, upper = get_range_bounds(df, grid_range)
                                    grid_strat = FuturesGridStrategy(
                                        client=client, lower=lower, upper=upper,
                                        grid_count=grid_count,
                                        capital=initial_capital * max(0.3, grid_pos * cap_mult),
                                        symbol=ccy,
                                    )
                                    grid_strat.start(price)
                                    current_strategy = ("futures_grid", grid_strat)
                                except ValueError as e:
                                    logger.warning(f"合约网格初始化失败: {e}")
                                    current_strategy = None
                                consecutive_trend_losses = 0
                                last_switch_time = now_ts
                                continue
                            # 3) V 底检测
                            entry_price = getattr(strat, 'entry_price', None)
                            if entry_price and price > 0:
                                if (new_regime == "trending_down" and price > entry_price * (1 + V_BOTTOM_CHECK_PRICE_PCT)) or \
                                   (new_regime == "trending_up" and price < entry_price * (1 - V_BOTTOM_CHECK_PRICE_PCT)):
                                    logger.warning(
                                        f"⚠️ 疑似 V 底/顶：开仓价 {entry_price:.4f} 现价 {price:.4f}，"
                                        f"反向 {abs(price/entry_price-1)*100:.1f}%，强制重新识别行情"
                                    )
                                    last_regime_check = 0
                                    continue
                            # 4) 通过所有检查 → 重新入场
                            logger.info(f"止损后行情仍为 {new_regime}，重新评估入场")
                            strat.start(new_regime, price, indicators=indicators)
                            consecutive_trend_losses += 1
                            last_trend_loss_time = now_ts

            except Exception as e:
                logger.error(f"行情检查异常: {e}")

            last_regime_check = time.time()

        try:
            ticker = client.get_ticker()
            price  = float(ticker["last"])

            if is_trx_swap and trx_swap_strat.running:
                pnl = trx_swap_strat.on_tick(price, indicators=indicators)
                if pnl != 0:
                    tracker.record(pnl, "trx_adaptive_futures")
                    logger.info(f"盈亏: {pnl:+.4f} | 累计: {tracker.realized_pnl:+.4f} USDT")
            elif not is_trx_swap and current_strategy:
                name, strat = current_strategy
                pnl = strat.on_tick(price) if name == "futures_grid" else strat.on_tick(price, indicators=indicators)
                if pnl != 0:
                    tracker.record(pnl, name)
                    logger.info(f"盈亏: {pnl:+.4f} | 累计: {tracker.realized_pnl:+.4f} USDT")
                    # 趋势盈利 → 重置连续亏损计数器
                    if pnl > 0 and name in ("futures_trend",):
                        if consecutive_trend_losses > 0:
                            logger.info(f"趋势盈利 {pnl:+.4f}，重置连续亏损计数（之前 {consecutive_trend_losses} 次）")
                        consecutive_trend_losses = 0
                # grid 浮亏止损自停后清空策略，等待下次行情切换重建
                if name == "futures_grid" and not strat.running:
                    current_strategy = None

            # ── 每 tick 轻量风控（前两次跳过等余额同步；含合约持仓浮亏 upl）──
            _check_count += 1
            floating = _floating_pnl(ccy, client, is_futures=True, price=price)
            dd = tracker.effective_drawdown(floating)
            if _check_count > 1 and dd >= config.MAX_DRAWDOWN_PCT:
                logger.warning(f"{ccy} 实时回撤 {dd:.2%}（含浮亏 {floating:+.2f}）超阈值，停止当前策略并冷却")
                if is_trx_swap:
                    pnl = trx_swap_strat.stop()
                    if pnl != 0:
                        tracker.record(pnl, "trx_adaptive_futures")
                elif current_strategy:
                    _name, _strat = current_strategy
                    _pnl = _strat.stop()
                    if _pnl:
                        tracker.record(_pnl, _name)
                # ── 暂停等待而不是永久退出 ──
                current_strategy = None
                current_regime = None
                consecutive_trend_losses = 0
                tracker.reset_drawdown_reference()
                logger.info(f"⏸️ {ccy} 进入冷却期（{SWITCH_COOLDOWN}s），等待回撤恢复后再重新评估行情")
                wait_start = time.time()
                while time.time() - wait_start < SWITCH_COOLDOWN and not stop_event.is_set():
                    stop_event.wait(30)
                if stop_event.is_set():
                    break
                logger.info(f"⏯️ {ccy} 冷却结束，重新开始行情评估")
                last_regime_check = 0
                continue

        except Exception as e:
            logger.error(f"策略执行异常: {e}")

        if should_stop:
            break

        stop_event.wait(CHECK_INTERVAL)

    logger.info(f"=== {ccy} 合约策略线程退出 ===")


def _acquire_singleton_lock(lock_file):
    """跨平台单实例文件锁。返回 True 表示加锁成功。"""
    try:
        if sys.platform == "win32":
            import msvcrt
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (IOError, OSError):
        return False


def main():
    # 进程锁：防止同一时刻运行多个 bot 实例
    lock_path = Path(__file__).parent / ".bot.lock"
    _lock_file = open(lock_path, "a+")
    if not _acquire_singleton_lock(_lock_file):
        # 尝试读取已有 PID
        existing_pid = ""
        try:
            if lock_path.exists():
                with open(lock_path) as f:
                    existing_pid = f.read().strip()
        except:
            pass
        pid_info = f" existing_pid={existing_pid}" if existing_pid else ""
        # 限频：同类消息 10 分钟最多一次
        rate_key = os.path.join(os.path.dirname(lock_path), ".lock_alert_rate")
        now = time.time()
        last_time = 0
        try:
            if os.path.exists(rate_key):
                with open(rate_key) as f:
                    last_time = float(f.read().strip())
        except:
            pass
        if now - last_time > 600:
            print(f"[INFO] Duplicate start blocked{pid_info} (suppressed for 10min)")
            with open(rate_key, "w") as f:
                f.write(str(now))
        sys.exit(1)
    try:
        _lock_file.seek(0)
        _lock_file.truncate()
    except OSError:
        pass
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger = logging.getLogger("main")
    logger.info(f"=== 量化机器人启动，交易对: {config.COINS} ===")

    stop_event = threading.Event()
    threads = []

    for ccy in config.COINS:
        mode   = config.COIN_CONFIG[ccy].get("mode", "spot")
        target = run_swap_coin if mode == "futures" else run_coin
        t = threading.Thread(
            target=target,
            args=(ccy, stop_event),
            name=f"bot-{ccy}",
            daemon=True,
        )
        threads.append(t)
        t.start()
        logger.info(f"已启动 {ccy} 线程（模式={mode}）")
        time.sleep(2)  # 错开各线程的首次行情请求

    try:
        last_macro_update = 0
        last_hb_check = 0
        heartbeat_path = Path(__file__).parent / ".bot_heartbeat"
        _alert_state = {"last_stale": 0, "last_stop": 0, "was_alerted": False}
        while True:
            now = time.time()
            # 心跳：每轮 tick 更新
            heartbeat_path.touch()

            # ── 心跳告警（每30秒检查一次，防抖：同类10分钟一次） ──
            if now - last_hb_check > 30:
                last_hb_check = now
                # 读取心跳文件年龄（主循环每次 tick 都更新，所以正常 < 2s）
                hb_age = now - heartbeat_path.stat().st_mtime if heartbeat_path.exists() else 9999
                if hb_age > 180:
                    if not _alert_state["was_alerted"] or \
                       (now - _alert_state["last_stale"] > 600):
                        if hb_age < 300:
                            send_tg(f"🟡 Bot 疑似卡顿，心跳 {int(hb_age)}s 未更新")
                            _alert_state["last_stale"] = now
                        else:
                            send_tg(f"🚨 Bot 可能已停止，心跳 {int(hb_age)}s 未更新，请检查")
                            _alert_state["last_stop"] = now
                        _alert_state["was_alerted"] = True
                elif _alert_state["was_alerted"]:
                    send_tg("✅ Bot 已恢复正常")
                    _alert_state = {"last_stale": 0, "last_stop": 0, "was_alerted": False}

            if now - last_macro_update > MACRO_CHECK_INTERVAL:
                _update_macro()
                last_macro_update = now
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到停止信号，关闭所有策略...")
        stop_event.set()
        for t in threads:
            t.join(timeout=15)
        logger.info("所有策略已停止")


if __name__ == "__main__":
    main()
