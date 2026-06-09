"""
TRX / ETH / SOL 量化交易机器人
每个币种在独立线程中运行，日志写入 bot_{CCY}.log
"""
import time
import logging
import threading
import sys
import os
from datetime import datetime, timezone
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
import volatility_adapter

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

# 全局防护（本金口径统一用 config.TOTAL_CAPITAL，支持 .env 的 TOTAL_CAPITAL 覆盖）
# 必须与 web 面板、一致性检查同一口径，否则 .env 设的真实入金对实盘熔断不生效
_guard = Guard(total_capital=config.TOTAL_CAPITAL)
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


def _apply_ml_regime(rule_regime, df, ccy, logger):
    """ML 行情分类器覆盖层：高置信时用 ML 结果，否则回退规则。
    缺模型/未装 sklearn/低置信/任何异常 → 原样返回规则结果，绝不中断实盘。"""
    if not getattr(config, "USE_ML_REGIME", False):
        return rule_regime
    try:
        import ml_regime
        pred = ml_regime.predict(df, ccy)
    except Exception:
        return rule_regime
    if not pred:
        return rule_regime
    ml_reg, conf = pred
    if conf < getattr(config, "ML_REGIME_CONFIDENCE", 0.70):
        return rule_regime
    if ml_reg != rule_regime:
        logger.info(f"🤖 ML 覆盖行情: {rule_regime} → {ml_reg} (置信度={conf:.0%})")
        return ml_reg
    return rule_regime


def _guard_mult(guard_result: dict) -> tuple:
    """将 account_guard 的 warn/protect/halt 状态转为仓位系数和开仓限制。
    返回 (capital_mult, can_open_new, extra_restrictions)
    单调性要求：越严重 → can_open 只能更严、cap_mult 只能更小。
    - normal:    (1.0,  True,  [])
    - warn:      (0.65, False, ["no_new_positions"])              日亏2%   暂停新开
    - protect:   (0.40, False, ["no_new_positions","reduce_position","widen_grid"])  日亏3.5% 更严，只管理存量
    - halt:      (0.0,  False, ["close_only"])                    日亏5%   只许平仓（主线已跳过）
    """
    mode = guard_result.get("mode", "normal")
    if mode == "halt":
        return (0.0, False, ["close_only"])
    if mode == "protect":
        return (0.40, False, ["no_new_positions", "reduce_position", "widen_grid"])
    if mode == "warn":
        return (0.65, False, ["no_new_positions"])
    return (1.0, True, [])


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

    # 现货：残留持仓不市价砸（交给策略管理，不再挂限价卖单）
    # 原因是挂限价卖单会与策略网格冲突，导致资金被锁在"幽灵订单"中无法使用
    base = _SPOT_BASE_CCY.get(ccy)
    if base:
        try:
            holding = client.get_spot_position(base)
            ticker  = client.get_ticker()
            price   = float(ticker["last"])
            min_sz  = config.COIN_CONFIG[ccy].get("min_order_size", 1)
            if holding >= min_sz:
                logger.info(
                    f"[启动清理] 检测到残留 {base} 持仓 {holding} "
                    f"（当前价 {price:.4f}），交给策略管理，不自动卖出"
                )
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
        _tick_start = now     # tick 耗时监控起点
        _ai_safety_mult = 1.0  # AI 安全系数：每 tick 重置，只能 ≤1.0

        # ── 全局熔断检查 ──────────────────────────────────
        guard_result = _guard.check()
        coin_result = _guard.coin_check(ccy)
        _guard_cap_mult, _guard_can_open, _guard_extra = _guard_mult(guard_result)
        guard_mode = guard_result.get("mode", "normal")

        if guard_result["status"] == "halt":
            logger.warning(f"⚠️ 全局熔断: {guard_result['reason']} — 跳过开仓")
            stop_event.wait(CHECK_INTERVAL)
            continue
        if not coin_result["allowed"]:
            logger.info(f"⚠️ {ccy} 冷却: {coin_result['reason']} — 跳过")
            stop_event.wait(CHECK_INTERVAL)
            continue
        if guard_mode == "protect":
            logger.info(f"🛡️ 保护模式: {guard_result['reason']} — 仓位×{_guard_cap_mult:.0%}")
        elif guard_mode == "warn":
            logger.info(f"⚠️ 预警模式: {guard_result['reason']} — 禁止新开仓")

        # ── 策略级熔断 ──────────────────────────────────
        # 检查本币所有策略，全封则跳过（活一个就继续）
        all_blocked = False
        for sname in StrategyGuard._get_coin_strategies(ccy):
            sr = _sguard.check(ccy, sname)
            if sr["mode"] != "normal" and not sr["allowed"]:
                logger.info(f"  🚫 策略熔断 {ccy}.{sname}: {sr['reason']}")
            else:
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
                new_regime = _apply_ml_regime(new_regime, df, ccy, logger)
                ticker  = client.get_ticker()
                price   = float(ticker["last"])
                balance = client.get_balance("USDT")

                logger.info(
                    f"行情: {new_regime} | 价格: {price} | "
                    f"RSI={indicators.get('rsi', 0):.1f} | 余额: {balance:.2f} USDT"
                )

                # AI 二次验证（只在trending信号时调用，节省75% API调用）
                ai_hint = None
                _ai_safety_mult = 1.0  # AI 安全系数：只能 ≤1.0（降风险）
                if indicators and new_regime in ("trending_up", "trending_down"):
                    ai_hint = get_ai_regime_hint(indicators, new_regime, symbol=symbol)
                    if ai_hint:
                        confidence = ai_hint.get("confidence", 0)
                        regime = ai_hint.get("regime", "")
                        # 低置信度 → 自动缩仓
                        if confidence < 0.60:
                            _ai_safety_mult = 0.50
                            logger.info(f"🛡️ AI 置信度 {confidence:.0%} < 60%，仓位减半")
                        elif confidence < 0.75:
                            _ai_safety_mult = 0.75
                            logger.info(f"🛡️ AI 置信度 {confidence:.0%} < 75%，仓位×0.75")
                        # 行情覆盖（规则引擎 trending → AI ranging = 降级）
                        if regime != new_regime and confidence >= config.AI_CONFIDENCE_THRESHOLD:
                            logger.info(
                                f"AI 覆盖: {new_regime} → {regime} "
                                f"(置信度={confidence:.0%})"
                            )
                            new_regime = regime
                elif indicators and new_regime in ("ranging",):
                    # ranging 时 AI 可以降低仓位（但不改变 regime）
                    try:
                        ai_hint = get_ai_regime_hint(indicators, new_regime, symbol=symbol)
                        if ai_hint and ai_hint.get("confidence", 0) < 0.50:
                            _ai_safety_mult = 0.65
                            logger.info(f"🛡️ AI 对 {new_regime} 低置信度，仓位×0.65")
                    except:
                        pass

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
                                if not _guard_can_open:
                                    logger.info(f"🛡️ {ccy} 预警模式禁止新开仓，空仓等待")
                                else:
                                    trx_strat.start(new_regime, price,
                                                    capital=initial_capital * _sg_mult * _guard_cap_mult * _ai_safety_mult,
                                                    indicators=indicators)
                                    logger.info(f"TRX 自适应策略启动: {new_regime}（仓位×{_sg_mult * _guard_cap_mult:.0%}）")
                            else:
                                logger.info("TRX 行情不明朗或策略受限，空仓等待")
                    elif new_regime in ("ranging", "trending_up", "trending_down") and not trx_strat.running:
                        # 行情未变但策略意外停止（如网格全成交后 IDLE），重新启动
                        _sg_allowed, _sg_mult = _strat_gate(ccy, "trx_adaptive")
                        if _sg_allowed and _guard_can_open:
                            logger.info(f"TRX 策略未运行，重新启动: {new_regime}")
                            trx_strat.start(new_regime, price,
                                            capital=initial_capital * _sg_mult * _guard_cap_mult * _ai_safety_mult,
                                            indicators=indicators)

                    # 网格停滞检测：运行中但长时间无成交 → 强制重组
                    STAGNATION_HOURS = 6  # 超过6h无成交视为停滞
                    if trx_strat and trx_strat.running and tracker.records:
                        try:
                            last_time = tracker.records[-1].get("time", "")
                            if last_time:
                                from datetime import datetime as dt
                                last_ts = dt.strptime(last_time, "%Y-%m-%d %H:%M:%S").timestamp()
                                stale_h = (now - last_ts) / 3600
                                if stale_h > STAGNATION_HOURS:
                                    logger.warning(f"🔧 {ccy} 网格停滞 {stale_h:.1f}h，强制重组")
                                    pnl = trx_strat.stop()
                                    if pnl != 0:
                                        tracker.record(pnl, "trx_adaptive")
                                    _sg_allowed, _sg_mult = _strat_gate(ccy, "trx_adaptive")
                                    if _sg_allowed and _guard_can_open:
                                        trx_strat.start(new_regime, price,
                                                        capital=initial_capital * _sg_mult * _guard_cap_mult * _ai_safety_mult,
                                                        indicators=indicators)
                                        # 记录重组时间戳，防止陷入无限重启循环
                                        tracker.record(0, "trx_adaptive", note="grid_restart")
                                        logger.info(f"🔧 {ccy} 网格重组完成")
                        except Exception:
                            pass
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
                            elif not _guard_can_open:
                                logger.info(f"🛡️ {ccy} 预警模式禁止新开仓，空仓等待")
                            elif new_regime == "ranging":
                                # 按币种读取网格参数（回测调优）
                                coin_base = ccy.split("-")[0]
                                grid_count = getattr(config, f"{coin_base}_SPOT_GRID_COUNT", config.GRID_COUNT)
                                grid_range = getattr(config, f"{coin_base}_SPOT_GRID_RANGE_PCT", config.GRID_RANGE_PCT)

                                # 波动率自适应：ATR 变化时动态调网格
                                grid_range, grid_count, _ = volatility_adapter.get_adapted_grid_params(
                                    coin_base, grid_range, grid_count, indicators)

                                # 宏观风险调整：高风险→网格更宽档更少，低风险→正常
                                m_adj = get_macro_adjustment(GLOBAL_MACRO)
                                # 保护模式加宽网格
                                gwide = m_adj["grid_width_mult"] * (1.3 if "widen_grid" in _guard_extra else 1.0)
                                grid_range *= gwide
                                grid_count = max(2, int(grid_count * m_adj["max_positions_mult"]))
                                cap_mult = m_adj["position_multiplier"] * _grid_mult * _guard_cap_mult
                                if m_adj["macro_risk"] > 0.65:
                                    logger.info(
                                        f"⚠️ 宏观风险 {m_adj['macro_risk']:.0%}，"
                                        f"网格加宽×{gwide:.1f}，"
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
                                        capital=initial_capital * max(0.5, 0.9 * cap_mult) * _ai_safety_mult,
                                        size_decimals=size_dec,
                                        symbol=ccy,
                                    )
                                    strat.start(price)
                                    current_strategy = ("grid", strat)
                                    total_mult = _grid_mult * _guard_cap_mult
                                    logger.info(f"启动网格: {lower:.6f} ~ {upper:.6f}（仓位×{total_mult:.0%})")
                                except ValueError as e:
                                    logger.warning(f"网格初始化失败: {e}")

                            elif new_regime in ("trending_up", "trending_down"):
                                strat = TrendStrategy(
                                    client=client,
                                    capital=initial_capital * _trend_mult * _guard_cap_mult * _ai_safety_mult,
                                    size_decimals=size_dec,
                                    ccy=ccy,
                                )
                                strat.start(new_regime, price, indicators=indicators)
                                current_strategy = ("trend", strat)
                                total_mult = _trend_mult * _guard_cap_mult
                                logger.info(f"启动趋势策略: {new_regime}（仓位×{total_mult:.0%})")

                            else:
                                logger.info("行情不明朗，空仓等待")

                    # 通用网格停滞检测（ETH/SOL spot）：运行中但长时间无成交 → 强制重组
                    STAGNATION_HOURS = 6
                    if not is_trx and current_strategy and tracker.records:
                        try:
                            name, strat = current_strategy
                            if name == "grid" and strat.running:
                                last_time = tracker.records[-1].get("time", "")
                                if last_time:
                                    from datetime import datetime as dt
                                    last_ts = dt.strptime(last_time, "%Y-%m-%d %H:%M:%S").timestamp()
                                    stale_h = (now - last_ts) / 3600
                                    if stale_h > STAGNATION_HOURS:
                                        logger.warning(f"🔧 {ccy} 网格停滞 {stale_h:.1f}h，强制重组")
                                        _pnl = strat.stop()
                                        if _pnl:
                                            tracker.record(_pnl, name)
                                        _grid_allowed, _grid_mult = _strat_gate(ccy, "grid")
                                        if _grid_allowed and _guard_can_open:
                                            coin_base = ccy.split("-")[0]
                                            gc = getattr(config, f"{coin_base}_SPOT_GRID_COUNT", config.GRID_COUNT)
                                            gr = getattr(config, f"{coin_base}_SPOT_GRID_RANGE_PCT", config.GRID_RANGE_PCT)
                                            gr, gc, _ = volatility_adapter.get_adapted_grid_params(coin_base, gr, gc, indicators)
                                            lower, upper = get_range_bounds(df, gr)
                                            new_strat = GridStrategy(
                                                client=client, lower=lower, upper=upper,
                                                grid_count=gc,
                                                capital=initial_capital * _grid_mult * _guard_cap_mult * _ai_safety_mult,
                                                size_decimals=size_dec, symbol=ccy,
                                            )
                                            new_strat.start(price)
                                            current_strategy = ("grid", new_strat)
                                            logger.info(f"🔧 {ccy} 网格重组完成")
                                            tracker.record(0, "grid", note="grid_restart")
                        except Exception:
                            pass

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
                                if not _fb_allowed or not _guard_can_open:
                                    logger.info(f"⚠️ {ccy} 网格策略受限或预警模式，连亏后空仓冷却")
                                    current_strategy = None
                                    consecutive_trend_losses = 0
                                    last_switch_time = now_ts
                                    continue
                                coin_base = ccy.split("-")[0]
                                grid_count = getattr(config, f"{coin_base}_SPOT_GRID_COUNT", config.GRID_COUNT)
                                grid_range = getattr(config, f"{coin_base}_SPOT_GRID_RANGE_PCT", config.GRID_RANGE_PCT)
                                m_adj = get_macro_adjustment(GLOBAL_MACRO)
                                gwide = m_adj["grid_width_mult"] * (1.3 if "widen_grid" in _guard_extra else 1.0)
                                grid_range *= gwide
                                grid_count = max(2, int(grid_count * m_adj["max_positions_mult"]))
                                cap_mult = m_adj["position_multiplier"] * _fb_mult * _guard_cap_mult
                                lower, upper = get_range_bounds(df, grid_range)
                                try:
                                    grid_strat = GridStrategy(
                                        client=client, lower=lower, upper=upper,
                                        grid_count=grid_count,
                                        capital=initial_capital * max(0.5, 0.9 * cap_mult) * _ai_safety_mult,
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
                            # 4) 通过所有检查 → 重新入场（本次止损计入连续亏损）
                            consecutive_trend_losses += 1
                            last_trend_loss_time = now_ts
                            logger.info(f"止损后行情仍为 {new_regime}，重新评估入场（连续止损 {consecutive_trend_losses} 次）")
                            strat.start(new_regime, price, indicators=indicators)

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

        # ── tick 耗时监控 ────────────────────────────────
        _tick_dur = time.time() - _tick_start
        if _tick_dur > 5.0:
            logger.warning(f"⏱️ {ccy} tick 耗时 {_tick_dur:.1f}s (阈值5s) — 策略路径或API过慢")

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
        _tick_start = now     # tick 耗时监控起点
        _ai_safety_mult = 1.0  # AI 安全系数：每 tick 重置，只能 ≤1.0

        # ── 全局熔断检查 ──────────────────────────────────
        guard_result = _guard.check()
        coin_result = _guard.coin_check(ccy)
        _guard_cap_mult, _guard_can_open, _guard_extra = _guard_mult(guard_result)
        guard_mode = guard_result.get("mode", "normal")

        if guard_result["status"] == "halt":
            logger.warning(f"⚠️ 全局熔断: {guard_result['reason']} — 跳过开仓")
            stop_event.wait(CHECK_INTERVAL)
            continue
        if not coin_result["allowed"]:
            logger.info(f"⚠️ {ccy} 冷却: {coin_result['reason']} — 跳过")
            stop_event.wait(CHECK_INTERVAL)
            continue
        if guard_mode == "protect":
            logger.info(f"🛡️ 保护模式: {guard_result['reason']} — 仓位×{_guard_cap_mult:.0%}")
        elif guard_mode == "warn":
            logger.info(f"⚠️ 预警模式: {guard_result['reason']} — 禁止新开仓")

        # ── 策略级熔断（合约） ──────────────────────────
        all_blocked = False
        for sname in StrategyGuard._get_coin_strategies(ccy):
            sr = _sguard.check(ccy, sname)
            if sr["mode"] != "normal" and not sr["allowed"]:
                logger.info(f"  🚫 策略熔断 {ccy}.{sname}: {sr['reason']}")
            else:
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
                new_regime = _apply_ml_regime(new_regime, df, ccy, logger)
                ticker     = client.get_ticker()
                price      = float(ticker["last"])
                balance    = client.get_balance("USDT")

                logger.info(
                    f"行情: {new_regime} | 价格: {price} | "
                    f"RSI={indicators.get('rsi', 0):.1f} | 余额: {balance:.2f} USDT"
                )

                if indicators and new_regime in ("trending_up", "trending_down"):
                    ai_hint = get_ai_regime_hint(indicators, new_regime, symbol=symbol)
                    if ai_hint:
                        confidence = ai_hint.get("confidence", 0)
                        regime = ai_hint.get("regime", "")
                        if confidence < 0.60:
                            _ai_safety_mult = 0.50
                            logger.info(f"🛡️ AI 置信度 {confidence:.0%} < 60%，仓位减半")
                        elif confidence < 0.75:
                            _ai_safety_mult = 0.75
                            logger.info(f"🛡️ AI 置信度 {confidence:.0%} < 75%，仓位×0.75")
                        if regime != new_regime and confidence >= config.AI_CONFIDENCE_THRESHOLD:
                            logger.info(
                                f"AI 覆盖: {new_regime} → {regime} "
                                f"(置信度={confidence:.0%})"
                            )
                            new_regime = regime
                elif indicators and new_regime in ("ranging",):
                    try:
                        ai_hint = get_ai_regime_hint(indicators, new_regime, symbol=symbol)
                        if ai_hint and ai_hint.get("confidence", 0) < 0.50:
                            _ai_safety_mult = 0.65
                            logger.info(f"🛡️ AI 对 {new_regime} 低置信度，仓位×0.65")
                    except:
                        pass

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
                                if not _guard_can_open:
                                    logger.info(f"🛡️ {ccy} 预警模式禁止新开仓，空仓等待")
                                else:
                                    trx_swap_strat.start(new_regime, price,
                                                         capital=initial_capital * _sg_mult * _guard_cap_mult * _ai_safety_mult,
                                                         indicators=indicators)
                                    logger.info(f"TRX_SWAP 自适应策略启动: {new_regime}（仓位×{_sg_mult * _guard_cap_mult:.0%})")
                            else:
                                logger.info("TRX_SWAP 行情不明朗或策略受限，空仓等待")
                    elif new_regime in ("ranging", "trending_up", "trending_down") and not trx_swap_strat.running:
                        _sg_allowed, _sg_mult = _strat_gate(ccy, "trx_adaptive_futures")
                        if _sg_allowed and _guard_can_open:
                            logger.info(f"TRX_SWAP 策略未运行，重新启动: {new_regime}")
                            trx_swap_strat.start(new_regime, price,
                                                 capital=initial_capital * _sg_mult * _guard_cap_mult * _ai_safety_mult,
                                                 indicators=indicators)

                    # 网格停滞检测（合约）：运行中但长时间无成交 → 强制重组
                    STAGNATION_HOURS = 6
                    if trx_swap_strat and trx_swap_strat.running and tracker.records:
                        try:
                            last_time = tracker.records[-1].get("time", "")
                            if last_time:
                                from datetime import datetime as dt
                                last_ts = dt.strptime(last_time, "%Y-%m-%d %H:%M:%S").timestamp()
                                stale_h = (now - last_ts) / 3600
                                if stale_h > STAGNATION_HOURS:
                                    logger.warning(f"🔧 {ccy} 网格停滞 {stale_h:.1f}h，强制重组")
                                    pnl = trx_swap_strat.stop()
                                    if pnl != 0:
                                        tracker.record(pnl, "trx_adaptive_futures")
                                    _sg_allowed, _sg_mult = _strat_gate(ccy, "trx_adaptive_futures")
                                    if _sg_allowed and _guard_can_open:
                                        trx_swap_strat.start(new_regime, price,
                                                             capital=initial_capital * _sg_mult * _guard_cap_mult * _ai_safety_mult,
                                                             indicators=indicators)
                                        logger.info(f"🔧 {ccy} 网格重组完成")
                                        tracker.record(0, "trx_adaptive_futures", note="grid_restart")
                        except Exception:
                            pass
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
                            elif not _guard_can_open:
                                logger.info(f"🛡️ {ccy} 预警模式禁止新开仓，空仓等待")
                            elif new_regime == "ranging":
                                lower, upper = 0, 0
                                try:
                                    from market_analyzer import get_range_bounds
                                    # 使用币种专属网格参数
                                    grid_count = getattr(config, f"{ccy[:-5]}_GRID_COUNT", config.GRID_COUNT)
                                    grid_range = getattr(config, f"{ccy[:-5]}_GRID_RANGE_PCT", config.GRID_RANGE_PCT)
                                    grid_pos   = getattr(config, f"{ccy[:-5]}_GRID_POSITION_PCT", 0.5)

                                    # 波动率自适应
                                    grid_range, grid_count, _ = volatility_adapter.get_adapted_grid_params(
                                        ccy[:-5], grid_range, grid_count, indicators)

                                    # 宏观风险调整
                                    m_adj = get_macro_adjustment(GLOBAL_MACRO)
                                    gwide = m_adj["grid_width_mult"] * (1.3 if "widen_grid" in _guard_extra else 1.0)
                                    grid_range *= gwide
                                    grid_count = max(2, int(grid_count * m_adj["max_positions_mult"]))
                                    cap_mult = m_adj["position_multiplier"] * _fg_mult * _guard_cap_mult
                                    if m_adj["macro_risk"] > 0.65:
                                        logger.info(
                                            f"⚠️ {ccy} 宏观风险 {m_adj['macro_risk']:.0%}，"
                                            f"网格加宽×{gwide:.1f}，"
                                            f"警告: {m_adj.get('warnings', [])}"
                                        )

                                    lower, upper = get_range_bounds(df, grid_range)
                                    strat = FuturesGridStrategy(
                                        client=client, lower=lower, upper=upper,
                                        grid_count=grid_count,
                                        capital=initial_capital * max(0.3, grid_pos * cap_mult) * _ai_safety_mult,
                                        symbol=ccy,
                                    )
                                    strat.start(price)
                                    current_strategy = ("futures_grid", strat)
                                    total_mult = _fg_mult * _guard_cap_mult
                                    logger.info(f"启动合约网格: {lower:.4f} ~ {upper:.4f}（仓位×{total_mult:.0%})")
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
                                                                 capital=initial_capital * _ft_mult * _guard_cap_mult * _ai_safety_mult,
                                                                 ccy=ccy)
                                    strat.start(new_regime, price, indicators=indicators)
                                    current_strategy = ("futures_trend", strat)
                                    total_mult = _ft_mult * _guard_cap_mult
                                    logger.info(f"启动合约趋势策略: {new_regime}（仓位×{total_mult:.0%})")
                                else:
                                    logger.info("资金费率不利，空仓等待")
                            else:
                                logger.info("行情不明朗，空仓等待")

                    # 通用期货网格停滞检测（ETH_SWAP/SOL_SWAP）：运行中但长时间无成交 → 强制重组
                    STAGNATION_HOURS = 6
                    if not is_trx_swap and current_strategy and tracker.records:
                        try:
                            name, strat = current_strategy
                            if name == "futures_grid" and strat.running:
                                last_time = tracker.records[-1].get("time", "")
                                if last_time:
                                    from datetime import datetime as dt
                                    last_ts = dt.strptime(last_time, "%Y-%m-%d %H:%M:%S").timestamp()
                                    stale_h = (now - last_ts) / 3600
                                    if stale_h > STAGNATION_HOURS:
                                        logger.warning(f"🔧 {ccy} 合约网格停滞 {stale_h:.1f}h，强制重组")
                                        _pnl = strat.stop()
                                        if _pnl:
                                            tracker.record(_pnl, name)
                                        _fb_allowed, _fb_mult = _strat_gate(ccy, "futures_grid")
                                        if _fb_allowed and _guard_can_open:
                                            coin_base = ccy.split("-")[0]
                                            gc = getattr(config, f"{coin_base}_GRID_COUNT", config.GRID_COUNT)
                                            gr = getattr(config, f"{coin_base}_GRID_RANGE_PCT", config.GRID_RANGE_PCT)
                                            gr, gc, _ = volatility_adapter.get_adapted_grid_params(coin_base, gr, gc, indicators)
                                            lower, upper = get_range_bounds(df, gr)
                                            new_strat = FuturesGridStrategy(
                                                client=client, lower=lower, upper=upper,
                                                grid_count=gc,
                                                capital=initial_capital * _fb_mult * _guard_cap_mult * _ai_safety_mult,
                                                symbol=ccy,
                                            )
                                            new_strat.start(price)
                                            current_strategy = ("futures_grid", new_strat)
                                            logger.info(f"🔧 {ccy} 合约网格重组完成")
                                            tracker.record(0, "futures_grid", note="grid_restart")
                        except Exception:
                            pass

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
                                if not _fb_allowed or not _guard_can_open:
                                    logger.info(f"⚠️ {ccy} 合约网格受限或预警模式，连亏后空仓冷却")
                                    current_strategy = None
                                    consecutive_trend_losses = 0
                                    last_switch_time = now_ts
                                    continue
                                from market_analyzer import get_range_bounds
                                grid_count = getattr(config, f"{ccy[:-5]}_GRID_COUNT", config.GRID_COUNT)
                                grid_range = getattr(config, f"{ccy[:-5]}_GRID_RANGE_PCT", config.GRID_RANGE_PCT)
                                grid_pos   = getattr(config, f"{ccy[:-5]}_GRID_POSITION_PCT", 0.5)
                                m_adj = get_macro_adjustment(GLOBAL_MACRO)
                                gwide = m_adj["grid_width_mult"] * (1.3 if "widen_grid" in _guard_extra else 1.0)
                                grid_range *= gwide
                                grid_count = max(2, int(grid_count * m_adj["max_positions_mult"]))
                                cap_mult = m_adj["position_multiplier"] * _fb_mult * _guard_cap_mult
                                try:
                                    lower, upper = get_range_bounds(df, grid_range)
                                    grid_strat = FuturesGridStrategy(
                                        client=client, lower=lower, upper=upper,
                                        grid_count=grid_count,
                                        capital=initial_capital * max(0.3, grid_pos * cap_mult) * _ai_safety_mult,
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
                            # 4) 通过所有检查 → 重新入场（本次止损计入连续亏损）
                            consecutive_trend_losses += 1
                            last_trend_loss_time = now_ts
                            logger.info(f"止损后行情仍为 {new_regime}，重新评估入场（连续止损 {consecutive_trend_losses} 次）")
                            strat.start(new_regime, price, indicators=indicators)

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

        # ── tick 耗时监控 ────────────────────────────────
        _tick_dur = time.time() - _tick_start
        if _tick_dur > 8.0:
            logger.warning(f"⏱️ {ccy} tick 耗时 {_tick_dur:.1f}s (阈值8s) — API或策略路径过慢")

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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger = logging.getLogger("main")
    logger.info(f"=== 量化机器人启动，交易对: {config.COINS} ===")

    # ── 启动前安全检查：extreme/halt/protect 状态恢复 ──────────
    _startup_blocked = False
    # 1) 极端行情锁定告警（best-effort）
    try:
        from account_guard import _safe_parse_dt
        from evolution_lock import _load_lock_log
        blocks = _load_lock_log().get("blocks", [])
        now_utc = datetime.now(timezone.utc)
        recent = [
            b for b in blocks
            if _safe_parse_dt(b.get("timestamp", "")) and
               (now_utc - _safe_parse_dt(b.get("timestamp", ""))).total_seconds() < 3600
        ]
        extreme_msgs = [b.get("reason", "") for b in recent if "extreme" in str(b.get("reason", "")).lower()]
        if extreme_msgs:
            logger.warning(f"⚠️ 启动检查：过去 1h 有 extreme_market 锁定事件 ({len(extreme_msgs)} 次)")
            logger.warning(f"   最新: {extreme_msgs[-1][:120]}")
    except Exception as e:
        logger.warning(f"极端行情启动检查跳过: {e}")

    # 2) 账户 HALT 检查（关键：halt 则只监控不交易）— 独立 try，不被上面影响
    try:
        guard_status = _guard.status_report()
        if guard_status["status"] in ("halt", "protect", "warn"):
            logger.warning(f"⚠️ 启动检查：账户守护者状态={guard_status['status']}, 日PnL={guard_status['daily_pnl_pct']:+.1f}%")
            if guard_status["status"] == "halt":
                logger.error("❌ 启动时账户处于 HALT 状态，仅恢复监控不启动策略")
                _startup_blocked = True
    except Exception as e:
        logger.warning(f"账户状态启动检查失败（不影响启动）: {e}")

    # ── TOTAL_CAPITAL 与真实账户权益一致性检查（仅实盘）────────────────
    if getattr(config, "FLAG", "1") == "1":
        logger.info("📊 模拟盘模式 — 跳过 TOTAL_CAPITAL 一致性检查")
    else:
        try:
            check_client = OKXClient(symbol="TRX-USDT-SWAP")
            usdt_balance = check_client.get_balance("USDT")
            total_in_config = sum(c["initial_capital"] for c in config.COIN_CONFIG.values())
            cfg_total = getattr(config, "TOTAL_CAPITAL", total_in_config)
            if usdt_balance > 0 and total_in_config > 0:
                gap_pct = abs(usdt_balance - cfg_total) / cfg_total
                if gap_pct > 0.20:
                    logger.warning(
                        f"⚠️ TOTAL_CAPITAL 与账户权益偏差 {gap_pct:.1%}: "
                        f"config={cfg_total:.0f} vs 账户={usdt_balance:.0f} USDT"
                    )
                elif gap_pct > 0.05:
                    logger.info(
                        f"📊 TOTAL_CAPITAL 与账户权益偏差 {gap_pct:.1%}: "
                        f"config={cfg_total:.0f} vs 账户={usdt_balance:.0f} USDT"
                    )
                else:
                    logger.info(
                        f"✅ TOTAL_CAPITAL 一致性 OK: config={cfg_total:.0f} vs 账户={usdt_balance:.0f} USDT "
                        f"(偏差 {gap_pct:.1%})"
                    )
            elif usdt_balance <= 0:
                logger.warning("⚠️ 无法获取账户 USDT 余额，跳过一致性检查")
        except Exception as e:
            logger.warning(f"⚠️ TOTAL_CAPITAL 一致性检查失败（不影响启动）: {e}")

    stop_event = threading.Event()
    threads = []

    if _startup_blocked:
        logger.error("启动被阻断，30分钟后可手动重启。如需强制启动，删除 guard_state.json")
        # 仍然启动一个监督心跳（不交易，等人工确认）
        import time as _time
        while True:
            try:
                Path(__file__).parent / ".bot_heartbeat"
                (Path(__file__).parent / ".bot_heartbeat").touch()
            except:
                pass
            _time.sleep(10)
        return

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
        from state_db import GuardStateDB
        _hb_db = GuardStateDB()
        last_macro_update = 0
        last_hb_check = 0
        last_db_hb = 0
        heartbeat_path = Path(__file__).parent / ".bot_heartbeat"
        _alert_state = {"last_stale": 0, "last_stop": 0, "was_alerted": False}
        while True:
            now = time.time()
            # 心跳：每轮 tick 更新
            heartbeat_path.touch()
            # DB 心跳：每 15 分钟写一次，让 watchdog 区分"无成交"和"系统卡死"
            if now - last_db_hb > 900:
                try:
                    _hb_db.write_heartbeat()
                except Exception:
                    pass
                last_db_hb = now

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
