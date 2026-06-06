"""
参数评分引擎 — 替代简单二元回滚，用多维指标打分

五个评估维度:
  ① 净PnL         ② PnL%         ③ 最大回撤
  ④ 手续费吞噬率   ⑤ 相对baseline优势

评分等级:
  score ≥ 80 → promote (正式采纳)
  score 50-79 → keep  (保留观察)
  score 20-49 → downgrade (降级/警告)
  score < 20  → rollback (强制回滚)
"""

import json, os, re, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
SCORE_FILE = str(ROOT / "param_scores.json")
logger = logging.getLogger("param_score")

# ═══════════════════════════════════════════════════════════
# ① 分币种回滚阈值
# ═══════════════════════════════════════════════════════════

COIN_THRESHOLDS = {
    # coin: {pnl_dollar, pnl_pct, fee_ratio_warn, fee_ratio_fatal, drawdown_max}
    "TRX":      {"pnl_dollar": -1.0, "pnl_pct": -1.2, "fee_warn": 0.30, "fee_fatal": 0.50, "dd_max": 8},
    "ETH":      {"pnl_dollar": -3.0, "pnl_pct": -2.0, "fee_warn": 0.35, "fee_fatal": 0.55, "dd_max": 12},
    "SOL":      {"pnl_dollar": -2.0, "pnl_pct": -2.5, "fee_warn": 0.40, "fee_fatal": 0.60, "dd_max": 15},
    "TRX_SWAP": {"pnl_dollar": -1.5, "pnl_pct": -1.0, "fee_warn": 0.25, "fee_fatal": 0.45, "dd_max": 6},
    "ETH_SWAP": {"pnl_dollar": -2.5, "pnl_pct": -1.5, "fee_warn": 0.30, "fee_fatal": 0.50, "dd_max": 10},
    "SOL_SWAP": {"pnl_dollar": -2.0, "pnl_pct": -2.0, "fee_warn": 0.35, "fee_fatal": 0.55, "dd_max": 12},
}

# 极端行情暂停阈值
# 注意：is_extreme_market 实际比较的是 ticker 的 open24h（24 小时涨跌），故键名用 24h。
EXTREME_THRESHOLDS = {
    "btc_24h_drop_pct": -3.0,
    "eth_24h_drop_pct": -4.0,
    "funding_extreme_abs": 0.0005,   # 费率绝对值 > 0.05%
}

# 回滚冷却期
ROLLBACK_COOLDOWN_DAYS = 7
# 冷却期内如果新回测收益超过 baseline 30% 可以破例
COOLDOWN_OVERRIDE_MULTIPLIER = 1.30

# ═══════════════════════════════════════════════════════════
# ② 手续费吞噬率计算
# ═══════════════════════════════════════════════════════════

def calc_fee_ratio(coin: str, since_iso: str = None) -> dict:
    """
    从日志计算手续费 / 毛利润 比例。
    返回 {gross_pnl, total_fees, fee_ratio, total_trades}
    """
    log_file = ROOT / f"bot_{coin}.log"
    if not log_file.exists():
        return {"gross_pnl": 0, "total_fees": 0, "fee_ratio": 0, "total_trades": 0}

    gross_pnl = 0.0  # 成交价差利润（不算手续费）
    total_fees = 0.0
    trade_count = 0

    since_dt = None
    if since_iso:
        from datetime import datetime, timedelta
        since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        since_local = (since_dt + timedelta(hours=8)).replace(tzinfo=None)

    last_ts = None
    try:
        with open(log_file) as f:
            for line in f:
                try:
                    last_ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                except:
                    pass

                if since_dt and last_ts and last_ts < since_local:
                    continue

                # 匹配手续费: "fee=0.0123" "手续费: 0.045" "taker_fee: 0.02"
                m = re.search(r'(?:fee|手续费|taker_fee|maker_fee)\s*[:=]\s*([\d.]+)', line, re.IGNORECASE)
                if m:
                    total_fees += float(m.group(1))

                # 匹配毛利润: "grid_profit=0.5" "trend_profit=-1.2" "settle: +1.5"
                m = re.search(r'(?:grid_profit|trend_profit|pnl_delta)\s*[:=]\s*([+-]?[\d.]+)', line)
                if m:
                    gross_pnl += float(m.group(1))
                    trade_count += 1

                # 网格结算: "settled +0.5 USDT"
                m = re.search(r'settled?\s*:\s*([+-][\d.]+)', line)
                if m:
                    gross_pnl += float(m.group(1))
                    trade_count += 1

    except Exception as e:
        return {"gross_pnl": gross_pnl, "total_fees": total_fees, "fee_ratio": 0, "total_trades": trade_count, "error": str(e)}

    fee_ratio = total_fees / gross_pnl if gross_pnl > 0 else (1.0 if total_fees > 0 else 0)

    return {
        "gross_pnl": round(gross_pnl, 3),
        "total_fees": round(total_fees, 4),
        "fee_ratio": round(fee_ratio, 3),
        "total_trades": trade_count,
    }


# ═══════════════════════════════════════════════════════════
# ③ 极端行情检测
# ═══════════════════════════════════════════════════════════

def is_extreme_market() -> dict:
    """检测是否处于极端行情，返回 {is_extreme, reasons}"""
    import httpx
    reasons = []
    is_extreme = False

    try:
        client = httpx.Client(timeout=10)

        # BTC ticker
        r = client.get("https://www.okx.com/api/v5/market/ticker", params={"instId": "BTC-USDT"})
        if r.status_code == 200:
            data = r.json()
            item = data.get("data", [{}])[0]
            price = float(item.get("last", 0))
            open24 = float(item.get("open24h", 0))
            if open24 > 0:
                change_pct = (price - open24) / open24 * 100
                if change_pct < EXTREME_THRESHOLDS["btc_24h_drop_pct"]:
                    is_extreme = True
                    reasons.append(f"BTC 24h 跌幅 {change_pct:.1f}% > 阈值 {EXTREME_THRESHOLDS['btc_24h_drop_pct']}%")

        # BTC 资金费率
        r = client.get("https://www.okx.com/api/v5/public/funding-rate", params={"instId": "BTC-USDT-SWAP"})
        if r.status_code == 200:
            data = r.json()
            item = data.get("data", [{}])[0]
            fr = float(item.get("fundingRate", 0))
            if abs(fr) > EXTREME_THRESHOLDS["funding_extreme_abs"]:
                is_extreme = True
                reasons.append(f"资金费率极端 {fr:.4%}")

        # ETH ticker
        r = client.get("https://www.okx.com/api/v5/market/ticker", params={"instId": "ETH-USDT"})
        if r.status_code == 200:
            data = r.json()
            item = data.get("data", [{}])[0]
            price = float(item.get("last", 0))
            open24 = float(item.get("open24h", 0))
            if open24 > 0:
                change_pct = (price - open24) / open24 * 100
                if change_pct < EXTREME_THRESHOLDS["eth_24h_drop_pct"]:
                    is_extreme = True
                    reasons.append(f"ETH 24h 跌幅 {change_pct:.1f}% > 阈值 {EXTREME_THRESHOLDS['eth_24h_drop_pct']}%")

        client.close()
    except Exception as e:
        logger.warning(f"极端行情检测失败: {e}")

    return {"is_extreme": is_extreme, "reasons": reasons}


# ═══════════════════════════════════════════════════════════
# ④ 参数冷却/黑名单
# ═══════════════════════════════════════════════════════════

def _load_scores():
    if os.path.exists(SCORE_FILE):
        with open(SCORE_FILE) as f:
            return json.load(f)
    return {"scores": [], "cooldown": {}, "history": []}


def _save_scores(data):
    # 原子写入：先写临时文件再 rename，防止中途 kill 导致文件损坏
    tmp = SCORE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, SCORE_FILE)


def is_on_cooldown(coin: str, param: str, value) -> dict:
    """
    检查参数是否在冷却期。
    返回 {on_cooldown, can_override, reason}
    """
    scores = _load_scores()
    key = f"{coin}.{param}.{value}"
    cooldown = scores.get("cooldown", {}).get(key)

    if not cooldown:
        return {"on_cooldown": False, "can_override": False, "reason": ""}

    rolled_at = datetime.fromisoformat(cooldown["rolled_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    days = (now - rolled_at).total_seconds() / 86400

    if days >= ROLLBACK_COOLDOWN_DAYS:
        # 冷却期结束
        del scores["cooldown"][key]
        _save_scores(scores)
        return {"on_cooldown": False, "can_override": False, "reason": ""}

    can_override = cooldown.get("override_allowed", False)
    remaining = round(ROLLBACK_COOLDOWN_DAYS - days, 1)

    return {
        "on_cooldown": True,
        "can_override": can_override,
        "reason": f"冷却期剩余 {remaining} 天 (回滚於 {cooldown['rolled_at'][:10]})",
        "rolled_at": cooldown["rolled_at"],
        "rollback_reason": cooldown.get("reason", ""),
    }


def add_cooldown(coin: str, param: str, value, rollback_reason=""):
    """将参数加入冷却黑名单"""
    scores = _load_scores()
    key = f"{coin}.{param}.{value}"
    scores.setdefault("cooldown", {})[key] = {
        "rolled_at": datetime.now(timezone.utc).isoformat(),
        "reason": rollback_reason,
    }
    _save_scores(scores)
    logger.info(f"  🚫 {key} 冷藏 {ROLLBACK_COOLDOWN_DAYS} 天")


# ═══════════════════════════════════════════════════════════
# ⑤ 多维参数评分
# ═══════════════════════════════════════════════════════════

def score_param(
    coin: str,
    param: str,
    current_value,
    new_value,
    current_pnl: float,
    new_pnl: float,
    current_dd: float = 0,
    new_dd: float = 0,
    current_fee_ratio: float = 0,
    new_fee_ratio: float = 0,
    win_rate: float = 50,
    backtest_baseline_pnl: float = 0,
) -> dict:
    """
    多维评分引擎。

    满分 100 分，扣除项:
      - PnL 绝对亏损
      - PnL% 亏损
      - 最大回撤
      - 手续费占比过高
      - 相对 baseline 不优
    """

    thresholds = COIN_THRESHOLDS.get(coin, COIN_THRESHOLDS["ETH"])
    score = 60.0  # 起评分

    # ── PnL 维度 (0-25分) ──
    if new_pnl >= 0:
        score += 20
    elif new_pnl > thresholds["pnl_dollar"]:
        score += 10  # 亏损但未超阈值
    else:
        score += max(-15, new_pnl * 2)  # 严重亏损扣分

    # ── PnL% 维度 (0-15分) ──
    # 根据币种实际本金估算 PnL%
    base_capitals = {"TRX": 50, "ETH": 3000, "SOL": 4000, "TRX_SWAP": 60, "ETH_SWAP": 120, "SOL_SWAP": 40}
    cap = base_capitals.get(coin, 50)
    pnl_pct = new_pnl / cap * 100 if cap > 0 else 0

    if pnl_pct > 0:
        score += 15
    elif pnl_pct > thresholds["pnl_pct"]:
        score += 5
    else:
        score += max(-15, pnl_pct * 3)

    # ── 回撤惩罚 (0 to -20) ──
    if new_dd > thresholds["dd_max"] * 1.5:
        score -= 20
    elif new_dd > thresholds["dd_max"]:
        score -= 10

    # ── 手续费吞噬惩罚 (0 to -20) ──
    if new_fee_ratio > thresholds["fee_fatal"]:
        score -= 20
    elif new_fee_ratio > thresholds["fee_warn"]:
        score -= 10

    # ── 相对 baseline 优势 (-15 to +15) ──
    # 核心问题: 新参数有没有比旧参数更好？
    if backtest_baseline_pnl:
        improvement = new_pnl - current_pnl
        pct_improvement = (improvement / abs(backtest_baseline_pnl)) * 100 if backtest_baseline_pnl else 0
        if improvement > 0 and pct_improvement > 1:
            score += 15
        elif improvement > 0:
            score += 5
        elif improvement < -5:
            score -= 15
        elif improvement < 0:
            score -= 5
    else:
        # 无 baseline，看绝对表现
        if new_pnl > current_pnl * 1.1:
            score += 5

    # ── 胜率调整 (-5 to +5) ──
    if win_rate > 70:
        score += 5
    elif win_rate < 40:
        score -= 5

    # 钳制
    score = max(0, min(100, round(score, 1)))

    # ── 评级 ──
    if score >= 80:
        grade = "promote"
        action = "✅ 正式采纳"
    elif score >= 50:
        grade = "keep"
        action = "👀 保留观察"
    elif score >= 20:
        grade = "downgrade"
        action = "⚠️ 降级警告"
    else:
        grade = "rollback"
        action = "🔄 强制回滚"

    # 检查冷却期
    cooldown = is_on_cooldown(coin, param, new_value)
    if cooldown["on_cooldown"] and grade in ("promote", "keep", "downgrade") and not cooldown["can_override"]:
        grade = "cooldown_blocked"
        action = f"🚫 冷却期阻止 ({cooldown['reason']})"

    return {
        "score": score,
        "grade": grade,
        "action": action,
        "coin": coin,
        "param": param,
        "current_value": current_value,
        "new_value": new_value,
        "details": {
            "pnl_delta": round(new_pnl - current_pnl, 3),
            "pnl_pct": round(pnl_pct, 2),
            "fee_ratio": round(new_fee_ratio, 3),
            "drawdown_pct": round(new_dd, 2),
            "win_rate": round(win_rate, 1),
            "vs_baseline": round((new_pnl - current_pnl), 3),
        },
        "cooldown": cooldown,
    }


def score_from_finding(finding: dict, pnl_info: dict = None, fee_info: dict = None) -> dict:
    """从 explore 的 finding + 实测 pnl/fee 数据生成评分"""
    coin = finding["coin"]
    param = finding["param"]

    current_pnl = finding.get("current_pnl", 0)
    new_pnl = finding.get("candidate_pnl", 0)
    win_rate = finding.get("win_rate", 50)

    # 从实测数据补充
    if pnl_info:
        current_pnl = pnl_info.get("net_pnl", current_pnl)
    if fee_info:
        new_fee = fee_info.get("fee_ratio", 0)
    else:
        new_fee = 0

    return score_param(
        coin=coin,
        param=param,
        current_value=finding.get("current", 0),
        new_value=finding.get("candidate", 0),
        current_pnl=current_pnl,
        new_pnl=new_pnl,
        new_fee_ratio=new_fee,
        win_rate=win_rate,
        backtest_baseline_pnl=current_pnl,
    )


# ═══════════════════════════════════════════════════════════
# 状态查看
# ═══════════════════════════════════════════════════════════

def show_scores():
    scores = _load_scores()
    history = scores.get("scores", [])
    cooldowns = scores.get("cooldown", {})

    print("📊 参数评分历史")
    print("=" * 60)

    if not history:
        print("  暂无评分记录")
    else:
        recent = history[-20:]
        by_grade = {}
        for s in recent:
            by_grade.setdefault(s["grade"], []).append(s)

        for grade, items in by_grade.items():
            gname = {"promote":"✅ 采纳","keep":"👀 观察","downgrade":"⚠️ 降级","rollback":"🔄 回滚"}.get(grade, grade)
            print(f"\n  {gname} ({len(items)} 项):")
            for s in items[-3:]:
                print(f"    {s['coin']}.{s['param']}: {s['current_value']}→{s['new_value']}  评分: {s['score']:.0f}")

    if cooldowns:
        print(f"\n🚫 参数冷却 ({len(cooldowns)} 项):")
        for key, info in cooldowns.items():
            days_left = ROLLBACK_COOLDOWN_DAYS - (datetime.now(timezone.utc) - datetime.fromisoformat(info["rolled_at"].replace("Z", "+00:00"))).total_seconds() / 86400
            if days_left > 0:
                print(f"    {key}: 剩余 {days_left:.1f} 天 ({info.get('reason','')})")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    if len(sys.argv) < 2:
        print("用法: python param_score.py [score|cooldown|extreme|fees|test]")
        print("  score    查看评分历史")
        print("  cooldown 查看冷却列表")
        print("  extreme  检测极端行情")
        print("  fees     测试手续费吞噬率")
        print("  test     跑一次评分演示")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "score":
        show_scores()
    elif cmd == "cooldown":
        scores = _load_scores()
        for k, v in scores.get("cooldown", {}).items():
            print(f"  {k}: {v}")
    elif cmd == "extreme":
        result = is_extreme_market()
        print(f"  极端行情: {'是 ⚠️' if result['is_extreme'] else '否 ✅'}")
        for r in result["reasons"]:
            print(f"    - {r}")
    elif cmd == "fees":
        for coin in ["TRX", "ETH", "SOL", "TRX_SWAP", "ETH_SWAP", "SOL_SWAP"]:
            fees = calc_fee_ratio(coin)
            print(f"  {coin:12s}  毛利润: ${fees['gross_pnl']:.3f}  手续费: ${fees['total_fees']:.4f}  吞噬率: {fees['fee_ratio']:.1%}  交易: {fees['total_trades']}")
    elif cmd == "test":
        # 演示评分
        test_cases = [
            ("ETH", "grid_count", 6, 2, -6.7, 8.1, 50, 0.15, 0.05),
            ("SOL", "grid_count", 3, 2, -13.4, -5.8, 50, 0.30, 0.20),
            ("TRX", "grid_range_pct", 0.05, 0.07, 16.5, 20.6, 42, 0.08, 0.06),
            ("ETH_SWAP", "grid_count", 5, 2, -3.5, -1.2, 55, 0.25, 0.40),
        ]
        for coin, param, old_v, new_v, old_pnl, new_pnl, wr, old_fee, new_fee in test_cases:
            r = score_param(coin, param, old_v, new_v, old_pnl, new_pnl, current_fee_ratio=old_fee, new_fee_ratio=new_fee, win_rate=wr, backtest_baseline_pnl=old_pnl)
            print(f"\n  {coin}.{param}: {old_v}→{new_v}")
            print(f"    PnL: {old_pnl:+.1f}%→{new_pnl:+.1f}%  胜率: {wr:.0f}%  手续费: {old_fee:.0%}→{new_fee:.0%}")
            print(f"    评分: {r['score']:.0f}  等级: {r['grade']}  动作: {r['action']}")
    else:
        print(f"未知命令: {cmd}")
