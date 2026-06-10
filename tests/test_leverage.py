"""
测试合约杠杆真正落到交易所 —— 覆盖 okx_client.set_leverage 这条路径。

历史教训（REVIEW.md 2026-06-10）：config.get_leverage 的键是币种键(TRX_SWAP)，
但 set_leverage 默认用的是交易对符号(TRX-USDT-SWAP) → 永远查不到 → 退回默认 5x，
导致 TRX_SWAP 实跑 5x 而非配置 3x。test_get_leverage 只测了 config 函数本身，
没覆盖 set_leverage 路径，所以漏网。这里直接断言「发给交易所的 lever」= 配置值。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# 没装 okx SDK 的环境（如本地）跳过；CI 装了 okx 正常执行
pytest.importorskip("okx")

import config
from okx_client import OKXClient


def _make_client(symbol):
    """绕过 __init__（避免构造真实 OKX API 对象），只装配 set_leverage 所需的属性。
    用假 account 捕获真正发给交易所的 lever。"""
    client = object.__new__(OKXClient)
    client.symbol = symbol
    captured = {}

    class FakeAccount:
        def set_leverage(self, instId, lever, mgnMode):
            captured["instId"] = instId
            captured["lever"] = lever
            captured["mgnMode"] = mgnMode
            return {"code": "0"}

    client.account = FakeAccount()
    return client, captured


@pytest.mark.parametrize("coin_key", [k for k, v in config.COIN_CONFIG.items() if v["mode"] == "futures"])
def test_set_leverage_uses_per_coin_config(coin_key):
    """无参 set_leverage() 发给交易所的 lever 必须等于该币种配置杠杆。"""
    symbol = config.COIN_CONFIG[coin_key]["symbol"]
    expected = str(config.get_leverage(coin_key))
    client, captured = _make_client(symbol)

    client.set_leverage()

    assert captured["instId"] == symbol
    assert captured["lever"] == expected, (
        f"{symbol}: 发给交易所 {captured['lever']}x，应为配置的 {expected}x"
    )


def test_set_leverage_trx_swap_is_3_not_default():
    """回归：TRX_SWAP 必须是 3x，不能退回默认 5x（这正是被修复的 bug）。"""
    client, captured = _make_client("TRX-USDT-SWAP")
    client.set_leverage()
    assert captured["lever"] == "3"
    assert captured["lever"] != str(config.FUTURES_DEFAULT_LEVERAGE)


def test_set_leverage_explicit_arg_overrides():
    """显式传 lever 时按传入值（dashboard 手动设杠杆走这条）。"""
    client, captured = _make_client("ETH-USDT-SWAP")
    client.set_leverage(lever=2)
    assert captured["lever"] == "2"


def test_set_leverage_unknown_symbol_falls_back_to_default():
    """未配置的符号回退默认杠杆，不崩。"""
    client, captured = _make_client("BTC-USDT-SWAP")
    client.set_leverage()
    assert captured["lever"] == str(config.FUTURES_DEFAULT_LEVERAGE)
