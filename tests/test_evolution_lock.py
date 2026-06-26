"""
测试进化锁核心逻辑：_param_matches / can_evolve / _safe_parse_dt / get_leverage
"""
import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import json
from unittest.mock import patch


# ═══════════════════════════════════════════════════════════════
# 1. _param_matches — 双向参数名匹配
# ═══════════════════════════════════════════════════════════════

def test_param_matches_exact():
    """精确匹配"""
    from evolution_lock import _param_matches
    assert _param_matches("ETH_GRID_COUNT", "ETH_GRID_COUNT", "ETH") is True


def test_param_matches_without_coin_prefix():
    """grid_count 匹配 ETH_GRID_COUNT"""
    from evolution_lock import _param_matches
    assert _param_matches("ETH_GRID_COUNT", "grid_count", "ETH") is True
    assert _param_matches("grid_count", "ETH_GRID_COUNT", "ETH") is True


def test_param_matches_different_coin():
    """不同币种不匹配"""
    from evolution_lock import _param_matches
    assert _param_matches("ETH_GRID_COUNT", "grid_count", "SOL") is False


def test_param_matches_unrelated():
    """完全不相关的参数"""
    from evolution_lock import _param_matches
    assert _param_matches("TREND_RSI_OB", "grid_count", "TRX") is False


def test_param_matches_same_coin_different_param():
    """同一币种不同参数不匹配"""
    from evolution_lock import _param_matches
    assert _param_matches("ETH_GRID_COUNT", "ETH_GRID_RANGE_PCT", "ETH") is False


# ═══════════════════════════════════════════════════════════════
# 2. can_evolve — 多层守卫 (mock 外部依赖)
# ═══════════════════════════════════════════════════════════════

def test_can_evolve_manual_lock():
    """手动锁：禁止一切"""
    import evolution_lock
    lock_file = os.path.join(evolution_lock.ROOT, ".evolution_manual_lock")
    try:
        os.unlink(lock_file)
    except OSError:
        pass
    with open(lock_file, "w") as f:
        f.write("manual")
    try:
        ok, reason = evolution_lock.can_evolve("explore")
        assert ok is False
        assert "manual" in reason.lower()
    finally:
        os.unlink(lock_file)


def test_can_evolve_extreme_mock():
    """极端行情下：探索/写配置被禁止"""
    import evolution_lock
    # 清理手动锁
    lock_file = os.path.join(evolution_lock.ROOT, ".evolution_manual_lock")
    try:
        os.unlink(lock_file)
    except OSError:
        pass

    with patch("brain.is_extreme_market") as mock_extreme:
        mock_extreme.return_value = {
            "is_extreme": True,
            "reasons": ["BTC 24h -10%"]
        }
        ok, reason = evolution_lock.can_evolve("explore")
        assert ok is False, f"explore should be blocked in extreme: {reason}"

        ok, reason = evolution_lock.can_evolve("write_config")
        assert ok is False, f"write_config should be blocked: {reason}"


def test_can_evolve_normal_mock():
    """正常市场：允许探索"""
    import evolution_lock
    lock_file = os.path.join(evolution_lock.ROOT, ".evolution_manual_lock")
    try:
        os.unlink(lock_file)
    except OSError:
        pass

    with patch("brain.is_extreme_market") as mock_extreme:
        mock_extreme.return_value = {"is_extreme": False, "reasons": []}
        ok, reason = evolution_lock.can_evolve("explore")
        assert ok is True, f"explore should be allowed in normal: {reason}"


def test_can_evolve_account_guard_protect():
    """账户 protect 模式 → 禁止进化"""
    import evolution_lock
    lock_file = os.path.join(evolution_lock.ROOT, ".evolution_manual_lock")
    try:
        os.unlink(lock_file)
    except OSError:
        pass

    with patch("brain.is_extreme_market") as mock_extreme:
        mock_extreme.return_value = {"is_extreme": False, "reasons": []}
        with patch("account_guard.Guard.check") as mock_guard:
            mock_guard.return_value = {"status": "protect", "mode": "protect"}
            ok, reason = evolution_lock.can_evolve("explore")
            assert ok is False, f"explore blocked by protect: {reason}"


# ═══════════════════════════════════════════════════════════════
# 3. _safe_parse_dt — 时区安全
# ═══════════════════════════════════════════════════════════════

def test_safe_parse_dt_utc():
    """UTC 时间戳正常解析"""
    from account_guard import _safe_parse_dt
    result = _safe_parse_dt("2026-06-06T18:00:00+00:00")
    assert result is not None
    assert result.tzinfo is not None


def test_safe_parse_dt_naive():
    """无时区时间戳 → 自动加 UTC"""
    from account_guard import _safe_parse_dt
    result = _safe_parse_dt("2026-06-06 18:00:00")
    assert result is not None
    assert result.tzinfo is not None


def test_safe_parse_dt_garbage():
    """垃圾输入 → 返回 None (安全降级)"""
    from account_guard import _safe_parse_dt
    result = _safe_parse_dt("not a date at all")
    assert result is None


def test_safe_parse_dt_none():
    """None 输入 → 返回 None"""
    from account_guard import _safe_parse_dt
    result = _safe_parse_dt(None)
    assert result is None


# ═══════════════════════════════════════════════════════════════
# 4. get_leverage / get_ct_val — 分币种配置
# ═══════════════════════════════════════════════════════════════

def test_get_leverage_per_coin():
    """TRX_SWAP=3, ETH_SWAP=5, SOL_SWAP=5"""
    import config
    assert config.get_leverage("TRX_SWAP") == 3
    assert config.get_leverage("ETH_SWAP") == 5
    assert config.get_leverage("SOL_SWAP") == 5


def test_get_leverage_fallback():
    """未知币种 → 默认杠杆"""
    import config
    assert config.get_leverage("BTC_SWAP") == config.FUTURES_DEFAULT_LEVERAGE
    assert config.get_leverage("TRX") == config.FUTURES_DEFAULT_LEVERAGE


def test_get_ct_val():
    """面值获取"""
    import config
    assert config.get_ct_val("TRX_SWAP") == 1000.0
    assert config.get_ct_val("ETH_SWAP") == 0.01
    assert config.get_ct_val("SOL_SWAP") == 1.0
    assert config.get_ct_val("TRX") == 1.0  # 现货 fallback
