"""
资金费率套利执行器 — 监控费率、自动开仓/平仓

用法:
  python carry_executor.py                # 单次扫描+执行
  python carry_executor.py --watch 3600   # 持续监控
"""

import sys
import os
import time
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from okx_client import OKXClient
from carry import fetch_funding_rate, carry_analysis, annualize_rate
from notify import send_tg

logger = logging.getLogger("carry_exec")

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

# 自动做 carry 的币种（需要同时有现货和合约）
AUTO_CARRY_COINS = {
    "TRX":  {"spot_size": 1000, "ct_val": 1000.0, "min_annual": 12.0},  # 年化>12%
    "ETH":  {"spot_size": 1500, "ct_val": 0.01,   "min_annual": 10.0},  # 年化>10%
    "SOL":  {"spot_size": 1000, "ct_val": 1.0,    "min_annual": 15.0},  # 年化>15%（波动大）
}

# 平仓条件
EXIT_ANNUAL_PCT = 3.0   # 年化<3%时平仓
MIN_HOLD_HOURS   = 24    # 最少持有时长（避免频繁进出）

# 仓位文件
POSITION_FILE = Path(__file__).parent / ".carry_positions.json"


class CarryExecutor:
    """自动执行资金费率套利"""

    def __init__(self):
        self._positions = self._load_positions()

    def _load_positions(self):
        if POSITION_FILE.exists():
            try:
                return json.loads(POSITION_FILE.read_text())
            except Exception:
                pass
        return {}

    def _save_positions(self):
        POSITION_FILE.write_text(json.dumps(self._positions, indent=2, default=str))

    def scan_and_act(self) -> dict:
        """主循环：扫描所有币种 → 决策开仓/平仓"""
        result = {"scanned": [], "opened": [], "closed": [], "holding": []}
        now = datetime.now(timezone.utc)

        for coin, cfg in AUTO_CARRY_COINS.items():
            symbol = f"{coin}-USDT-SWAP"
            spot_symbol = f"{coin}-USDT"

            # 获取费率
            fr_data = fetch_funding_rate(symbol)
            if not fr_data:
                result["scanned"].append({coin: "fetch_failed"})
                continue

            funding_rate = fr_data["funding_rate"]
            analysis = carry_analysis(symbol, funding_rate, cfg["ct_val"], cfg["spot_size"])
            annual_net = analysis["annual_net_pct"]
            result["scanned"].append({coin: round(annual_net, 2)})

            # ── 决策 ──
            if coin in self._positions:
                # 已有仓位 → 检查是否该平
                pos = self._positions[coin]
                hold_hours = (now - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600

                if annual_net < EXIT_ANNUAL_PCT and hold_hours >= MIN_HOLD_HOURS:
                    if self._close_carry(coin, pos):
                        result["closed"].append(coin)
                        logger.info(f"✅ {coin} carry平仓: 年化降至{annual_net:.1f}%")
                        send_tg(f"📤 [{coin}] Carry 平仓 — 年化降至 {annual_net:.1f}%")
                        del self._positions[coin]
                        self._save_positions()
                else:
                    result["holding"].append({
                        coin: f"年化{annual_net:.1f}%, 持{hold_hours:.0f}h"
                    })
            else:
                # 无仓位 → 检查是否该开
                if annual_net >= cfg["min_annual"] and funding_rate > 0:
                    if self._open_carry(coin, cfg, annual_net):
                        self._positions[coin] = {
                            "opened_at": now.isoformat(),
                            "spot_size": cfg["spot_size"],
                            "entry_annual": annual_net,
                            "entry_rate": funding_rate,
                        }
                        self._save_positions()
                        result["opened"].append(coin)
                        logger.info(f"🚀 {coin} carry开仓: 年化{annual_net:.1f}%, size={cfg['spot_size']}USDT")
                        send_tg(
                            f"📥 [{coin}] Carry 开仓\n"
                            f"费率: {funding_rate*100:.4f}%\n"
                            f"年化: {annual_net:.1f}%\n"
                            f"本金: {cfg['spot_size']} USDT"
                        )

        return result

    def _open_carry(self, coin, cfg, annual_net) -> bool:
        """开仓: 现货买入 + 永续做空 等额"""
        spot_symbol = f"{coin}-USDT"
        swap_symbol = f"{coin}-USDT-SWAP"

        try:
            spot_client = OKXClient(symbol=spot_symbol)
            swap_client = OKXClient(symbol=swap_symbol, flag="1")  # flag=1 实盘合约

            # 获取价格
            spot_ticker = spot_client.get_ticker()
            price = float(spot_ticker["last"])
            spot_size = cfg["spot_size"] / price

            # 现货买入
            spot_client.place_order("buy", price, spot_size)
            logger.info(f"[{coin}] 现货买入 {spot_size:.4f} @ {price}")

            # 永续做空（等额）
            ct_val = cfg["ct_val"]
            contracts = max(1, int(cfg["spot_size"] / ct_val / price))
            swap_client.place_order("sell", price, contracts)
            logger.info(f"[{coin}] 永续做空 {contracts}张 @ {price}")

            return True
        except Exception as e:
            logger.error(f"[{coin}] carry开仓失败: {e}")
            send_tg(f"❌ [{coin}] Carry 开仓失败: {e}")
            return False

    def _close_carry(self, coin, pos) -> bool:
        """平仓: 现货卖出 + 永续平空"""
        spot_symbol = f"{coin}-USDT"
        swap_symbol = f"{coin}-USDT-SWAP"

        try:
            spot_client = OKXClient(symbol=spot_symbol)
            swap_client = OKXClient(symbol=swap_symbol, flag="1")

            price = float(spot_client.get_ticker()["last"])
            spot_size = pos["spot_size"] / price

            # 现货卖出
            spot_client.place_order("sell", price, spot_size)
            # 永续平空
            swap_positions = swap_client.get_positions()
            if swap_positions:
                for p in swap_positions:
                    if p.get("instId") == swap_symbol:
                        swap_client.place_order("buy", price, abs(float(p.get("pos", 0))))

            return True
        except Exception as e:
            logger.error(f"[{coin}] carry平仓失败: {e}")
            send_tg(f"❌ [{coin}] Carry 平仓失败: {e}")
            return False

    def status(self) -> dict:
        return {
            "positions": self._positions,
            "count": len(self._positions),
        }


def run_once():
    """单次执行（给 cron/watchdog 调用）"""
    executor = CarryExecutor()
    result = executor.scan_and_act()
    return result


def run_watch(interval_s: int = 3600):
    """持续监控"""
    executor = CarryExecutor()
    logger.info(f"资金费率套利监控启动，间隔 {interval_s}s")
    while True:
        try:
            result = executor.scan_and_act()
            summary = (
                f"扫描{len(result['scanned'])} | "
                f"新开{len(result['opened'])} | "
                f"平仓{len(result['closed'])} | "
                f"持有{len(result['holding'])}"
            )
            logger.info(f"[carry] {summary}")
        except Exception as e:
            logger.error(f"[carry] 异常: {e}")
        time.sleep(interval_s)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--watch", type=int, default=0, help="持续监控，间隔秒数")
    p.add_argument("--status", action="store_true", help="查看当前持仓")
    args = p.parse_args()

    if args.status:
        executor = CarryExecutor()
        print(json.dumps(executor.status(), indent=2))
    elif args.watch:
        run_watch(args.watch)
    else:
        result = run_once()
        print(json.dumps(result, indent=2, default=str))
