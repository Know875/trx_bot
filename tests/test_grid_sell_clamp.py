"""
回归测试：现货网格卖单超卖修复（P0-1）。

覆盖：
  1. _fee_adjust_sell_size — 现货买入到手已扣手续费，卖单缩量；合约不缩量。
  2. _clamp_sell_size — 卖单数量 clamp 到实际可用余额，超额不再放行全额。
  3. coin 专属 logger — 基类不再用共享 base_adaptive logger（防日志串台）。

注意：策略类不 import okx，可脱离运行环境单测（client 用桩对象）。
"""
from strategies.trx_adaptive import TRXAdaptiveStrategy
from strategies.trx_adaptive_futures import TRXAdaptiveFuturesStrategy


class FakeClient:
    """最小桩：只实现被测路径用到的方法。"""
    def __init__(self, spot_pos=0.0):
        self._spot_pos = spot_pos

    def get_spot_position(self, base):
        return self._spot_pos


def _spot(spot_pos, decimals=0):
    return TRXAdaptiveStrategy(client=FakeClient(spot_pos), capital=5000, size_decimals=decimals)


# ── 1. fee adjust ──────────────────────────────────────────────
def test_fee_adjust_spot_int():
    s = _spot(1000)
    assert s._fee_adjust_sell_size(100) == 99   # floor(100*0.999)=99

def test_fee_adjust_spot_decimals():
    s = _spot(1000, decimals=2)
    # floor(10*0.999*100)/100 = floor(998.99...)/100 → wait 10*0.999=9.99 → 9.99
    assert s._fee_adjust_sell_size(10) == 9.99

def test_fee_adjust_futures_unchanged():
    f = TRXAdaptiveFuturesStrategy(client=FakeClient(), capital=4000)
    assert f._fee_adjust_sell_size(100) == 100   # 合约按张数原样


# ── 2. clamp sell size ─────────────────────────────────────────
def test_clamp_within_balance():
    s = _spot(1000)
    s._sell_orders = {}
    # 想卖 200，余额 1000，足够 → 返回 200（floor 到整数）
    assert s._clamp_sell_size(0.32, 200) == 200

def test_clamp_exceeds_balance_returns_remaining():
    s = _spot(1000)
    # 已挂 900，再想卖 200 → 只剩 100 可卖，应 clamp 到 100（旧 bug：放行 200 超卖）
    s._sell_orders = {0.30: {"order_id": "a", "size": 900}}
    assert s._clamp_sell_size(0.33, 200) == 100

def test_clamp_below_min_returns_zero():
    s = _spot(1000)
    # 已挂 1000，余额耗尽 → 剩 0 < min_order_size(1) → 跳过
    s._sell_orders = {0.30: {"order_id": "a", "size": 1000}}
    assert s._clamp_sell_size(0.33, 200) == 0

def test_clamp_query_failure_keeps_original():
    class Boom:
        def get_spot_position(self, base):
            raise RuntimeError("api down")
    s = TRXAdaptiveStrategy(client=Boom(), capital=5000, size_decimals=0)
    s._sell_orders = {}
    # 查询失败保持旧行为，不误杀
    assert s._clamp_sell_size(0.33, 200) == 200


# ── 3. coin 专属 logger（防串台）────────────────────────────────
def test_logger_is_coin_specific():
    s = _spot(1000)
    f = TRXAdaptiveFuturesStrategy(client=FakeClient(), capital=4000)
    assert s._log.name == "trx_adaptive"
    assert f._log.name == "trx_adaptive_futures"
    # 不再是共享的 base_adaptive
    assert s._log.name != "base_adaptive"
    assert f._log.name != "base_adaptive"
