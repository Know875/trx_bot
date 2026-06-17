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
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from okx_client import OKXClient
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
            got = 0
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


"""
资金费率套利监控 — delta-neutral carry

原理：当某币永续资金费率持续为正（多头付费给空头）时：
  现货买入等量 + 永续做空等量 → 价格涨跌对冲 → 净赚资金费

这是散户在 OKX 为数不多有数学依据的真实边际，
年化在高费率期 (>> 交易成本) 可观，且几乎无方向性风险。

用法:
  python carry.py                    # 打印当前所有币种费率+年化
  python carry.py --watch 3600       # 持续监控，间隔3600秒
  python carry.py --json             # JSON输出，给其他脚本消费
  python carry.py --alert 20.0       # 年化>20%时推TG
"""

_carry_logger = logging.getLogger("carry")


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

# 监控的永续合约
WATCH_SWAPS = {
    "TRX-USDT-SWAP":  1000.0,  # ct_val (每张面值 USDT)
    "ETH-USDT-SWAP":  0.01,
    "SOL-USDT-SWAP":  1.0,
    "BTC-USDT-SWAP":  0.01,    # BTC 作为基准监控（不一定要套利，但费率是先行指标）
}

# 交易成本（双边）
TRADE_COST_PCT = 0.10   # 双边开仓成本 ≈ 2×0.05%（taker fee + spread）

# 费率年化阈值
ALERT_ANNUAL_PCT = 20.0  # 默认告警阈值

# 历史费率缓存
CACHE_FILE = Path(__file__).parent / ".funding_cache.json"


# ═══════════════════════════════════════════════════════════════
# 核心：费率获取 + 年化计算
# ═══════════════════════════════════════════════════════════════

def fetch_funding_rate(symbol: str) -> Optional[dict]:
    """
    获取当前资金费率。返回 None 表示获取失败。

    用 GET /api/v5/public/funding-rate（无需认证）
    """
    import httpx
    try:
        r = httpx.get(
            f"https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": symbol},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        items = data.get("data", [])
        if not items:
            return None
        item = items[0]
        return {
            "symbol": item.get("instId", symbol),
            "funding_rate": float(item.get("fundingRate", 0)),
            "next_funding_time": item.get("nextFundingTime", ""),
            "funding_time": "",  # 这个接口不返回 method/maxRate
        }
    except Exception as e:
        _carry_logger.warning(f"获取 {symbol} 费率失败: {e}")
        return None


def annualize_rate(rate: float, interval_hours: int = 8) -> float:
    """
    年化资金费率。

    rate: 每次结算费率（如 0.0001 = 0.01%）
    interval_hours: 结算间隔（OKX 默认 8h）
    """
    settlements_per_year = 365 * 24 / interval_hours
    return rate * settlements_per_year * 100  # 转百分比


def fee_cost_pct(spot_size_usdt: float = 1000.0) -> float:
    """双边开仓成本占本金的百分比"""
    return TRADE_COST_PCT


def carry_analysis(symbol: str, funding_rate: float, ct_val: float,
                   capital: float = 1000.0) -> dict:
    """
    分析一笔 delta-neutral carry 的预期收益。

    参数:
      funding_rate:  当前费率（下次结算）
      ct_val:        合约面值
      capital:       本金（现货端 = 永续端）

    返回:
      - annual_pct:  预期年化%
      - net_annual:  扣除成本后的年化%
      - monthly:     预期月收益 USDT
      - risk:        风险等级 (low/medium/high)
      - viable:      是否值得做（>成本且有利）
    """
    annual_raw = annualize_rate(funding_rate)
    cost = fee_cost_pct(capital)
    annual_net = annual_raw - cost  # 简化：成本摊销首期

    # 风险等级：只看费率本身
    if annual_raw > 50:
        risk = "high"    # 高费率 → 可能费率反转
    elif annual_raw > 20:
        risk = "medium"
    else:
        risk = "low"

    monthly = capital * annual_net / 100 / 12

    return {
        "symbol": symbol,
        "funding_rate": funding_rate,
        "annual_raw_pct": round(annual_raw, 2),
        "cost_pct": round(cost, 2),
        "annual_net_pct": round(annual_net, 2),
        "monthly_usdt": round(monthly, 2),
        "risk": risk,
        "viable": annual_net > 0 and funding_rate > 0,
        "strong_signal": annual_net > 10 and funding_rate > 0,
    }


# ═══════════════════════════════════════════════════════════════
# 历史缓存（跟踪费率变化趋势，判断稳定性）
# ═══════════════════════════════════════════════════════════════

def _load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_cache(data):
    CACHE_FILE.write_text(json.dumps(data))


def update_cache(symbol: str, rate: float):
    """记录费率快照"""
    cache = _load_cache()
    now = datetime.now(timezone.utc).isoformat()
    series = cache.get(symbol, [])
    series.append({"time": now, "rate": rate})
    # 只保留24h
    cutoff = time.time() - 86400
    series = [s for s in series if _ts_parse(s["time"]) > cutoff]
    cache[symbol] = series
    _save_cache(cache)


def _ts_parse(ts):
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0


def funding_trend(symbol: str) -> dict:
    """24h 费率趋势"""
    cache = _load_cache()
    series = cache.get(symbol, [])
    if not series:
        return {"trend": "unknown", "samples": 0}

    rates = [s["rate"] for s in series]
    avg = sum(rates) / len(rates)
    first = rates[0] if rates else 0
    last = rates[-1] if rates else 0
    change = last - first

    # 趋势判断
    if all(r > 0 for r in rates):
        if change > 0:
            trend = "↑ 加速（费率上升中）"
        elif change < 0:
            trend = "↘ 减速（费率下降中）"
        else:
            trend = "→ 稳定"
    elif not any(r > 0 for r in rates):
        trend = "❌ 全负（无套利机会）"
    else:
        trend = "⚠️ 不稳定（时正时负）"

    return {
        "trend": trend,
        "samples": len(rates),
        "avg": round(avg, 6),
        "first": round(first, 6),
        "last": round(last, 6),
        "positive_pct": round(sum(1 for r in rates if r > 0) / len(rates) * 100, 1),
    }


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def scan_all() -> list[dict]:
    """扫描所有监控币种"""
    results = []
    for symbol, ct_val in WATCH_SWAPS.items():
        rate_info = fetch_funding_rate(symbol)
        if rate_info is None:
            continue
        rate = rate_info["funding_rate"]
        update_cache(symbol, rate)
        analysis = carry_analysis(symbol, rate, ct_val)
        trend = funding_trend(symbol)
        results.append({**analysis, "trend": trend, "next_time": rate_info["next_funding_time"]})
    return results


def send_tg_alert(msg: str):
    """发送 Telegram 告警"""
    from notify import send_tg
    send_tg(msg)


def main():
    parser = argparse.ArgumentParser(description="资金费率套利监控")
    parser.add_argument("--watch", type=int, default=0, help="持续监控（秒）")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    parser.add_argument("--alert", type=float, default=ALERT_ANNUAL_PCT, help=f"TG告警阈值（默认{ALERT_ANNUAL_PCT}%）")
    parser.add_argument("--coin", default="", help="只监控指定币种")
    parser.add_argument("--trend", action="store_true", help="显示24h费率趋势")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [carry] %(levelname)s: %(message)s",
    )

    def do_scan():
        results = scan_all()
        if args.coin:
            results = [r for r in results if args.coin.upper() in r["symbol"]]

        if not results:
            _carry_logger.warning("无费率数据")
            return

        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            print(f"\n{'═' * 70}")
            print(f"  资金费率套利监控  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
            print(f"{'═' * 70}")
            for r in results:
                emoji = "🟢" if r["strong_signal"] else ("🟡" if r["viable"] else "⚪")
                coin = r["symbol"].replace("-USDT-SWAP", "")
                print(f"  {emoji} {coin:>5s}  "
                      f"费率={r['funding_rate']:+.6f}  "
                      f"年化={r['annual_raw_pct']:+.2f}% "
                      f"净={r['annual_net_pct']:+.2f}% "
                      f"月={r['monthly_usdt']:+.2f} USDT "
                      f"风险={r['risk']}")
                if args.trend:
                    t = r["trend"]
                    print(f"      趋势: {t['trend']} "
                          f"({t['samples']}样本, +%= {t['positive_pct']}%)")

            # 汇总
            strong = [r for r in results if r["strong_signal"]]
            viable = [r for r in results if r["viable"]]
            print(f"{'─' * 70}")
            if strong:
                coins = ", ".join(r["symbol"] for r in strong)
                print(f"  🔥 强烈信号: {coins}")
            if viable:
                print(f"  ✅ 可套利: {len(viable)}/{len(results)}")
            else:
                print(f"  ❌ 当前无套利机会")

        # ── TG 告警 ──
        if args.alert > 0:
            alerts = [r for r in results if r["annual_net_pct"] >= args.alert and r["viable"]]
            if alerts:
                msg_parts = []
                for r in alerts:
                    coin = r["symbol"].replace("-USDT-SWAP", "")
                    msg_parts.append(f"{coin} 净年化 {r['annual_net_pct']:+.1f}%")
                msg = "💰 资金费率套利机会:\n" + "\n".join(msg_parts)
                send_tg_alert(msg)
                _carry_logger.info(f"已推送 {len(alerts)} 条告警")

    do_scan()

    if args.watch > 0:
        _carry_logger.info(f"持续监控，间隔 {args.watch}s")
        try:
            while True:
                time.sleep(args.watch)
                do_scan()
        except KeyboardInterrupt:
            _carry_logger.info("监控停止")


def _cli_carry():
    main()
