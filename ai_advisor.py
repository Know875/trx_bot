"""
AI 行情顾问：调用 DeepSeek API 做技术面+基本面双层分析
- get_ai_regime_hint(): 原版，纯技术指标（向后兼容）
- get_enhanced_ai_advice(): 增强版，技术+基本面 → 返回仓位建议+风险警示
任何异常都静默降级，返回 None，不影响主策略运行
"""
import json
import logging
import httpx
import config

logger = logging.getLogger("ai_advisor")

_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
_MODEL = "deepseek-chat"
_TIMEOUT = 15  # 秒

_SYSTEM_PROMPT = """你是一个专业的加密货币量化交易分析师。
你的任务是根据技术指标对当前行情做出判断，输出必须是合法的 JSON，不要有任何多余文字。"""

_USER_PROMPT_TMPL = """当前 {symbol} 技术指标如下：
- 价格: {price:.6f} USDT
- EMA20: {ema20:.6f}  EMA60: {ema60:.6f}  EMA偏离: {ema_diff:+.4%}
- 布林带宽度(BB_width): {bb_width:.4f}
- ATR: {atr:.6f}  ATR占价格比: {atr_pct:.4%}
- RSI(14): {rsi:.1f}

规则引擎判断结果: {rule_regime}

请综合以上指标，给出你的行情判断。
输出格式（严格 JSON，不要 markdown 代码块）：
{{"regime": "<ranging|trending_up|trending_down|wait>", "confidence": <0.0~1.0>, "reason": "<简短中文理由，30字以内>"}}"""


def get_ai_regime_hint(indicators: dict, rule_regime: str, symbol: str = "CRYPTO/USDT") -> dict | None:
    """
    调用 DeepSeek 对规则引擎结论做二次验证。
    返回 dict 或 None（失败时降级）。
    """
    if not getattr(config, "DEEPSEEK_API_KEY", ""):
        return None

    prompt = _USER_PROMPT_TMPL.format(
        symbol=symbol,
        price=indicators.get("price", 0),
        ema20=indicators.get("ema20", 0),
        ema60=indicators.get("ema60", 0),
        ema_diff=(indicators.get("ema20", 0) - indicators.get("ema60", 1)) / indicators.get("ema60", 1),
        bb_width=indicators.get("bb_width", 0),
        atr=indicators.get("atr", 0),
        atr_pct=indicators.get("atr_pct", 0),
        rsi=indicators.get("rsi", 50),
        rule_regime=rule_regime,
    )

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = httpx.post(
            _DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        result = json.loads(content)

        regime = result.get("regime", "").strip()
        confidence = float(result.get("confidence", 0))
        reason = result.get("reason", "")

        if regime not in ("ranging", "trending_up", "trending_down", "wait"):
            logger.warning(f"AI 返回了未知 regime: {regime}，忽略")
            return None

        logger.info(f"AI 建议: {regime} (置信度={confidence:.0%}) | {reason}")
        return {"regime": regime, "confidence": confidence, "reason": reason}

    except httpx.TimeoutException:
        logger.warning("AI 顾问请求超时，跳过")
    except Exception as e:
        logger.warning(f"AI 顾问异常: {e}，跳过")
    return None


# ═══════════════════════════════════════════════════════════════
# 增强版 AI 顾问：技术面 + 基本面双层分析
# ═══════════════════════════════════════════════════════════════

_ENHANCED_SYSTEM = """你是一个专业的加密货币量化交易分析师，擅长综合技术指标和基本面数据做判断。
你的输出必须是合法的 JSON，不要有任何多余文字。"""

_ENHANCED_PROMPT_TMPL = """当前 {symbol} 的多层信息：

{tech_context}

{fund_context}
规则引擎判断结果: {rule_regime}

请综合以上技术+基本面信息，给出你的判断：
1. 行情判断（regime）
2. 置信度（confidence: 0~1）
3. 趋势入场建议（entry_advice: "full"全仓/ "half"半仓/ "skip"跳过）
4. 风险提示（warning_flags: 数组，如 ["资金费率偏空","市场恐慌","假突破风险"]）
5. 简短理由（reason: 30字以内）

输出格式（严格 JSON）：
{{"regime": "<ranging|trending_up|trending_down|wait>", "confidence": <0.0~1.0>, "entry_advice": "<full|half|skip>", "warning_flags": [...], "reason": "..."}}"""


def get_enhanced_ai_advice(indicators: dict, fund_context: str, rule_regime: str,
                           symbol: str = "CRYPTO/USDT") -> dict | None:
    """
    增强版 AI 顾问：技术面 + 基本面双层分析。
    返回包含仓位建议和风险警示的 dict，失败时返回 None。
    """
    if not getattr(config, "DEEPSEEK_API_KEY", ""):
        return None

    tech = (
        f"[技术层]\n"
        f"  价格: {indicators.get('price', 0):.6f} USDT\n"
        f"  EMA偏离: {(indicators.get('ema20', 0) - indicators.get('ema60', 1)) / indicators.get('ema60', 1):+.4%}\n"
        f"  布林带宽度: {indicators.get('bb_width', 0):.4f}\n"
        f"  ATR占价格比: {indicators.get('atr_pct', 0):.4%}\n"
        f"  RSI(14): {indicators.get('rsi', 50):.1f}\n"
        f"  ADX: {indicators.get('adx', 0):.1f}"
    )

    prompt = _ENHANCED_PROMPT_TMPL.format(
        symbol=symbol,
        tech_context=tech,
        fund_context=fund_context or "",
        rule_regime=rule_regime,
    )

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _ENHANCED_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = httpx.post(
            _DEEPSEEK_URL,
            headers={
                "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        result = json.loads(content)

        regime = result.get("regime", "").strip()
        confidence = float(result.get("confidence", 0))
        entry_advice = result.get("entry_advice", "half")
        warning_flags = result.get("warning_flags", [])
        reason = result.get("reason", "")

        if regime not in ("ranging", "trending_up", "trending_down", "wait"):
            logger.warning(f"增强AI 返回了未知 regime: {regime}，忽略")
            return None

        logger.info(
            f"增强AI: {regime} 置信={confidence:.0%} "
            f"入场={entry_advice} 风险={warning_flags} | {reason}"
        )
        return {
            "regime": regime,
            "confidence": confidence,
            "entry_advice": entry_advice,
            "warning_flags": warning_flags,
            "reason": reason,
        }

    except httpx.TimeoutException:
        logger.warning("增强AI 请求超时，降级到规则引擎")
    except Exception as e:
        logger.warning(f"增强AI 异常: {e}，降级到规则引擎")
    return None


# ═══════════════════════════════════════════════════════════════
# 安全包装：AI 只能降风险，不能加风险
# ═══════════════════════════════════════════════════════════════

# 入场级别 → 仓位倍数
_ENTRY_MAP = {"full": 1.0, "half": 0.6, "skip": 0.0}

# baseline_mult: 规则引擎决定的仓位（如 macro_adjustment 后）
# ai_advice: AI 返回的增强建议（可能为 None）
# 规则：AI 只能把仓位向下调整，不能向上


def safe_ai_advice(baseline_mult: float, ai_advice: dict | None) -> dict:
    """
    AI 安全包装：只允许降低风险。
    
    返回:
      - effective_mult: 实际生效的仓位倍数（≤ baseline_mult）
      - ai_entry: AI 推荐的入场级别
      - ai_regime: AI 判断的行情（可为 None）
      - warnings: AI 提示的风险标记
      - downgraded: 是否被 AI 降级了
    """
    if ai_advice is None or not isinstance(ai_advice, dict):
        return {
            "effective_mult": baseline_mult,
            "ai_entry": None,
            "ai_regime": None,
            "warnings": [],
            "downgraded": False,
        }

    ai_entry = ai_advice.get("entry_advice", "half")
    ai_mult = _ENTRY_MAP.get(ai_entry, 0.6)

    # 核心安全约束：AI 只能降低仓位
    effective_mult = min(baseline_mult, ai_mult)

    result = {
        "effective_mult": effective_mult,
        "ai_entry": ai_entry,
        "ai_regime": ai_advice.get("regime"),
        "warnings": ai_advice.get("warning_flags", []),
        "downgraded": effective_mult < baseline_mult,
    }

    if result["downgraded"]:
        logger.info(
            f"🛡️ AI 安全降级: 仓位 {baseline_mult:.0%}→{effective_mult:.0%} "
            f"({ai_entry}), 风险标记: {result['warnings']}"
        )

    return result
