"""
配置中心 — 所有敏感信息从环境变量读取，配置文件不包含密钥。
加载优先级: 环境变量 > .env 文件 > 默认值
"""
import os
from pathlib import Path

# ── 自动加载 .env 文件 ────────────────────────────────────────
_ENV_PATH = Path(__file__).parent / ".env"
if _ENV_PATH.exists():
    with open(_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)

# ── Telegram 通知（留空则不启用）────────────────────────────
TG_BOT_TOKEN = _env("TG_BOT_TOKEN")
TG_CHAT_ID   = _env("TG_CHAT_ID")

# ── DeepSeek AI 顾问（留空则不启用）─────────────────────────
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
AI_CONFIDENCE_THRESHOLD = 0.75

# ── OKX API ─────────────────────────────────────────────────
API_KEY    = _env("OKX_API_KEY")
SECRET_KEY = _env("OKX_SECRET_KEY")
PASSPHRASE = _env("OKX_PASSPHRASE")
FLAG       = _env("OKX_FLAG", "1")  # "1"=模拟盘  "0"=实盘

# ── Dashboard 认证 ──────────────────────────────────────────
DASHBOARD_USERNAME = _env("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = _env("DASHBOARD_PASSWORD", "admin123")

SYMBOL         = "TRX-USDT"
INITIAL_CAPITAL = 1000.0   # 向后兼容

# ── 多币种配置 ─────────────────────────────────────────────────
COINS = ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]

COIN_CONFIG = {
    # ── 现货 ──
    "TRX": {
        "symbol": "TRX-USDT", "mode": "spot",
        "initial_capital": 3000.0, "size_decimals": 0, "min_order_size": 1,
    },
    "ETH": {
        "symbol": "ETH-USDT", "mode": "spot",
        "initial_capital": 3000.0, "size_decimals": 4, "min_order_size": 0.001,
    },
    "SOL": {
        "symbol": "SOL-USDT", "mode": "spot",
        "initial_capital": 4000.0, "size_decimals": 2, "min_order_size": 0.01,
    },
    # ── 合约（永续）──
    "TRX_SWAP": {
        "symbol": "TRX-USDT-SWAP", "mode": "futures",
        "initial_capital": 2000.0, "base_ccy": "TRX",
        "ct_val": 1000.0,   # 每张合约面值（TRX）
        "size_decimals": 0, "min_order_size": 1,
    },
    "ETH_SWAP": {
        "symbol": "ETH-USDT-SWAP", "mode": "futures",
        "initial_capital": 2500.0, "base_ccy": "ETH",
        "ct_val": 0.01,     # 每张合约面值（ETH）
        "size_decimals": 4, "min_order_size": 0.001,
    },
    "SOL_SWAP": {
        "symbol": "SOL-USDT-SWAP", "mode": "futures",
        "initial_capital": 2500.0, "base_ccy": "SOL",
        "ct_val": 1.0,      # 每张合约面值（SOL）
        "size_decimals": 2, "min_order_size": 0.01,
        "market_flag": "0", # 模拟盘 SOL-USDT-SWAP 价格失真，行情数据从实盘取
    },
}

# ── 账户总本金（风控统一基准）─────────────────────────────────
# 默认 = 各币种 initial_capital 之和；用环境变量 TOTAL_CAPITAL 覆盖为真实入金。
# 账户级熔断 (account_guard) 与各处风控统一引用此值，避免口径不一致。
TOTAL_CAPITAL = float(_env("TOTAL_CAPITAL",
                           str(sum(c["initial_capital"] for c in COIN_CONFIG.values()))))

# ── 手续费率（用于盈亏结算，避免高估利润）──────────────────────
# OKX 现货挂单(maker)约 0.08%，吃单(taker)约 0.10%；网格双边按 maker 估算。
SPOT_FEE_RATE    = float(_env("SPOT_FEE_RATE", "0.001"))     # 现货单边费率
FUTURES_FEE_RATE = float(_env("FUTURES_FEE_RATE", "0.0005")) # 合约单边费率

# ── 合约参数 ───────────────────────────────────────────────────
FUTURES_LEVERAGE    = 5
FUTURES_MARGIN_MODE = "isolated"   # isolated / cross

# ── 网格参数（通用）──────────────────────────────────────────
GRID_COUNT          = 5
GRID_RANGE_PCT      = 0.04
GRID_MIN_PROFIT_PCT = 0.002
FUTURES_GRID_MAX_FLOAT_LOSS_PCT = 0.08  # 合约网格持仓浮亏超过本金8%时止损平仓
# 各币种独立阈值（币种 → 比例），不在此列表的用上面的默认值
FUTURES_GRID_MAX_FLOAT_LOSS = {
    "ETH_SWAP": 0.12,   # ETH 波动大，放宽到 12%
    "SOL_SWAP": 0.12,   # SOL 波动大，放宽到 12%
}

# ── 各币种网格参数（180天回测调优）────────────────────────────
ETH_SPOT_GRID_RANGE_PCT = 0.16
ETH_SPOT_GRID_COUNT     = 2
ETH_GRID_COUNT          = 2
ETH_GRID_POSITION_PCT   = 0.70
ETH_GRID_RANGE_PCT      = 0.12

SOL_SPOT_GRID_RANGE_PCT = 0.04
SOL_SPOT_GRID_COUNT     = 2
SOL_GRID_COUNT          = 2
SOL_GRID_POSITION_PCT   = 0.70
SOL_GRID_RANGE_PCT      = 0.04

# ── 趋势参数（180天回测调优）─────────────────────────────────
TREND_TAKE_PROFIT_PCT = 0.025
TREND_STOP_LOSS_PCT   = 0.015
TREND_POSITION_PCT    = 0.45
TREND_TRAILING_PCT    = 0.015
TREND_ATR_STOP_MULT   = 1.5
TREND_ATR_TP_MULT     = 2.5
TREND_RSI_OB          = 70
TREND_RSI_OS          = 30
ETH_TREND_RSI_OB      = 999
ETH_TREND_RSI_OS      = 0
SOL_TREND_RSI_OB      = 65
SOL_TREND_RSI_OS      = 35

# ── TRX 专属参数（基于 TRX 行为研究 + 180天回测）──────────
TRX_GRID_COUNT          = 3
TRX_GRID_COUNT_NON_PEAK = 9
TRX_GRID_RANGE_PCT      = 0.05
TRX_NARROW_GRID_RANGE_PCT = 0.02
TRX_GRID_MIN_PROFIT_PCT = 0.002
TRX_GRID_POSITION_PCT   = 0.70

TRX_DEAD_RANGE_BB_WIDTH = 0.010
TRX_DEAD_RANGE_ATR_PCT  = 0.003

TRX_VOL_BURST_RATIO     = 3.0
TRX_VOL_BURST_BB_EXPAND = 0.20
TRX_DEAD_VOL_BURST_RATIO = 1.8

TRX_ADX_TREND_MIN       = 25
TRX_ADX_RANGE_MAX       = 28
TRX_VOL_CONFIRM_RATIO   = 1.5
TRX_BREAKOUT_CONFIRM_TICKS = 15
TRX_UNIVERSAL_CONFIRM_TICKS = 10
TRX_TREND_POSITION_PCT  = 0.30
TRX_TREND_TP1_PCT       = 0.50
TRX_TREND_ATR_STOP_MULT = 1.5
TRX_TREND_ATR_TP1_MULT  = 1.5
TRX_TREND_ATR_TP2_MULT  = 3.0
TRX_TREND_TRAILING_PCT  = 0.010
TRX_RSI_ENTRY_MAX       = 65
TRX_RSI_ENTRY_MIN       = 35
TRX_ABNORMAL_VOL_RATIO  = 5.0
TRX_COOLDOWN_TICKS      = 12
TRX_EMA_DIFF_GRID_TO_TREND = 0.008
TRX_EMA_DIFF_CONFIRM_MIN   = 0.003

# ── TRX 合约专属参数 ───────────────────────────────────────────
TRX_SWAP_GRID_POSITION_PCT  = 0.30
TRX_SWAP_TREND_POSITION_PCT = 0.15

# ── 风控 ──────────────────────────────────────────────────────
MAX_DRAWDOWN_PCT = 0.10

# ── SOL 专属行情判定（比通用参数更敏感，适配 SOL 高波动）──
SOL_REGIME = {
    # 趋势判定：降低阈值，更快识别 SOL 的趋势
    "t_ema_diff_min": 0.004,    # 通用 0.007 → SOL 0.004
    "t_bb_width_min": 0.008,    # 通用 0.012 → SOL 0.008
    "t_atr_pct_min": 0.002,     # 通用 0.002 → 不变
    "t_adx_min": 20,            # 通用 25   → SOL 20
    # 震荡判定：收紧范围，让 SOL 更容易退出 ranging
    "r_bb_width_max": 0.022,    # 通用 0.030 → SOL 0.022
    "r_ema_diff_max": 0.008,    # 通用 0.015 → SOL 0.008
    "r_atr_pct_max": 0.012,     # 通用 0.015 → SOL 0.012
    # 死盘（极端窄幅）
    "d_bb_width_max": 0.010,    # 通用 0.012 → 不变
    "d_atr_pct_max": 0.004,     # 通用 0.005 → SOL 0.004
    "d_ema_diff_max": 0.003,    # 通用 0.005 → SOL 0.003
    # 动量覆盖：15分钟内价格变化超过此比例，强制判为趋势
    "momentum_override": 0.015,  # 1.5% → 强制趋势判定
}

# ── SOL 专属网格控制 ──────────────────────────────────────────
SOL_SPOT_MAX_GRID_ENTRIES = 5   # 现货网格最多同时持有 5 个买入档位
SOL_FUTURES_MAX_GRID_ENTRIES = 3.0 # 合约网格最多同时持有 4 个买入档位

# ── 行情 ──────────────────────────────────────────────────────
CANDLE_BAR   = "15m"
CANDLE_LIMIT = 100
