"""
波动率自适应适配器 — 根据当前 ATR 动态调整网格参数。

核心逻辑：
- 高波动 → 加宽网格区间 + 减少档位（避免频繁触价被套）
- 低波动 → 缩窄网格区间 + 增加档位（密集吃小波动）
- 波动率比 > 2.0 → 视为极端，网格不做变更（避免在崩盘/爆拉时乱调）
"""

import logging
import config

logger = logging.getLogger("volatility")


# 各币种基准 ATR%（用于判断"正常波动"级别）
BASELINE_ATR_PCT = {
    "TRX":      0.008,   # TRX 日常 ~0.8%
    "ETH":      0.015,   # ETH 日常 ~1.5%
    "SOL":      0.025,   # SOL 日常 ~2.5%
    "TRX_SWAP": 0.008,
    "ETH_SWAP": 0.015,
    "SOL_SWAP": 0.025,
}

# 自适应参数上下界
WIDTH_MULT_MIN = 0.4    # 网格宽度最低缩到 40%
WIDTH_MULT_MAX = 2.5    # 最高扩到 2.5x
COUNT_MULT_MIN = 0.4    # 档位最低缩到 40%
COUNT_MULT_MAX = 2.5    # 最高扩到 2.5x
EXTREME_VOL_RATIO = 2.5 # 波动率比超过此值视为极端行情，不动网格


def adapt_grid(coin: str, indicators: dict) -> tuple:
    """
    根据波动率自适应调整网格参数。
    
    返回: (width_mult: float, count_mult: float, atr_pct: float, vol_ratio: float)
    
    width_mult: 网格区间宽度乘数（1.0 = 不变，>1 = 加宽）
    count_mult: 网格档位乘数（1.0 = 不变，<1 = 减少档位）
    atr_pct:    当前 ATR%
    vol_ratio:  当前 ATR / 基准 ATR
    """
    atr = indicators.get("atr", 0)
    atr_pct = indicators.get("atr_pct", indicators.get("atr_pct", 0.01))
    
    if atr_pct <= 0:
        return (1.0, 1.0, 0.0, 1.0)
    
    baseline = BASELINE_ATR_PCT.get(coin, 0.015)
    vol_ratio = atr_pct / baseline if baseline > 0 else 1.0
    
    # 极端波动不动
    if vol_ratio >= EXTREME_VOL_RATIO:
        logger.info(f"[{coin}] 极端波动 vol_ratio={vol_ratio:.1f}x — 网格不变更")
        return (1.0, 1.0, atr_pct, vol_ratio)
    
    # 高波动：加宽网格，减少档位
    # 低波动：缩窄网格，增加档位
    width_mult = max(WIDTH_MULT_MIN, min(WIDTH_MULT_MAX, vol_ratio))
    count_mult = max(COUNT_MULT_MIN, min(COUNT_MULT_MAX, 1.0 / max(vol_ratio, 0.3)))
    
    return (width_mult, count_mult, atr_pct, vol_ratio)


def get_adapted_grid_params(coin: str, base_range_pct: float, base_count: int,
                            indicators: dict) -> tuple:
    """
    获取自适应后的网格范围%和档位数。
    
    返回: (adapted_range_pct: float, adapted_count: int, vol_ratio: float)
    """
    width_mult, count_mult, atr_pct, vol_ratio = adapt_grid(coin, indicators)
    
    adapted_range = round(base_range_pct * width_mult, 4)
    adapted_count = max(1, int(base_count * count_mult))
    
    if abs(width_mult - 1.0) > 0.05 or abs(count_mult - 1.0) > 0.05:
        logger.info(
            f"[{coin}] 波动自适: ATR%={atr_pct:.3%} vol_ratio={vol_ratio:.1f}x "
            f"→ 宽度×{width_mult:.2f}({adapted_range:.3%}) 档位×{count_mult:.2f}({adapted_count})"
        )
    
    return (adapted_range, adapted_count, vol_ratio)
