"""
测试 AI 安全约束：只降不升
"""
import pytest
from unittest.mock import MagicMock


def test_safe_ai_full_to_full():
    """AI same level → no downgrade"""
    from ai_advisor import safe_ai_advice
    result = safe_ai_advice(
        baseline_mult=1.0,
        ai_advice={"entry_advice": "full", "regime": "trending_up", "warning_flags": [], "confidence": 0.8},
    )
    assert result["effective_mult"] == 1.0
    assert result["downgraded"] is False
    assert result["ai_entry"] == "full"


def test_safe_ai_full_to_half():
    """AI says half → clamp down"""
    from ai_advisor import safe_ai_advice
    result = safe_ai_advice(
        baseline_mult=1.0,
        ai_advice={"entry_advice": "half", "regime": "ranging", "warning_flags": ["高波动"], "confidence": 0.7},
    )
    assert result["effective_mult"] == 0.6
    assert result["downgraded"] is True
    assert "高波动" in result["warnings"]


def test_safe_ai_full_to_skip():
    """AI says skip → zero position"""
    from ai_advisor import safe_ai_advice
    result = safe_ai_advice(
        baseline_mult=1.0,
        ai_advice={"entry_advice": "skip", "regime": "wait", "warning_flags": ["市场恐慌"], "confidence": 0.9},
    )
    assert result["effective_mult"] == 0.0
    assert result["downgraded"] is True


def test_safe_ai_cannot_increase():
    """AI can NEVER raise position above baseline"""
    from ai_advisor import safe_ai_advice
    # baseline=0.5, AI says full → still 0.5
    result = safe_ai_advice(
        baseline_mult=0.5,
        ai_advice={"entry_advice": "full", "regime": "trending_up", "warning_flags": [], "confidence": 0.8},
    )
    assert result["effective_mult"] == 0.5
    assert result["downgraded"] is False  # didn't downgrade, but also didn't upgrade


def test_safe_ai_half_to_half():
    """Both half → no change"""
    from ai_advisor import safe_ai_advice
    result = safe_ai_advice(
        baseline_mult=0.6,
        ai_advice={"entry_advice": "half", "regime": "trending_up", "warning_flags": [], "confidence": 0.75},
    )
    assert result["effective_mult"] == 0.6


def test_safe_ai_half_to_skip():
    """Half → skip is allowed (further reduce)"""
    from ai_advisor import safe_ai_advice
    result = safe_ai_advice(
        baseline_mult=0.6,
        ai_advice={"entry_advice": "skip", "regime": "wait", "warning_flags": ["假突破"], "confidence": 0.8},
    )
    assert result["effective_mult"] == 0.0


def test_safe_ai_none():
    """AI failure → no change, no error"""
    from ai_advisor import safe_ai_advice
    result = safe_ai_advice(baseline_mult=0.8, ai_advice=None)
    assert result["effective_mult"] == 0.8
    assert result["downgraded"] is False
    assert result["ai_entry"] is None


def test_safe_ai_garbage_input():
    """Malformed AI response → safe fallback"""
    from ai_advisor import safe_ai_advice
    result = safe_ai_advice(baseline_mult=0.7, ai_advice={})
    assert result["effective_mult"] == 0.6  # defaults to half: min(0.7, 0.6)
    result2 = safe_ai_advice(baseline_mult=0.7, ai_advice=42)
    assert result2["effective_mult"] == 0.7  # not a dict → no change


def test_safe_ai_unknown_entry():
    """Unknown entry_advice → half (0.6)"""
    from ai_advisor import safe_ai_advice
    result = safe_ai_advice(
        baseline_mult=1.0,
        ai_advice={"entry_advice": "whatever", "regime": "trending_up", "warning_flags": [], "confidence": 0.8},
    )
    assert result["effective_mult"] == 0.6  # unknown → half
