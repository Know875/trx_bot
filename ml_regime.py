"""
ML 行情分类器 — 用历史 15m K 线训练，预测近未来 regime。

定位：作为 market_analyzer 规则引擎的**增强层**，不是替代。
  - 训练数据：backtest_data/*_90d.csv（15m，与实盘 CANDLE_BAR 一致）
  - 标签：向前看 N 根的收益，按波动自适应阈值分 3 类（ranging / up / down）
  - 推理：predict() 带置信度；缺模型 / 缺 sklearn / 低置信 → 返回 None（调用方回退规则）

特征与 market_analyzer.calc_indicators 同源（ADX 直接复用），保证训练/推理一致。

用法：
  python ml_regime.py train [coin|all]   # 训练并保存模型 + 打印验证集表现
  python ml_regime.py eval  [coin|all]   # 查看已训练模型的表现
模型保存在 models/{coin}_regime.joblib（已 gitignore，每台机器各自训练）
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger("ml_regime")

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "backtest_data"
MODEL_DIR = ROOT / "models"

# ── 标签参数 ───────────────────────────────────────────────
LABEL_LOOKAHEAD = 8       # 向前看 8 根 15m = 2 小时
LABEL_VOL_MULT  = 1.0     # 判趋势的阈值 = LABEL_VOL_MULT × atr_pct（波动自适应）
LABEL_MIN_THR   = 0.003   # 阈值下限，避免极低波动时乱标趋势

FEATURE_COLS = [
    "ema_diff", "bb_width", "atr_pct", "rsi", "adx",
    "di_diff", "macd_hist", "vol_ratio", "mom_4", "mom_16",
]

# coin → 15m CSV
CSV_MAP = {
    "TRX":      "TRX_USDT_90d.csv",
    "ETH":      "ETH_USDT_90d.csv",
    "SOL":      "SOL_USDT_90d.csv",
    "TRX_SWAP": "TRX_USDT_SWAP_90d.csv",
    "ETH_SWAP": "ETH_USDT_SWAP_90d.csv",
    "SOL_SWAP": "SOL_USDT_SWAP_90d.csv",
}

_LABEL_NAMES = {0: "ranging", 1: "trending_up", 2: "trending_down"}


# ═══════════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════════

def _load_csv(coin: str) -> pd.DataFrame | None:
    fname = CSV_MAP.get(coin)
    if not fname:
        return None
    path = DATA_DIR / fname
    if not path.exists():
        return None
    df = pd.read_csv(path)
    for col in ("open", "high", "low", "close", "vol"):
        df[col] = df[col].astype(float)
    return df.reset_index(drop=True)


def _load_from_okx(coin: str, total: int = 8000) -> pd.DataFrame | None:
    """从 OKX 拉 15m 历史（~80+ 天），样本远多于本地 CSV。失败返回 None。"""
    try:
        import config
        from okx_client import OKXClient
        from market_analyzer import parse_candles
        cfg = config.COIN_CONFIG.get(coin)
        if not cfg:
            return None
        client = OKXClient(symbol=cfg["symbol"], market_flag=cfg.get("market_flag"))
        raw = client.get_history_candles(bar="15m", total=total)
        if not raw:
            return None
        df = parse_candles(raw)
        return df if len(df) >= 1000 else None
    except Exception as e:
        logger.warning(f"{coin}: OKX 历史拉取失败，改用本地 CSV: {e}")
        return None


def _load_training_df(coin: str) -> pd.DataFrame | None:
    """训练数据：优先 OKX 历史（多），回退本地 CSV（少但离线可用）。"""
    df = _load_from_okx(coin)
    if df is not None and len(df) >= 1000:
        logger.info(f"{coin}: 使用 OKX 历史 {len(df)} 根 15m")
        return df
    df = _load_csv(coin)
    if df is not None:
        logger.info(f"{coin}: 使用本地 CSV {len(df)} 根 15m")
    return df


# ═══════════════════════════════════════════════════════════
# 特征工程（与 market_analyzer.calc_indicators 同源）
# ═══════════════════════════════════════════════════════════

def _feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """对整段 K 线计算逐行特征。训练与推理共用此函数，保证一致。"""
    from market_analyzer import _calc_adx  # 复用同一份 ADX 实现

    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    vol   = df["vol"].astype(float)

    out = pd.DataFrame(index=df.index)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    ema60 = close.ewm(span=60, adjust=False).mean()
    out["ema_diff"] = (ema20 - ema60) / ema60.replace(0, np.nan)

    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    out["bb_width"] = (bb_std * 4) / bb_mid.replace(0, np.nan)

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    out["atr_pct"] = atr / close.replace(0, np.nan)

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.where(loss > 0, np.nan)
    rsi = (100 - 100 / (1 + rs))
    out["rsi"] = rsi.where((gain > 0) | (loss > 0), 50.0).fillna(50.0)

    macd = ema12 - ema26
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    out["macd_hist"] = (macd - macd_sig) / close.replace(0, np.nan)

    adx, plus_di, minus_di = _calc_adx(high, low, close)
    out["adx"] = adx
    out["di_diff"] = plus_di - minus_di

    vol_ma = vol.rolling(20).mean()
    out["vol_ratio"] = (vol / vol_ma.replace(0, np.nan)).fillna(1.0)

    out["mom_4"]  = close.pct_change(4)
    out["mom_16"] = close.pct_change(16)

    return out


def _atr_pct_series(df: pd.DataFrame) -> pd.Series:
    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return (tr.rolling(14).mean()) / close.replace(0, np.nan)


def _make_labels(df: pd.DataFrame) -> pd.Series:
    """向前看 LABEL_LOOKAHEAD 根，按波动自适应阈值打 3 类标签。"""
    close = df["close"].astype(float)
    fwd = close.shift(-LABEL_LOOKAHEAD) / close - 1.0
    thr = (LABEL_VOL_MULT * _atr_pct_series(df)).clip(lower=LABEL_MIN_THR)

    lab = pd.Series(0, index=df.index, dtype="float")  # 0 = ranging
    lab[fwd > thr] = 1   # trending_up
    lab[fwd < -thr] = 2  # trending_down
    lab[fwd.isna()] = np.nan
    return lab


def _rule_label_vectorized(feats: pd.DataFrame) -> pd.Series:
    """向量化复刻 market_analyzer 通用规则，用于和 ML 做对照（wait 归入 ranging）。"""
    ema_diff = feats["ema_diff"]
    bb = feats["bb_width"]
    atrp = feats["atr_pct"]
    adx = feats["adx"]
    mom = feats["mom_4"]

    is_trend = (ema_diff.abs() > 0.007) & (bb > 0.012) & (atrp > 0.002) & (adx > 25)
    is_range = (bb < 0.030) & (ema_diff.abs() < 0.015) & (atrp < 0.015)
    is_dead  = (bb < 0.012) & (atrp < 0.005) & (ema_diff.abs() < 0.005)

    lab = pd.Series(0, index=feats.index)  # 默认 ranging（含 wait）
    trend_dir = np.where(ema_diff > 0, 1, 2)
    lab[is_trend & ~(is_range | is_dead)] = trend_dir[is_trend & ~(is_range | is_dead)]
    # 动量覆盖（既非趋势也非震荡时）
    neither = ~is_trend & ~is_range & ~is_dead
    lab[neither & (mom > 0.03)] = 1
    lab[neither & (mom < -0.03)] = 2
    return lab


# ═══════════════════════════════════════════════════════════
# 训练
# ═══════════════════════════════════════════════════════════

def _model_path(coin: str) -> Path:
    return MODEL_DIR / f"{coin}_regime.joblib"


def train(coin: str) -> dict | None:
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import accuracy_score, f1_score
        import joblib
    except ImportError:
        logger.error("缺少 scikit-learn，请先 pip install scikit-learn")
        return None

    df = _load_training_df(coin)
    if df is None or len(df) < 500:
        logger.warning(f"{coin}: 无足够训练数据（OKX 历史 + backtest_data/{CSV_MAP.get(coin)} 均不可用）")
        return None

    feats = _feature_frame(df)
    labels = _make_labels(df)
    data = feats.copy()
    data["_y"] = labels
    data = data.dropna()
    if len(data) < 400:
        logger.warning(f"{coin}: 清洗后样本不足（{len(data)}）")
        return None

    X = data[FEATURE_COLS]
    y = data["_y"].astype(int)

    # 时间序列切分（前 80% 训练，后 20% 验证，绝不打乱，防未来信息泄漏）
    k = int(len(X) * 0.8)
    X_tr, X_te = X.iloc[:k], X.iloc[k:]
    y_tr, y_te = y.iloc[:k], y.iloc[k:]

    model = RandomForestClassifier(
        n_estimators=200, max_depth=8, min_samples_leaf=50,
        class_weight="balanced", random_state=42, n_jobs=-1,
    )
    model.fit(X_tr, y_tr)

    pred = model.predict(X_te)
    ml_acc = accuracy_score(y_te, pred)
    ml_f1 = f1_score(y_te, pred, average="macro")

    # 规则引擎在同一验证集上的准确率（对照）
    rule_pred = _rule_label_vectorized(X_te)
    rule_acc = accuracy_score(y_te, rule_pred)

    dist = y.value_counts(normalize=True).to_dict()
    importances = dict(sorted(
        zip(FEATURE_COLS, model.feature_importances_),
        key=lambda kv: kv[1], reverse=True,
    ))

    MODEL_DIR.mkdir(exist_ok=True)
    bundle = {
        "model": model,
        "features": FEATURE_COLS,
        "coin": coin,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "val_acc": round(float(ml_acc), 4),
        "val_f1": round(float(ml_f1), 4),
        "rule_acc": round(float(rule_acc), 4),
        "n_train": int(len(X_tr)),
        "n_val": int(len(X_te)),
        "lookahead": LABEL_LOOKAHEAD,
    }
    joblib.dump(bundle, _model_path(coin))

    print(f"\n【{coin}】训练完成  样本 train={len(X_tr)} val={len(X_te)}")
    print(f"  ML  验证准确率: {ml_acc:.1%}  (macro-F1 {ml_f1:.3f})")
    print(f"  规则验证准确率: {rule_acc:.1%}   → ML {'优于' if ml_acc > rule_acc else '未超过'}规则 "
          f"{abs(ml_acc - rule_acc)*100:+.1f}pp")
    print(f"  标签分布: " + ", ".join(f"{_LABEL_NAMES[int(c)]}={p:.0%}" for c, p in sorted(dist.items())))
    print(f"  特征重要性 Top5: " + ", ".join(f"{k}={v:.2f}" for k, v in list(importances.items())[:5]))
    return bundle


def train_all():
    for coin in CSV_MAP:
        try:
            train(coin)
        except Exception as e:
            logger.error(f"{coin} 训练失败: {e}")


# ═══════════════════════════════════════════════════════════
# 推理（实盘调用）
# ═══════════════════════════════════════════════════════════

_MODEL_CACHE: dict = {}   # coin → bundle | False（False 表示无模型，避免每 tick 重读磁盘）


def _get_model(coin: str):
    if coin in _MODEL_CACHE:
        return _MODEL_CACHE[coin]
    bundle = False
    path = _model_path(coin)
    if path.exists():
        try:
            import joblib
            bundle = joblib.load(path)
        except Exception as e:
            logger.warning(f"{coin} 模型加载失败: {e}")
            bundle = False
    _MODEL_CACHE[coin] = bundle
    return bundle


def predict(df: pd.DataFrame, coin: str):
    """返回 (regime, confidence) 或 None（无模型/缺依赖/特征缺失时回退）。"""
    bundle = _get_model(coin)
    if not bundle:
        return None
    try:
        model = bundle["model"]
        cols = bundle["features"]
        feats = _feature_frame(df)
        if feats.empty:
            return None
        x = feats.iloc[[-1]][cols]
        if x.isnull().any(axis=1).iloc[0]:
            return None
        proba = model.predict_proba(x)[0]
        i = int(np.argmax(proba))
        cls = int(model.classes_[i])
        return _LABEL_NAMES.get(cls, "ranging"), float(proba[i])
    except Exception as e:
        logger.warning(f"{coin} ML 推理异常: {e}")
        return None


def reset_cache():
    _MODEL_CACHE.clear()


# ═══════════════════════════════════════════════════════════
# 评估
# ═══════════════════════════════════════════════════════════

def evaluate(coin: str):
    bundle = _get_model(coin)
    if not bundle:
        print(f"【{coin}】尚无模型，请先 train")
        return
    print(f"【{coin}】训练于 {bundle.get('trained_at','?')[:19]} | "
          f"ML 验证准确率 {bundle.get('val_acc',0):.1%} (F1 {bundle.get('val_f1',0):.3f}) | "
          f"规则 {bundle.get('rule_acc',0):.1%} | "
          f"样本 {bundle.get('n_train','?')}/{bundle.get('n_val','?')}")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "train"
    target = sys.argv[2] if len(sys.argv) > 2 else "all"

    if cmd == "train":
        if target == "all":
            train_all()
        else:
            train(target)
    elif cmd == "eval":
        coins = list(CSV_MAP) if target == "all" else [target]
        for c in coins:
            evaluate(c)
    else:
        print("用法: python ml_regime.py [train|eval] [coin|all]")
        sys.exit(1)
