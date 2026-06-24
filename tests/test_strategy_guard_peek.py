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


# ── 人工禁用：永久，两种模式都只读返回（不参与冷却） ─────────────
def test_manual_disabled_is_readonly_both_modes():
    rec = {"reason": "manual", "manual": True, "since": PAST}
    for pk in (True, False):
        sg = _sg({"disabled": {KEY: rec}})
        r = sg.check("SOL", "trend", peek=pk)
        assert r["mode"] == "disabled"
        assert sg._db.writes == 0


# ── 自动禁用：冷却内仍禁用；冷却到期 → 自动转试运行 ───────────────
def test_auto_disabled_within_cooldown_stays_disabled():
    rec = {"reason": "试运行结束评分27<40", "manual": False, "since": PAST}  # 刚禁用
    for pk in (True, False):
        sg = _sg({"disabled": {KEY: rec}})
        r = sg.check("SOL", "trend", peek=pk)
        assert r["mode"] == "disabled"
        assert sg._db.writes == 0  # 冷却内不推进

def test_auto_disabled_cooldown_expired_enters_probation():
    from strategy_guard import DISABLE_COOLDOWN_HOURS
    old = (datetime.now(timezone.utc) - timedelta(hours=DISABLE_COOLDOWN_HOURS + 1)).isoformat()
    rec = {"reason": "连续3天评分<40", "manual": False, "since": old}
    # peek：评估为可恢复(probation)，但不写库、不真的恢复
    sg = _sg({"disabled": {KEY: rec}})
    r = sg.check("SOL", "trend", peek=True)
    assert r["mode"] == "probation"
    assert sg._db.writes == 0
    assert KEY in sg._db.data["disabled"]
    # non-peek：真的转入试运行
    sg = _sg({"disabled": {KEY: rec}})
    r = sg.check("SOL", "trend", peek=False)
    assert r["mode"] == "probation"
    assert KEY not in sg._db.data.get("disabled", {})
    assert KEY in sg._db.data["probation_until"]

def test_legacy_string_disabled_lazy_migrates_nonpeek_only():
    # 旧格式(字符串)：peek 只读保持禁用；non-peek 懒迁移盖时间戳但仍禁用
    sg = _sg({"disabled": {KEY: "试运行结束评分27<40，永久禁用"}})
    r = sg.check("SOL", "trend", peek=True)
    assert r["mode"] == "disabled"
    assert sg._db.writes == 0

    sg = _sg({"disabled": {KEY: "试运行结束评分27<40，永久禁用"}})
    r = sg.check("SOL", "trend", peek=False)
    assert r["mode"] == "disabled"            # 迁移当次仍禁用
    assert sg._db.data["disabled"][KEY]["since"] is not None  # 冷却开始计时


# ── P2-2：试运行到期无新评分 → 顺延，不拿过期评分裁决 ─────────────
def test_probation_expired_no_new_score_extends_not_disable():
    # 进入试运行时评分=30，到期时评分仍是30(未更新) → 不应禁用，应顺延
    sg = _sg({"probation_until": {KEY: PAST},
              "last_score": {KEY: 30},
              "probation_entry": {KEY: 30}})
    r = sg.check("SOL", "trend", peek=False)
    assert r["mode"] == "probation"               # 顺延，仍在试运行
    assert "disabled" not in sg._db.data          # 没有用过期低分禁用
    assert KEY in sg._db.data["probation_until"]  # 试运行被续期

def test_probation_expired_with_new_low_score_disables():
    # 进入时基准50，到期时新评分30(已更新且<40) → 正常裁决为禁用
    sg = _sg({"probation_until": {KEY: PAST},
              "last_score": {KEY: 30},
              "probation_entry": {KEY: 50}})
    r = sg.check("SOL", "trend", peek=False)
    assert r["mode"] == "disabled"
    assert KEY in sg._db.data["disabled"]

def test_probation_expired_no_new_score_peek_no_write():
    sg = _sg({"probation_until": {KEY: PAST},
              "last_score": {KEY: 30},
              "probation_entry": {KEY: 30}})
    r = sg.check("SOL", "trend", peek=True)
    assert r["mode"] == "probation"
    assert sg._db.writes == 0
