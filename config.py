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

# ── ML 行情分类器（增强 regime 判定）─────────────────────────
# 缺模型 / 未装 sklearn / 置信度不足时自动回退规则引擎，不影响实盘。
# 关闭：环境变量 USE_ML_REGIME=0。模型训练：python ml_regime.py train all
USE_ML_REGIME        = _env("USE_ML_REGIME", "1") == "1"
ML_REGIME_CONFIDENCE = float(_env("ML_REGIME_CONFIDENCE", "0.70"))  # 覆盖规则所需最低置信度

# ── OKX API ─────────────────────────────────────────────────
API_KEY    = _env("OKX_API_KEY")
SECRET_KEY = _env("OKX_SECRET_KEY")
PASSPHRASE = _env("OKX_PASSPHRASE")
FLAG       = _env("OKX_FLAG", "1")  # "1"=模拟盘  "0"=实盘

# ── Dashboard 认证 ──────────────────────────────────────────
DASHBOARD_USERNAME = _env("DASHBOARD_USERNAME", "")
DASHBOARD_PASSWORD = _env("DASHBOARD_PASSWORD", "")

SYMBOL         = "TRX-USDT"
INITIAL_CAPITAL = 1000.0   # 向后兼容

# ── 多币种配置 ─────────────────────────────────────────────────
COINS = ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]

COIN_CONFIG = {
    # ── 现货 ──
    # initial_capital：×2 扩大部署（原 29000 总额→58000），真实账户权益约 79688 USDT，
    # 峰值占用约 3 万 / 75k（~40% 利用率，留充足缓冲）。稳定后可再上调。
    "TRX": {
        "symbol": "TRX-USDT", "mode": "spot",
        "initial_capital": 10000.0, "size_decimals": 0, "min_order_size": 1,  # was 5000
    },
    "ETH": {
        "symbol": "ETH-USDT", "mode": "spot",
        "initial_capital": 10000.0, "size_decimals": 4, "min_order_size": 0.001,  # was 5000
    },
    "SOL": {
        "symbol": "SOL-USDT", "mode": "spot",
        "initial_capital": 12000.0, "size_decimals": 2, "min_order_size": 0.01,  # was 6000
    },
    # ── 合约（永续）──
    "TRX_SWAP": {
        "symbol": "TRX-USDT-SWAP", "mode": "futures",
        "initial_capital": 8000.0, "base_ccy": "TRX",  # was 4000
        "ct_val": 1000.0,   # 每张合约面值（TRX）
        "size_decimals": 0, "min_order_size": 1,
        "leverage": 3,       # TRX 波动大，低杠杆
    },
    "ETH_SWAP": {
        "symbol": "ETH-USDT-SWAP", "mode": "futures",
        "initial_capital": 9000.0, "base_ccy": "ETH",  # was 4500
        "ct_val": 0.01,     # 每张合约面值（ETH）
        "size_decimals": 4, "min_order_size": 0.001,
        "leverage": 5,       # ETH 波动中等
    },
    "SOL_SWAP": {
        "symbol": "SOL-USDT-SWAP", "mode": "futures",
        "initial_capital": 9000.0, "base_ccy": "SOL",  # was 4500
        "ct_val": 1.0,      # 每张合约面值（SOL）
        "size_decimals": 2, "min_order_size": 0.01,
        "market_flag": "0", # 模拟盘 SOL-USDT-SWAP 价格失真，行情数据从实盘取
        "leverage": 5,       # SOL 波动中等偏高
    },
}

# ── 账户总本金（风控统一基准）─────────────────────────────────
# = 真实账户权益（模拟盘约 79688 USDT：可用 75086 + 持仓占用约 4603）。
# 账户级熔断 (account_guard) 的日亏 2%/3.5%/5% 按此基准换算绝对金额，
# 必须反映真实权益，否则阈值要么过敏感(偏小)要么形同虚设(偏大)。
# 账户增减时请同步更新，或在服务器 .env 设 TOTAL_CAPITAL 覆盖此默认值。
TOTAL_CAPITAL = float(_env("TOTAL_CAPITAL", "79688"))

# ── 手续费率（用于盈亏结算，避免高估利润）──────────────────────
# OKX 现货挂单(maker)约 0.08%，吃单(taker)约 0.10%；网格双边按 maker 估算。
SPOT_FEE_RATE    = float(_env("SPOT_FEE_RATE", "0.001"))     # 现货单边费率
FUTURES_FEE_RATE = float(_env("FUTURES_FEE_RATE", "0.0005")) # 合约单边费率

# ── 回测真实度（让优化/进化的结论可信，不被零成本假象误导）────────
# 回测里每次成交都扣手续费 + 单边滑点；round-trip 成本 ≈ 2×(fee+slippage)。
BACKTEST_SLIPPAGE = float(_env("BACKTEST_SLIPPAGE", "0.0005"))  # 单边滑点估计
BACKTEST_FEE_RATE = float(_env("BACKTEST_FEE_RATE", str(SPOT_FEE_RATE)))

# ── 合约参数 ───────────────────────────────────────────────────
FUTURES_MARGIN_MODE = "isolated"   # isolated / cross
FUTURES_DEFAULT_LEVERAGE = 5       # 默认杠杆（未被 COIN_CONFIG 覆盖时使用）

def get_leverage(ccy: str) -> int:
    """返回币种杠杆：优先 CCY_SWAP 配置，fallback 到默认值"""
    if ccy in COIN_CONFIG and "leverage" in COIN_CONFIG[ccy]:
        return int(COIN_CONFIG[ccy]["leverage"])
    return FUTURES_DEFAULT_LEVERAGE

def get_ct_val(ccy: str) -> float:
    """返回币种合约单张面值"""
    if ccy in COIN_CONFIG:
        return float(COIN_CONFIG[ccy].get("ct_val", 1.0))
    return 1.0

# ── 网格参数（通用）──────────────────────────────────────────
GRID_COUNT          = 5
GRID_RANGE_PCT      = 0.04
GRID_MIN_PROFIT_PCT = 0.002
GRID_FLOAT_PROFIT_TARGET = 150.0   # 现货网格浮盈超过此 USDT 时触发止盈平仓
FUTURES_GRID_MAX_FLOAT_LOSS_PCT = 0.08  # 合约网格持仓浮亏超过本金8%时止损平仓
# 各币种独立阈值（币种 → 比例），不在此列表的用上面的默认值
FUTURES_GRID_MAX_FLOAT_LOSS = {
    "ETH_SWAP": 0.12,   # ETH 波动大，放宽到 12%
    "SOL_SWAP": 0.12,   # SOL 波动大，放宽到 12%
}

# ── 各币种网格参数（180天回测调优）────────────────────────────
ETH_SPOT_GRID_RANGE_PCT = 0.162  # AI#10 曾收窄至 0.120，因回测/实盘表现不佳已回退至 0.162
ETH_SPOT_GRID_COUNT     = 2
ETH_GRID_COUNT          = 2
ETH_GRID_POSITION_PCT   = 0.70
ETH_GRID_RANGE_PCT      = 0.162

SOL_SPOT_GRID_RANGE_PCT = 0.046  # AI#10 曾扩至 0.122(回测Δ+7.29%)，因实盘表现不佳已回退至 0.046
SOL_SPOT_GRID_COUNT     = 2
SOL_GRID_COUNT          = 2
SOL_GRID_POSITION_PCT   = 0.70
SOL_GRID_RANGE_PCT      = 0.088  # AI#10 曾扩至 0.122，现折中回退至 0.088

# ── 趋势参数（180天回测调优）─────────────────────────────────
TREND_TAKE_PROFIT_PCT = 0.025
TREND_STOP_LOSS_PCT   = 0.015
TREND_POSITION_PCT    = 0.45
TREND_TRAILING_PCT    = 0.015
TREND_ATR_STOP_MULT   = 1.5
TREND_ATR_TP_MULT     = 2.5
TREND_RSI_OB          = 70
TREND_RSI_OS          = 30
ETH_TREND_RSI_OB      = 999   # ETH做多不过滤超买（趋势强时追涨合理）
ETH_TREND_RSI_OS      = 20    # ETH做空：RSI<20极度超卖时跳过，防止在反弹前追空
SOL_TREND_RSI_OB      = 65
SOL_TREND_RSI_OS      = 35

# ── TRX 专属参数（基于 TRX 行为研究 + 180天回测）──────────
TRX_GRID_COUNT          = 8  # was 5 → 8 回测确认+3.397%
TRX_GRID_COUNT_NON_PEAK = 9
TRX_GRID_RANGE_PCT      = 0.079  # was 0.05 → 0.079 回测确认+6.469%
TRX_NARROW_GRID_RANGE_PCT = 0.04  # was 0.02 → 避免死盘+波动率适配双重压缩导致间距过小
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

# ── 合约专属仓位参数（futures_trend._position_pct() 读取） ───────
TRX_SWAP_GRID_POSITION_PCT  = 0.30
TRX_SWAP_TREND_POSITION_PCT = 0.15   # TRX 合约趋势：本金15%（低风险）
SOL_SWAP_TREND_POSITION_PCT = 0.25   # SOL 合约趋势：降至25%（原默认45%止损过大）
ETH_SWAP_TREND_POSITION_PCT = 0.30   # ETH 合约趋势：降至30%（-105U/-100U止损偏大）

# ── 风控 ──────────────────────────────────────────────────────
MAX_DRAWDOWN_PCT = 0.10

# ── 仓位上限（防止网格接飞刀无限累积） ─────────────────────
# 各币种持仓市值不超过该币种 initial_capital 的这个比例
# 超过上限后网格只挂卖单不挂买单，降到 70% 以下才恢复买盘
POSITION_CAP_PCT = {
    "TRX": 1.5,     # TRX波动小，可承载150%仓位
    "ETH": 1.2,     # ETH适中
    "SOL": 0.8,     # SOL波动大，严格限制80%
}
# 合约仓位上限（更保守）
SWAP_POSITION_CAP_PCT = {
    "TRX_SWAP": 0.6,
    "ETH_SWAP": 0.5,
    "SOL_SWAP": 0.4,
}

# ── 单币种止损 ───────────────────────────────────────────────
# 未实现亏损（含浮亏）超过该币种 initial_capital 的此比例时强制退出
COIN_STOP_LOSS_PCT = 0.15  # 15%

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
grid_range_pct = 0.122  # was 0.046
