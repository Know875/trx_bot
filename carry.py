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

import sys
import os
import time
import json
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger("carry")


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
        logger.warning(f"获取 {symbol} 费率失败: {e}")
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
    import httpx
    try:
        import config
        token = getattr(config, "TG_BOT_TOKEN", "") or os.environ.get("TG_BOT_TOKEN", "")
        chat_id = getattr(config, "TG_CHAT_ID", "") or os.environ.get("TG_CHAT_ID", "")
        if not token or not chat_id:
            return
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"TG通知失败: {e}")


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
            logger.warning("无费率数据")
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
                logger.info(f"已推送 {len(alerts)} 条告警")

    do_scan()

    if args.watch > 0:
        logger.info(f"持续监控，间隔 {args.watch}s")
        try:
            while True:
                time.sleep(args.watch)
                do_scan()
        except KeyboardInterrupt:
            logger.info("监控停止")


if __name__ == "__main__":
    main()
