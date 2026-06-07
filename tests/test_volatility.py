"""Test volatility_adapter module."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from volatility_adapter import adapt_grid, get_adapted_grid_params, BASELINE_ATR_PCT


class TestAdaptGrid:
    def test_normal_volatility(self):
        """ATR matches baseline → no adjustment."""
        ind = {"atr": 0.015, "atr_pct": 0.015}
        width, count, atr, ratio = adapt_grid("ETH", ind)
        assert 0.98 <= width <= 1.02, f"width={width} should be ~1.0"
        assert 0.98 <= count <= 1.02, f"count={count} should be ~1.0"

    def test_high_volatility(self):
        """High ATR → wider grid, fewer levels."""
        ind = {"atr": 0.03, "atr_pct": 0.03}
        width, count, atr, ratio = adapt_grid("ETH", ind)
        assert width > 1.2, f"width={width} should > 1.2 in high vol"
        assert count < 0.9, f"count={count} should < 0.9 in high vol"

    def test_low_volatility(self):
        """Low ATR → narrower grid, more levels."""
        ind = {"atr": 0.005, "atr_pct": 0.005}
        width, count, atr, ratio = adapt_grid("ETH", ind)
        assert width < 0.9, f"width={width} should < 0.9 in low vol"
        assert count > 1.2, f"count={count} should > 1.2 in low vol"

    def test_extreme_volatility_no_change(self):
        """Extreme vol (>2.5x baseline) → no grid change."""
        ind = {"atr": 0.06, "atr_pct": 0.06}
        width, count, atr, ratio = adapt_grid("ETH", ind)
        assert ratio >= 2.5
        assert width == 1.0
        assert count == 1.0

    def test_zero_atr(self):
        """Zero ATR → defaults."""
        ind = {"atr": 0, "atr_pct": 0}
        width, count, atr, ratio = adapt_grid("TRX", ind)
        assert width == 1.0 and count == 1.0

    def test_unknown_coin(self):
        """Unknown coin uses default baseline."""
        ind = {"atr": 0.02, "atr_pct": 0.02}
        width, count, atr, ratio = adapt_grid("UNKNOWN", ind)
        assert width > 0 and count > 0


class TestAdaptedGridParams:
    def test_returns_valid_params(self):
        ind = {"atr": 0.02, "atr_pct": 0.02}
        gr, gc, vr = get_adapted_grid_params("SOL", 0.05, 3, ind)
        assert gr > 0
        assert gc >= 1
        assert vr > 0

    def test_count_never_zero(self):
        ind = {"atr": 0.01, "atr_pct": 0.01}
        _, gc, _ = get_adapted_grid_params("TRX", 0.05, 1, ind)
        assert gc >= 1

    def test_known_baselines(self):
        assert "TRX" in BASELINE_ATR_PCT
        assert "ETH" in BASELINE_ATR_PCT
        assert "SOL" in BASELINE_ATR_PCT
        assert BASELINE_ATR_PCT["SOL"] > BASELINE_ATR_PCT["TRX"]
