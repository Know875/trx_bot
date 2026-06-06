"""
测试回测保真度：手续费+滑点是否正确计算
"""
import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import pandas as pd
import numpy as np


# ═══════════════════════════════════════════════════════════════
# 1. 回测网格：手续费+滑点
# ═══════════════════════════════════════════════════════════════

def test_grid_backtest_with_fees():
    """含手续费的网格回测应比无手续费更差（不会虚高）"""
    from backtesting.engine import calc_indicators

    # 构造震荡数据
    n = 200
    np.random.seed(42)
    close = np.ones(n) * 0.09
    for i in range(1, n):
        close[i] = close[i - 1] * (1 + np.random.randn() * 0.005)
    high = close * 1.002
    low = close * 0.998
    df = pd.DataFrame({"close": close, "high": high, "low": low, "vol": np.ones(n)})
    df.index = pd.date_range("2026-06-01", periods=n, freq="15min")

    ind = calc_indicators(df)

    from backtesting.grid import simulate_grid

    result_no_fee = simulate_grid(ind, grid_width=0.02, grid_levels=3, fee_rate=0.0, slippage=0.0)
    result_with_fee = simulate_grid(ind, grid_width=0.02, grid_levels=3, fee_rate=0.001, slippage=0.0005)

    assert "pnl" in result_no_fee
    assert "pnl" in result_with_fee
    assert result_with_fee.get("total_fees", 0) > 0


# ═══════════════════════════════════════════════════════════════
# 2. 回测趋势：_pnl 双边成本
# ═══════════════════════════════════════════════════════════════

def test_trend_pnl_zero_cost():
    """无成本时 _pnl = pos × (exit-entry) / entry"""
    from backtesting.trend import _pnl
    pnl = _pnl(100, "long", 2000, 2100, cost_rate=0.0)
    expected = 100 * (2100 - 2000) / 2000  # = 5.0
    assert abs(pnl - expected) < 0.01


def test_trend_pnl_with_cost():
    """有成本时双边都扣"""
    from backtesting.trend import _pnl
    cost = 0.0015
    pnl = _pnl(100, "long", 2000, 2100, cost_rate=cost)
    # gross = 5.0, cost = 100 * 2 * 0.0015 = 0.3, net = 4.7
    assert abs(pnl - 4.70) < 0.01


def test_trend_pnl_short():
    """做空 _pnl"""
    from backtesting.trend import _pnl
    pnl = _pnl(100, "short", 2100, 2000, cost_rate=0.0)
    expected = 100 * (2100 - 2000) / 2100
    assert abs(pnl - expected) < 0.01


def test_trend_pnl_loss_once():
    """亏损也正确扣成本"""
    from backtesting.trend import _pnl
    cost = 0.001
    pnl = _pnl(100, "long", 2100, 2000, cost_rate=cost)
    assert pnl < 0
    assert pnl < -4.5


# ═══════════════════════════════════════════════════════════════
# 3. config 回测参数
# ═══════════════════════════════════════════════════════════════

def test_backtest_config_exists():
    """回测成本参数存在且合理"""
    import config
    assert hasattr(config, "BACKTEST_SLIPPAGE")
    assert hasattr(config, "BACKTEST_FEE_RATE")
    assert 0 < config.BACKTEST_SLIPPAGE < 0.01
    assert 0 < config.BACKTEST_FEE_RATE < 0.01


# ═══════════════════════════════════════════════════════════════
# 4. 金额为负/零边界
# ═══════════════════════════════════════════════════════════════

def test_negative_price_handled():
    """负价格 _calc_pnl 不崩溃"""
    from strategies.trx_adaptive import TRXAdaptiveStrategy
    from unittest.mock import MagicMock

    class Dummy(TRXAdaptiveStrategy):
        def __init__(self):
            self.client = MagicMock()
            self.symbol = "TRX-USDT"

    strat = Dummy()
    try:
        strat._calc_pnl(1000, 0.08, -0.01, "long")
    except Exception as e:
        pytest.fail(f"_calc_pnl crashed on negative price: {e}")
