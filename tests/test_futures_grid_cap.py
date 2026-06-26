"""
回归测试：FuturesGridStrategy 仓位上限不再静默失效。

历史 bug：构造点传入的是币种键 (symbol="ETH_SWAP")，但 _coin 解析只反查
交易对符号 (COIN_CONFIG[*].symbol == "ETH-USDT-SWAP")，两者永不相等 →
self._coin 恒为空 → 所有 `if self._coin:` 守卫的 SWAP_POSITION_CAP_PCT
仓位上限成了死代码。本测试锁定 _coin 解析与 start() 的封顶行为。

futures_grid 仅依赖 config + notify（均为标准库），可直接导入；client 用桩。
"""
import sys
import importlib.util
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import config  # noqa: E402

# 直接从文件加载真实模块，绕开其它测试（如 test_grid_band）在 sys.modules 里
# 留下的 MagicMock 桩——否则按收集顺序本测试可能拿到桩类而非真身。
_spec = importlib.util.spec_from_file_location(
    "_real_futures_grid", _ROOT / "strategies" / "futures_grid.py")
_fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fg)
FuturesGridStrategy = _fg.FuturesGridStrategy


def _make(symbol):
    client = mock.MagicMock()
    # 构造期只调用纯数学的 _validate/_build_grid，不触网
    return FuturesGridStrategy(
        client=client, lower=2900.0, upper=3100.0,
        grid_count=4, capital=9000.0, symbol=symbol,
    )


def test_coin_resolved_from_coin_key():
    """main.py 实际入参：symbol=ccy=币种键 → _coin 必须等于该键。"""
    for key in ("TRX_SWAP", "ETH_SWAP", "SOL_SWAP"):
        s = _make(key)
        assert s._coin == key, f"{key}: _coin 解析为 {s._coin!r}，仓位上限会失效"


def test_coin_resolved_from_trading_symbol():
    """兼容另一种入参：交易对符号 → 反查出币种键。"""
    s = _make("ETH-USDT-SWAP")
    assert s._coin == "ETH_SWAP"


def test_position_cap_blocks_buys_when_over_limit():
    """持仓市值超 SWAP_POSITION_CAP_PCT×70% 时，start() 不挂任何买单。"""
    s = _make("ETH_SWAP")
    cap_pct = config.SWAP_POSITION_CAP_PCT["ETH_SWAP"]          # 0.5
    cap_value = cap_pct * config.COIN_CONFIG["ETH_SWAP"]["initial_capital"]
    # 构造一个 pos_val 远超 70% 阈值的持仓（code 用 |pos| * markPx 估值）
    over = cap_value  # 远大于 cap_value*0.7
    s.client.get_futures_position.return_value = {"pos": "1", "markPx": str(over)}

    s.start(current_price=3000.0)

    assert s.buy_orders == {}, "超仓位上限时不应再挂买单"
    s.client.place_futures_order.assert_not_called()


def test_position_cap_allows_buys_when_under_limit():
    """无持仓时正常挂买单（封顶逻辑不误伤）。"""
    s = _make("ETH_SWAP")
    s.client.get_futures_position.return_value = {}   # 无持仓
    s.client.get_ct_val.return_value = 0.01
    s.client.place_futures_order.return_value = "oid-1"

    s.start(current_price=3000.0)

    assert s.buy_orders, "正常情况下应挂出买单"
    assert s.running is True
