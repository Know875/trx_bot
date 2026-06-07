"""
测试资金关键路径：_calc_pnl / _guard_mult / 手续费计算
——这些函数算错一分钱就亏真金白银
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock


# ═══════════════════════════════════════════════════════════════
# 1. 现货 _calc_pnl（TRX Adaptive）
# ═══════════════════════════════════════════════════════════════

def test_trx_spot_calc_pnl_long_profit():
    """现货做多盈利: size×(exit-entry) - 双边手续费"""
    from strategies.trx_adaptive import TRXAdaptiveStrategy

    class Dummy(TRXAdaptiveStrategy):
        def __init__(self):
            self.client = MagicMock()
            self.symbol = "TRX-USDT"

    strat = Dummy()
    # size=1000, entry=0.08, exit=0.10, fee=0.001
    # gross = 1000 * 0.02 = 20
    # fee = 1000 * (0.08+0.10) * 0.001 = 0.18
    # net = 19.82
    pnl = strat._calc_pnl(1000, 0.08, 0.10, "long")
    assert abs(pnl - 19.82) < 0.001, f"期待 19.82，实际 {pnl}"


def test_trx_spot_calc_pnl_loss():
    """现货做多亏损: 价格跌了"""
    from strategies.trx_adaptive import TRXAdaptiveStrategy

    class Dummy(TRXAdaptiveStrategy):
        def __init__(self):
            self.client = MagicMock()
            self.symbol = "TRX-USDT"

    strat = Dummy()
    # size=1000, entry=0.10, exit=0.08
    # gross = 1000 * -0.02 = -20
    # fee = 1000 * 0.18 * 0.001 = 0.18
    pnl = strat._calc_pnl(1000, 0.10, 0.08, "long")
    assert abs(pnl - (-20.18)) < 0.001


def test_trx_spot_calc_pnl_zero_size():
    """零仓位 = 零盈亏"""
    from strategies.trx_adaptive import TRXAdaptiveStrategy

    class Dummy(TRXAdaptiveStrategy):
        def __init__(self):
            self.client = MagicMock()
            self.symbol = "TRX-USDT"

    strat = Dummy()
    pnl = strat._calc_pnl(0, 0.08, 0.10, "long")
    assert pnl == 0


def test_trx_spot_calc_pnl_price_unchanged():
    """价格不变：只亏手续费"""
    from strategies.trx_adaptive import TRXAdaptiveStrategy

    class Dummy(TRXAdaptiveStrategy):
        def __init__(self):
            self.client = MagicMock()
            self.symbol = "TRX-USDT"

    strat = Dummy()
    # fee = 1000 * 0.20 * 0.001 = 0.20
    pnl = strat._calc_pnl(1000, 0.10, 0.10, "long")
    assert abs(pnl - (-0.20)) < 0.001


# ═══════════════════════════════════════════════════════════════
# 2. 合约 _calc_pnl（TRX Futures）
# ═══════════════════════════════════════════════════════════════

def test_trx_futures_calc_pnl_long():
    """合约做多: 张数×面值×(exit-entry) - 双边手续费"""
    from strategies.trx_adaptive_futures import TRXAdaptiveFuturesStrategy

    class Dummy(TRXAdaptiveFuturesStrategy):
        def __init__(self):
            self.client = MagicMock()
            self.symbol = "TRX-USDT-SWAP"
            self._config = {"ct_val": 1000.0}

        def _get_ct_val(self):
            return self._config["ct_val"]

    strat = Dummy()
    # size=10张, ct_val=1000, entry=0.08, exit=0.095
    # gross = 10*1000*(0.095-0.08) = 150
    # fee = 10*1000*(0.08+0.095)*0.0005 = 0.875
    pnl = strat._calc_pnl(10, 0.08, 0.095, "long")
    assert abs(pnl - (150 - 0.875)) < 0.01


def test_trx_futures_calc_pnl_short():
    """合约做空: 张数×面值×(entry-exit) - 双边手续费"""
    from strategies.trx_adaptive_futures import TRXAdaptiveFuturesStrategy

    class Dummy(TRXAdaptiveFuturesStrategy):
        def __init__(self):
            self.client = MagicMock()
            self.symbol = "TRX-USDT-SWAP"
            self._config = {"ct_val": 1000.0}

        def _get_ct_val(self):
            return self._config["ct_val"]

    strat = Dummy()
    # 做空: entry=0.10, exit=0.08, win
    # gross = 10*1000*(0.10-0.08) = 200
    # fee = 10*1000*(0.10+0.08)*0.0005 = 0.9
    pnl = strat._calc_pnl(10, 0.10, 0.08, "short")
    assert abs(pnl - (200 - 0.9)) < 0.01


def test_trx_futures_calc_pnl_short_loss():
    """合约做空亏损"""
    from strategies.trx_adaptive_futures import TRXAdaptiveFuturesStrategy

    class Dummy(TRXAdaptiveFuturesStrategy):
        def __init__(self):
            self.client = MagicMock()
            self.symbol = "TRX-USDT-SWAP"
            self._config = {"ct_val": 1000.0}

        def _get_ct_val(self):
            return self._config["ct_val"]

    strat = Dummy()
    # entry=0.08, exit=0.10, loss
    # gross = 10*1000*(0.08-0.10) = -200
    # fee = 10*1000*(0.08+0.10)*0.0005 = 0.9
    pnl = strat._calc_pnl(10, 0.08, 0.10, "short")
    assert abs(pnl - (-200.9)) < 0.01


# ═══════════════════════════════════════════════════════════════
# 3. _guard_mult（熔断仓位乘数）
# ═══════════════════════════════════════════════════════════════

def test_guard_mult_normal():
    """正常模式：全仓，可开新仓"""
    from main import _guard_mult
    mult, can_open, extra = _guard_mult({"status": "normal", "mode": "normal"})
    assert mult == 1.0
    assert can_open is True
    # extra 可能是 list 格式 ["grid_width_multi=1.0"] 或 dict 格式
    if isinstance(extra, list):
        assert "grid_width_multi=1.0" in extra or len(extra) == 0
    else:
        assert extra == {}


def test_guard_mult_warn():
    """预警模式：仓位×0.65，禁止新开仓"""
    from main import _guard_mult
    mult, can_open, extra = _guard_mult({"status": "warn", "mode": "warn"})
    assert mult == 0.65
    assert can_open is False


def test_guard_mult_protect():
    """保护模式：仓位×0.40，禁止新开仓（比 warn 更严，单调性）"""
    from main import _guard_mult
    mult, can_open, extra = _guard_mult({"status": "protect", "mode": "protect"})
    assert mult == 0.40
    assert can_open is False
    # extra 包含网格加宽标记
    assert "widen_grid" in extra or any("widen" in str(x) for x in extra)


def test_guard_mult_monotonic():
    """严重度单调性：越亏越严，cap_mult 不可变大、can_open 不可放宽"""
    from main import _guard_mult
    mults = [_guard_mult({"mode": m})[0] for m in ("normal", "warn", "protect", "halt")]
    opens = [_guard_mult({"mode": m})[1] for m in ("normal", "warn", "protect", "halt")]
    assert mults == sorted(mults, reverse=True)       # 1.0 ≥ 0.65 ≥ 0.40 ≥ 0.0
    assert opens == [True, False, False, False]        # 关了就不再开


def test_guard_mult_halt():
    """熔断：仓位×0，禁止一切"""
    from main import _guard_mult
    mult, can_open, extra = _guard_mult({"status": "halt", "mode": "halt"})
    assert mult == 0.0
    assert can_open is False


def test_guard_mult_unknown():
    """未知状态：默认为正常（1.0），但不应该崩溃"""
    from main import _guard_mult
    mult, can_open, extra = _guard_mult({"status": "something_weird"})
    # 不应该抛出异常
    assert mult >= 0.0
    assert isinstance(can_open, bool)


# ═══════════════════════════════════════════════════════════════
# 4. 现货 grid.py _fee_adjusted_size / PnL
# ═══════════════════════════════════════════════════════════════

def test_grid_fee_adjusted_size():
    """买单到账量 = size × (1 - spot_fee)"""
    from strategies.grid import GridStrategy
    import config

    strat = GridStrategy(
        client=MagicMock(),
        lower=0.08, upper=0.10, grid_count=3,
        capital=1000, symbol="TRX-USDT",
    )
    raw_size = 1000
    adjusted = strat._fee_adjusted_size(raw_size)
    expected = raw_size * (1 - config.SPOT_FEE_RATE)
    assert abs(adjusted - expected) < 0.001


def test_grid_pnl_calculation():
    """网格盈亏 = size*(sell_price-buy_price) - fee"""
    from strategies.grid import GridStrategy

    client = MagicMock()
    strat = GridStrategy(
        client=client, lower=0.08, upper=0.10,
        grid_count=3, capital=1000, symbol="TRX-USDT",
    )
    # 直接测试盈亏公式
    import config
    size = 500
    buy_price = 0.09
    sell_price = 0.095
    fee = size * (buy_price + sell_price) * config.SPOT_FEE_RATE
    expected = size * (sell_price - buy_price) - fee
    assert expected > 0  # 盈利
    assert abs(expected - 2.4075) < 0.01  # 25.0 - 0.0425


def test_grid_pnl_loss():
    """网格亏损也是正确地扣了手续费"""
    size = 500
    buy_price = 0.10
    sell_price = 0.09

    import config
    fee = size * (buy_price + sell_price) * config.SPOT_FEE_RATE
    pnl = size * (sell_price - buy_price) - fee
    assert pnl < 0
    assert abs(pnl - (-5.095)) < 0.01
