"""
回归测试：main._price_in_grid_band —— 带感知停滞重组的判定。

价格仍在网格带内(行情清淡) → True(不重组)；漂出带 → False(重组)；
无法判定网格边界 → None(回退到原时间触发逻辑)。

main.py 依赖 okx/pandas 等重模块，此处用 sys.modules 桩隔离后再导入。
"""
import sys
import types
from unittest import mock

# ── 用 MagicMock 桩掉 main 顶部的重依赖（okx/pandas 等本机不可用）──
_HEAVY = [
    "okx_client", "market_analyzer", "tracker", "account_guard",
    "strategy_report", "notify", "ai_advisor", "macro", "volatility_adapter",
    "strategies", "strategies.grid", "strategies.trend", "strategies.futures_trend",
    "strategies.futures_grid", "strategies.trx_adaptive", "strategies.trx_adaptive_futures",
    "okx", "okx.Trade", "okx.Account", "okx.MarketData", "okx.PublicData",
]
for _m in _HEAVY:
    sys.modules.setdefault(_m, mock.MagicMock())

import main  # noqa: E402


class _WithBounds:
    """grid / futures_grid：暴露 lower / upper。"""
    def __init__(self, lower, upper):
        self.lower, self.upper = lower, upper


class _Adaptive:
    """base_adaptive：暴露 _grid_prices 列表。"""
    def __init__(self, prices):
        self._grid_prices = prices


def test_price_inside_band_with_lower_upper():
    s = _WithBounds(0.30, 0.34)
    assert main._price_in_grid_band(s, 0.32) is True
    assert main._price_in_grid_band(s, 0.30) is True   # 边界含
    assert main._price_in_grid_band(s, 0.34) is True

def test_price_outside_band_with_lower_upper():
    s = _WithBounds(0.30, 0.34)
    assert main._price_in_grid_band(s, 0.29) is False   # 跌破
    assert main._price_in_grid_band(s, 0.35) is False   # 涨破

def test_adaptive_grid_prices_band():
    s = _Adaptive([1.90, 1.95, 2.00, 2.05, 2.10])
    assert main._price_in_grid_band(s, 2.00) is True
    assert main._price_in_grid_band(s, 1.80) is False
    assert main._price_in_grid_band(s, 2.20) is False

def test_unknown_bounds_returns_none():
    assert main._price_in_grid_band(object(), 1.0) is None      # 无 lower/upper/_grid_prices
    assert main._price_in_grid_band(_Adaptive([]), 1.0) is None  # 空网格
    assert main._price_in_grid_band(None, 1.0) is None
    assert main._price_in_grid_band(_WithBounds(1, 2), None) is None


# ── _timeit / _phase_breakdown ──────────────────────────────────
def test_timeit_accumulates_and_returns():
    store = {}
    out = main._timeit(store, "x", lambda a, b: a + b, 2, 3)
    assert out == 5
    main._timeit(store, "x", lambda: None)
    assert "x" in store and store["x"] >= 0      # 同名累计

def test_timeit_records_even_on_exception():
    store = {}
    def boom():
        raise ValueError("boom")
    try:
        main._timeit(store, "y", boom)
    except ValueError:
        pass
    assert "y" in store                          # finally 仍记账

def test_phase_breakdown_sorts_and_filters():
    s = main._phase_breakdown({"a": 3.0, "b": 0.05, "c": 1.0})
    assert s.startswith("a=3.0s")                # 降序
    assert "b=" not in s                         # <0.1s 过滤
    assert main._phase_breakdown({}).startswith("各阶段均<0.1s")
