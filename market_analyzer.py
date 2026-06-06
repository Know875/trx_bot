import pandas as pd
import numpy as np


def parse_candles(raw):
    """将 OKX 返回的原始 K 线转为 DataFrame"""
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
    df = df[df["confirm"] == "1"].copy()
    for col in ["open", "high", "low", "close", "vol"]:
        df[col] = df[col].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    df = df.sort_values("ts").reset_index(drop=True)
    # 过滤冻结 K 线：vol=0 且四价相同，属于 OKX 返回的占位数据，会污染 RSI 等指标
    frozen = (df["vol"] == 0) & (df["open"] == df["close"]) & (df["high"] == df["low"])
    df = df[~frozen].reset_index(drop=True)
    return df


def _calc_adx(high, low, close, period=14):
    """ADX + DI± 计算"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    up_move   = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    atr14     = tr.ewm(span=period, min_periods=period, adjust=False).mean()
    plus_di   = 100 * plus_dm.ewm(span=period, min_periods=period, adjust=False).mean() / atr14.replace(0, np.nan)
    minus_di  = 100 * minus_dm.ewm(span=period, min_periods=period, adjust=False).mean() / atr14.replace(0, np.nan)
    dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx       = dx.ewm(span=period, min_periods=period, adjust=False).mean()
    return adx.fillna(0), plus_di.fillna(0), minus_di.fillna(0)


def calc_indicators(df):
    """计算并返回最新指标值（含 ADX、成交量比）"""
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["vol"]

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    ema60 = close.ewm(span=60, adjust=False).mean()

    # Bollinger Band 宽度
    bb_mid   = close.rolling(20).mean()
    bb_std   = close.rolling(20).std()
    bb_width = (bb_std * 4) / bb_mid
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    # ATR
    prev_close = close.shift(1)
    tr  = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    atr_pct = atr / close

    # RSI(14)
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.where(loss > 0, np.nan)
    rsi   = (100 - 100 / (1 + rs)).fillna(100.0)  # loss=0 且 gain>0 → 纯上涨 RSI=100
    # 修正平值行情（gain=0 且 loss=0）：纯零时 fillna 会填成 100，需改为 50
    rsi   = rsi.where((gain > 0) | (loss > 0), 50.0)

    # MACD
    macd_line   = ema12 - ema26
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist   = macd_line - macd_signal

    # ADX(14)
    adx, plus_di, minus_di = _calc_adx(high, low, close)

    # 成交量比（当前 / 20 周期均量）
    vol_ma     = vol.rolling(20).mean()
    vol_ratio  = (vol / vol_ma.replace(0, np.nan)).fillna(1.0)

    return {
        "price":      close.iloc[-1],
        "ema12":      ema12.iloc[-1],
        "ema20":      ema20.iloc[-1],
        "ema26":      ema26.iloc[-1],
        "ema60":      ema60.iloc[-1],
        "bb_width":   bb_width.iloc[-1],
        "bb_upper":   bb_upper.iloc[-1],
        "bb_lower":   bb_lower.iloc[-1],
        "atr":        atr.iloc[-1],
        "atr_pct":    atr_pct.iloc[-1],
        "rsi":        rsi.iloc[-1],
        "macd":       macd_line.iloc[-1],
        "macd_signal":macd_signal.iloc[-1],
        "macd_hist":  macd_hist.iloc[-1],
        "adx":        adx.iloc[-1],
        "plus_di":    plus_di.iloc[-1],
        "minus_di":   minus_di.iloc[-1],
        "vol_ratio":  vol_ratio.iloc[-1],
        # VWAP（成交量加权均价，用于网格锚点）
        "vwap":      (df["close"] * df["vol"]).sum() / df["vol"].sum() if df["vol"].sum() > 0 else df["close"].iloc[-1],
        # 前一根 K 线信号（用于金叉/死叉判断 + BB宽度扩张）
        "ema12_prev": ema12.iloc[-2] if len(ema12) > 1 else ema12.iloc[-1],
        "ema26_prev": ema26.iloc[-2] if len(ema26) > 1 else ema26.iloc[-1],
        "bb_width_prev": bb_width.iloc[-2] if len(bb_width) > 1 else bb_width.iloc[-1],
    }


def detect_regime(df, symbol=""):
    """
    识别当前行情模式，返回 (regime, indicators)
    regime: 'ranging' | 'trending_up' | 'trending_down' | 'wait'
    支持币种专属阈值：symbol 包含 "SOL" 时使用 SOL_REGIME
    """
    if len(df) < 60:
        return "wait", {}

    ind      = calc_indicators(df)
    ema_diff = (ind["ema20"] - ind["ema60"]) / ind["ema60"]
    adx      = ind["adx"]

    # ── 币种专属阈值 ──────────────────────────────────────
    import config
    is_sol = symbol and "SOL" in symbol
    is_trx = symbol and "TRX" in symbol

    if is_sol:
        sr = config.SOL_REGIME
        t_ema_min, t_bb_min, t_atr_min, t_adx_min = sr["t_ema_diff_min"], sr["t_bb_width_min"], sr["t_atr_pct_min"], sr["t_adx_min"]
        r_bb_max, r_ema_max, r_atr_max = sr["r_bb_width_max"], sr["r_ema_diff_max"], sr["r_atr_pct_max"]
        d_bb_max, d_atr_max, d_ema_max = sr["d_bb_width_max"], sr["d_atr_pct_max"], sr["d_ema_diff_max"]
        momentum_pct = sr.get("momentum_override", 0.03)
    else:
        # 通用阈值
        t_ema_min, t_bb_min, t_atr_min, t_adx_min = 0.007, 0.012, 0.002, 25
        r_bb_max, r_ema_max, r_atr_max = 0.030, 0.015, 0.015
        d_bb_max, d_atr_max, d_ema_max = 0.012, 0.005, 0.005
        momentum_pct = 0.03  # 非 SOL 币种也保留动量覆盖能力

    # ── 动量覆盖：1小时内大幅单边 → 直接判趋势 ───────────
    lookback = min(4, len(df))  # 4 根 15m K 线 = 1 小时
    if lookback >= 2:
        price_change = (df["close"].iloc[-1] - df["close"].iloc[-lookback]) / df["close"].iloc[-lookback]
    else:
        price_change = 0.0

    # ── 趋势判定 ──────────────────────────────────────────
    is_trending = (
        abs(ema_diff) > t_ema_min
        and ind["bb_width"] > t_bb_min
        and ind["atr_pct"] > t_atr_min
        and adx > t_adx_min
    )

    # ── 震荡判定 ──────────────────────────────────────────
    is_ranging = (
        ind["bb_width"] < r_bb_max
        and abs(ema_diff) < r_ema_max
        and ind["atr_pct"] < r_atr_max
    )

    # ── 极端窄幅（死盘）───────────────────────────────────
    is_dead_range = (
        ind["bb_width"] < d_bb_max
        and ind["atr_pct"] < d_atr_max
        and abs(ema_diff) < d_ema_max
    )

    if is_ranging or is_dead_range:
        regime = "ranging"
    elif is_trending:
        regime = "trending_up" if ind["ema20"] > ind["ema60"] else "trending_down"
    else:
        # 动量覆盖：既不满足震荡也不满足趋势，但价格大幅单边 → 强判
        if abs(price_change) > momentum_pct:
            regime = "trending_up" if price_change > 0 else "trending_down"
        else:
            regime = "wait"

    return regime, ind


def get_range_bounds(df, range_pct=0.04):
    """计算网格区间的上下界（基于近期高低点）"""
    recent = df.tail(20)
    mid = (recent["high"].max() + recent["low"].min()) / 2
    upper = mid * (1 + range_pct / 2)
    lower = mid * (1 - range_pct / 2)
    return lower, upper
