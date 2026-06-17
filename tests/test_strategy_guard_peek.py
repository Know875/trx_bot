"""
回归测试：strategy_guard.check(peek=True) 只读、不推进状态机（P1-4）。

确保「看一眼面板 / 写参 gate / 孤儿检测」调用 check 时不会把策略推进
试运行/暂停/禁用——状态机推进只应由主交易循环 (peek=False) 驱动。

用 FakeDB 隔离，不触碰真实 bot_state.db。
"""
import copy
from datetime import datetime, timezone, timedelta

from strategy_guard import StrategyGuard


class FakeDB:
    """字典支撑的桩，模拟 StrategyStateDB：get_json 返回副本，set_json 计数。"""
    def __init__(self, data=None):
        self.data = data or {}
        self.writes = 0

    def get_json(self, key, default):
        return copy.deepcopy(self.data.get(key, default))

    def set_json(self, key, value):
        self.writes += 1
        self.data[key] = value


def _sg(data):
    sg = StrategyGuard.__new__(StrategyGuard)  # 跳过 __init__，不建真实 DB
    sg._db = FakeDB(data)
    return sg


KEY = "SOL:trend"
PAST = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()


# ── 暂停到期：peek 不应推进到试运行 ───────────────────────────────
def test_peek_paused_expired_no_write():
    sg = _sg({"paused_until": {KEY: PAST}})
    r = sg.check("SOL", "trend", peek=True)
    assert r["mode"] == "probation"            # 评估结果正确
    assert sg._db.writes == 0                  # 但没写库
    assert "probation_until" not in sg._db.data  # 没真的进试运行
    assert KEY in sg._db.data["paused_until"]    # 暂停记录仍在

def test_nonpeek_paused_expired_advances():
    sg = _sg({"paused_until": {KEY: PAST}})
    r = sg.check("SOL", "trend", peek=False)
    assert r["mode"] == "probation"
    assert sg._db.writes > 0                     # 写了库
    assert KEY in sg._db.data["probation_until"] # 真的进了试运行
    assert KEY not in sg._db.data["paused_until"] # 暂停记录被清


# ── 低评分：peek 不应写入暂停 ─────────────────────────────────────
def test_peek_low_score_no_pause_write():
    sg = _sg({"last_score": {KEY: 20}})  # <25 → 应暂停72h
    r = sg.check("SOL", "trend", peek=True)
    assert r["mode"] == "paused"
    assert sg._db.writes == 0
    assert "paused_until" not in sg._db.data

def test_nonpeek_low_score_writes_pause():
    sg = _sg({"last_score": {KEY: 20}})
    r = sg.check("SOL", "trend", peek=False)
    assert r["mode"] == "paused"
    assert sg._db.writes > 0
    assert KEY in sg._db.data["paused_until"]


# ── 试运行到期 + 低分：peek 不应永久禁用 ─────────────────────────
def test_peek_probation_fail_no_disable_write():
    sg = _sg({"probation_until": {KEY: PAST}, "last_score": {KEY: 30}})  # <40
    r = sg.check("SOL", "trend", peek=True)
    assert r["mode"] == "disabled"        # 评估结论是会被禁用
    assert sg._db.writes == 0             # 但 peek 不真的禁用
    assert "disabled" not in sg._db.data

def test_nonpeek_probation_fail_disables():
    sg = _sg({"probation_until": {KEY: PAST}, "last_score": {KEY: 30}})
    r = sg.check("SOL", "trend", peek=False)
    assert r["mode"] == "disabled"
    assert KEY in sg._db.data["disabled"]


# ── 已禁用：两种模式都只读返回 ───────────────────────────────────
def test_disabled_is_readonly_both_modes():
    for pk in (True, False):
        sg = _sg({"disabled": {KEY: "manual"}})
        r = sg.check("SOL", "trend", peek=pk)
        assert r["mode"] == "disabled"
        assert sg._db.writes == 0
