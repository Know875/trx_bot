"""
资金费率套利执行器 — delta-neutral carry（现货多 + 永续空，吃正费率）。

⚠️ 安全须知（务必读）：
  1. 默认 **dry-run**（只演示决策、不下单）。真实下单需显式 `CARRY_LIVE=1`。
  2. **不要与主 bot (main.py) 同时跑同一批币种**：run_swap_coin 启动清理会平掉这里的
     永续空腿，导致现货裸露。要跑实盘 carry，请用主 bot 不交易的币种，或停掉主 bot 对应币。
  3. 现货腿与永续腿**必须同一环境**（同一个 OKX_FLAG）。本模块两腿统一用 config.FLAG。
  4. 开仓双腿原子化：第二腿失败 → 立即回滚第一腿，绝不留单边裸仓。
  5. 平仓先平永续空（去杠杆）再卖现货：万一中断，最坏只剩现货多头（无杠杆），不留裸空。

用法:
  python carry_executor.py                 # 单次扫描（dry-run）
  python carry_executor.py --watch 3600    # 持续监控（dry-run）
  CARRY_LIVE=1 python carry_executor.py    # 真实下单（务必先看懂上面 1-5）
  python carry_executor.py --status        # 查看当前 carry 持仓
"""

import sys
import os
import time
import json
import math
import logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from okx_client import OKXClient
from carry import fetch_funding_rate, carry_analysis
from notify import send_tg

logger = logging.getLogger("carry_exec")

# ── 是否真实下单（默认否）──
LIVE = os.getenv("CARRY_LIVE", "0") == "1"

# 自动 carry 的币种（需同时有现货和合约）。spot_size 为现货端本金(USDT)。
AUTO_CARRY_COINS = {
    "TRX": {"spot_size": 1000, "min_annual": 12.0},
    "ETH": {"spot_size": 1500, "min_annual": 10.0},
    "SOL": {"spot_size": 1000, "min_annual": 15.0},
}

EXIT_ANNUAL_PCT = 3.0    # 年化 < 3% 平仓
MIN_HOLD_HOURS  = 24     # 最少持有，避免频繁进出
FILL_TOLERANCE  = 0.85   # 现货实际到手 < 预期×此比例 → 视为未成交，放弃并不开空

POSITION_FILE = Path(__file__).parent / ".carry_positions.json"


class CarryExecutor:
    def __init__(self, live: bool = None):
        self.live = LIVE if live is None else live
        self._positions = self._load_positions()

    # ── 持仓记录 ──
    def _load_positions(self):
        if POSITION_FILE.exists():
            try:
                return json.loads(POSITION_FILE.read_text())
            except Exception:
                pass
        return {}

    def _save_positions(self):
        tmp = str(POSITION_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._positions, f, indent=2, default=str)
        os.replace(tmp, str(POSITION_FILE))

    def _clients(self, coin):
        """现货 + 永续，统一用 config.FLAG（两腿必须同环境）。"""
        spot = OKXClient(symbol=f"{coin}-USDT")
        swap = OKXClient(symbol=f"{coin}-USDT-SWAP")
        return spot, swap

    # ── 主循环 ──
    def scan_and_act(self) -> dict:
        result = {"live": self.live, "scanned": [], "opened": [], "closed": [], "holding": [], "would": []}
        now = datetime.now(timezone.utc)

        for coin, cfg in AUTO_CARRY_COINS.items():
            symbol = f"{coin}-USDT-SWAP"
            fr = fetch_funding_rate(symbol)
            if not fr:
                result["scanned"].append({coin: "fetch_failed"})
                continue
            rate = fr["funding_rate"]
            analysis = carry_analysis(symbol, rate, config.get_ct_val(f"{coin}_SWAP"), cfg["spot_size"])
            annual_net = analysis["annual_net_pct"]
            result["scanned"].append({coin: round(annual_net, 2)})

            if coin in self._positions:
                pos = self._positions[coin]
                try:
                    hold_h = (now - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 3600
                except Exception:
                    hold_h = 999
                if annual_net < EXIT_ANNUAL_PCT and hold_h >= MIN_HOLD_HOURS:
                    if not self.live:
                        result["would"].append(f"CLOSE {coin}(年化{annual_net:.1f}%)")
                        logger.info(f"[DRY] 将平仓 {coin}：年化降至 {annual_net:.1f}%")
                    elif self._close_carry(coin, pos):
                        del self._positions[coin]
                        self._save_positions()
                        result["closed"].append(coin)
                        send_tg(f"📤 [{coin}] Carry 平仓 — 年化降至 {annual_net:.1f}%")
                else:
                    result["holding"].append({coin: f"年化{annual_net:.1f}% 持{hold_h:.0f}h"})
            else:
                if annual_net >= cfg["min_annual"] and rate > 0:
                    if not self.live:
                        result["would"].append(f"OPEN {coin}(年化{annual_net:.1f}%)")
                        logger.info(f"[DRY] 将开仓 {coin}：年化 {annual_net:.1f}%，本金 {cfg['spot_size']} USDT")
                    else:
                        opened = self._open_carry(coin, cfg)
                        if opened:
                            self._positions[coin] = {
                                "opened_at": now.isoformat(),
                                "spot_size_usdt": cfg["spot_size"],
                                "spot_coins": opened["spot_coins"],
                                "contracts": opened["contracts"],
                                "entry_annual": annual_net,
                                "entry_rate": rate,
                            }
                            self._save_positions()
                            result["opened"].append(coin)
                            send_tg(f"📥 [{coin}] Carry 开仓\n费率 {rate*100:.4f}%\n年化 {annual_net:.1f}%\n"
                                    f"现货 {opened['spot_coins']} / 空 {opened['contracts']}张")
        return result

    # ── 开仓：现货市价买 + 永续市价空；第二腿失败回滚第一腿 ──
    def _open_carry(self, coin, cfg) -> dict | None:
        base = coin
        spot, swap = self._clients(coin)
        try:
            price = float(spot.get_ticker()["last"])
            if price <= 0:
                return None
            dec = config.COIN_CONFIG.get(coin, {}).get("size_decimals", 4)
            want_coins = cfg["spot_size"] / price
            want_coins = math.floor(want_coins * 10**dec) / 10**dec if dec > 0 else math.floor(want_coins)
            if want_coins <= 0:
                logger.warning(f"[{coin}] 计算现货数量为0，跳过")
                return None

            before = spot.get_spot_position(base)
            # 第一腿：现货市价买入
            spot.place_order("buy", price, want_coins, order_type="market")
            # 轮询确认成交（最多 5s）
            for _ in range(10):
                time.sleep(0.5)
                after = spot.get_spot_position(base)
                got = after - before
                if got >= want_coins * FILL_TOLERANCE:
                    break
            else:
                logger.error(f"[{coin}] 现货未足额成交: 预期{want_coins:.6f} 实际{got:.6f}")
                return None

            # 第二腿：永续市价做空（按实际现货量对冲）
            ct_val = swap.get_ct_val()
            # 向下取整，避免 round 把不足1张凑成1张 → 空腿大于现货 → 净空头裸仓。
            contracts = math.floor(got / ct_val)
            if contracts < 1:
                logger.error(f"[{coin}] 对冲张数不足1张(现货{got:.6f}/面值{ct_val})，回滚现货")
                try:
                    spot.place_order("sell", price, got, order_type="market")
                except Exception as e2:
                    logger.error(f"[{coin}] ⚠️ 现货回滚失败，需人工介入: {e2}")
                    send_tg(f"🚨 [{coin}] Carry 现货已买但无法对冲且回滚失败，请立即人工平掉现货")
                return None
            try:
                swap.set_leverage()
                swap.place_futures_order("sell", contracts, order_type="market")
            except Exception as e:
                # 第二腿失败 → 立即回滚第一腿，绝不留单边裸仓
                logger.error(f"[{coin}] 永续做空失败，回滚现货: {e}")
                try:
                    spot.place_order("sell", price, got, order_type="market")
                except Exception as e2:
                    logger.error(f"[{coin}] ⚠️ 现货回滚也失败，需人工介入: {e2}")
                    send_tg(f"🚨 [{coin}] Carry 开仓半成品！现货已买但空腿失败且回滚失败，请立即人工平掉现货")
                return None

            logger.info(f"[{coin}] carry 开仓成功：现货 {got:.6f} + 空 {contracts}张 @ {price}")
            return {"spot_coins": got, "contracts": contracts}
        except Exception as e:
            logger.error(f"[{coin}] carry 开仓异常: {e}")
            send_tg(f"❌ [{coin}] Carry 开仓异常: {e}")
            return None

    # ── 平仓：先平永续空（去杠杆）再卖现货 ──
    def _close_carry(self, coin, pos) -> bool:
        base = coin
        spot, swap = self._clients(coin)
        try:
            # 1) 平永续空（reduceOnly 由 close_futures_position 处理，平整个仓）
            try:
                swap.close_futures_position()
            except Exception as e:
                logger.error(f"[{coin}] 平永续空失败: {e}")
                send_tg(f"🚨 [{coin}] Carry 平空失败，请人工检查: {e}")
                return False
            # 2) 卖现货（按实际持仓，交叉校验 pos 记录防 API 幻觉）
            held = spot.get_spot_position(base)
            expected = pos.get("spot_coins", 0) if isinstance(pos, dict) else 0
            if expected > 0 and abs(held - expected) / expected > 0.5:
                logger.warning(f"[{coin}] 平仓时现货持仓 {held:.6f} 与开仓记录 {expected:.6f} 偏差 >50%，仍按实际持仓卖出")
            dec = config.COIN_CONFIG.get(coin, {}).get("size_decimals", 4)
            sell = math.floor(held * 10**dec) / 10**dec if dec > 0 else math.floor(held)
            min_sz = config.COIN_CONFIG.get(coin, {}).get("min_order_size", 0)
            if sell >= max(min_sz, 0):
                price = float(spot.get_ticker()["last"])
                spot.place_order("sell", price, sell, order_type="market")
            logger.info(f"[{coin}] carry 平仓完成：已平空 + 卖现货 {sell}")
            return True
        except Exception as e:
            logger.error(f"[{coin}] carry 平仓异常: {e}")
            send_tg(f"❌ [{coin}] Carry 平仓异常: {e}")
            return False

    def status(self) -> dict:
        return {"live": self.live, "count": len(self._positions), "positions": self._positions}


def run_once(live=None):
    return CarryExecutor(live=live).scan_and_act()


def run_watch(interval_s=3600, live=None):
    ex = CarryExecutor(live=live)
    logger.info(f"carry 监控启动（{'实盘' if ex.live else 'DRY-RUN'}），间隔 {interval_s}s")
    while True:
        try:
            r = ex.scan_and_act()
            logger.info(f"[carry] 扫描{len(r['scanned'])} 开{len(r['opened'])} 平{len(r['closed'])} "
                        f"持{len(r['holding'])} 拟{len(r['would'])}")
        except Exception as e:
            logger.error(f"[carry] 异常: {e}")
        time.sleep(interval_s)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [carry] %(levelname)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--watch", type=int, default=0, help="持续监控间隔秒")
    p.add_argument("--status", action="store_true")
    args = p.parse_args()

    mode = "🔴 实盘下单" if LIVE else "🟢 DRY-RUN（不下单）"
    logger.info(f"carry_executor 模式: {mode}（实盘需 CARRY_LIVE=1，且勿与主 bot 同币种同跑）")

    if args.status:
        print(json.dumps(CarryExecutor().status(), indent=2, ensure_ascii=False, default=str))
    elif args.watch:
        run_watch(args.watch)
    else:
        print(json.dumps(run_once(), indent=2, ensure_ascii=False, default=str))
