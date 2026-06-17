"""
宏观智能层 — 多层市场结构分析（不止看K线）

从 GPT 回答中提炼的五层分析框架：
  第一层：杠杆健康度（OI + 资金费率 + 爆仓量）
  第二层：机构流向（ETF净流入/流出）
  第三层：情绪面（叙事/信心/关键事件）
  第四层：资金轮动（BTC市占率、美股/IPO相关）
  第五层：技术结构（关键支撑/阻力位）

输出 → 风险评分 + 仓位建议倍率 + 4个关键信号检查

用法：
  from macro import MacroIntelligence
  mi = MacroIntelligence()
  report = mi.analyze()  # 完整宏观分析
  signals = mi.check_signals()  # 只看4个信号
"""

import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
import config

logger = logging.getLogger("macro")

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
TIMEOUT = 20

# ── OKX API 端点 ──
OKX_REST = "https://www.okx.com"

SYSTEM_PROMPT = """你是一个专业的加密货币宏观分析师。
你的分析框架有五层：
1. 杠杆健康度（OI变化 + 资金费率 + 24h爆仓）
2. 机构流向（ETF数据）
3. 情绪面（市场叙事 + 信心）
4. 资金轮动（BTC市占率）
5. 技术结构（关键价位）

你必须输出合法的 JSON，不要 markdown 代码块，不要额外文字。"""


class MacroIntelligence:
    """宏观智能分析器 — 每分钟最多调用一次 DeepSeek（有缓存）"""

    def __init__(self):
        self._client = httpx.Client(timeout=15)
        self._last_result = None
        self._last_time = 0

    # ═══════════════ 数据采集 ═══════════════

    def _okx(self, path: str, params: dict = None) -> dict:
        try:
            r = self._client.get(f"{OKX_REST}{path}", params=params)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == "0":
                return data
            logger.warning(f"OKX {path}: code={data.get('code')} msg={data.get('msg')}")
        except Exception as e:
            logger.warning(f"OKX {path} 请求失败: {e}")
        return {}

    def fetch_btc_oi(self) -> dict:
        """BTC 合约持仓量"""
        data = self._okx("/api/v5/public/open-interest", {"instType": "SWAP", "instId": "BTC-USDT-SWAP"})
        arr = data.get("data", [])
        if not arr:
            return {"oi": None, "oi_ts": None}
        item = arr[0]
        return {
            "oi": float(item.get("oi", 0)),
            "oi_ts": item.get("ts", ""),
        }

    def fetch_funding_rate(self, inst_id: str = "BTC-USDT-SWAP") -> dict:
        """资金费率"""
        data = self._okx("/api/v5/public/funding-rate", {"instId": inst_id})
        arr = data.get("data", [])
        if not arr:
            return {"funding_rate": None}
        item = arr[0]
        return {
            "funding_rate": float(item.get("fundingRate", 0)),
            "next_funding_time": item.get("nextFundingTime", ""),
        }

    def fetch_btc_dominance(self) -> float:
        """BTC 市占率（从 OKX 指数或 CoinGecko 免费 API）"""
        try:
            r = self._client.get(
                "https://api.coingecko.com/api/v3/global",
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            return data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0)
        except Exception:
            return 0

    def fetch_btc_price_and_change(self) -> dict:
        """BTC 当前价格 + 24h涨跌"""
        data = self._okx("/api/v5/market/ticker", {"instId": "BTC-USDT"})
        arr = data.get("data", [])
        if not arr:
            return {"price": 0, "change_24h_pct": 0}
        item = arr[0]
        return {
            "price": float(item.get("last", 0)),
            "change_24h_pct": float(item.get("open24h", 0)) and round(
                (float(item["last"]) - float(item["open24h"])) / float(item["open24h"]) * 100, 2
            ) or 0,
            "high_24h": float(item.get("high24h", 0)),
            "low_24h": float(item.get("low24h", 0)),
        }

    def fetch_liquidation_pressure(self) -> dict:
        """
        从 OKX 公开数据推算爆仓压力（无需 API key）
        用 OI 变化率 + 价格波动 + 资金费率 共同推测杠杆清算压力
        """
        # BTC OI + 价格 + 费率
        oi = self._okx("/api/v5/public/open-interest", {"instType": "SWAP", "instId": "BTC-USDT-SWAP"})
        ticker = self._okx("/api/v5/market/ticker", {"instId": "BTC-USDT-SWAP"})
        funding = self._okx("/api/v5/public/funding-rate", {"instId": "BTC-USDT-SWAP"})

        result = {"liq_pressure": "未知", "signals": []}

        try:
            oi_val = float(oi.get("data", [{}])[0].get("oi", 0))
            price = float(ticker.get("data", [{}])[0].get("last", 0))
            high24h = float(ticker.get("data", [{}])[0].get("high24h", price))
            low24h = float(ticker.get("data", [{}])[0].get("low24h", price))
            fr = float(funding.get("data", [{}])[0].get("fundingRate", 0))
            vol = float(ticker.get("data", [{}])[0].get("volCcy24h", 0))

            # 24h 波动幅度
            vol_pct = (high24h - low24h) / low24h if low24h > 0 else 0

            # 压力判断逻辑
            pressure = 0  # 0~100
            if fr < -0.0001:  # 负费率 = 多头被清算中
                result["signals"].append(f"资金费率负({fr:.4%})，多头正被清算")
                pressure += 25
            if vol_pct > 0.05:  # 24h波动超过5%
                result["signals"].append(f"24h波动{vol_pct:.1%}，清算风险升高")
                pressure += 20
            if price < low24h * 1.02:  # 价格在24h低位附近
                result["signals"].append(f"价格接近24h低点(${low24h:.0f})，多头压力大")
                pressure += 15

            # 根据压力给结论
            if pressure >= 50:
                result["liq_pressure"] = "高 — 多头正在被清算"
            elif pressure >= 25:
                result["liq_pressure"] = "中 — 存在清算风险"
            else:
                result["liq_pressure"] = "低 — 杠杆相对健康"

            result["oi"] = f"{oi_val/1e6:.1f}M"
            result["funding_rate"] = round(fr, 5)
            result["volatility_24h"] = f"{vol_pct:.1%}"
            result["pressure_score"] = pressure

        except Exception as e:
            logger.warning(f"清算压力计算失败: {e}")

        return result

    def fetch_altcoin_snapshot(self) -> dict:
        """ETH/SOL 对 BTC 的汇率（衡量山寨强弱）"""
        eth_btc = self._okx("/api/v5/market/ticker", {"instId": "ETH-BTC"})
        sol_btc = self._okx("/api/v5/market/ticker", {"instId": "SOL-BTC"})

        result = {}
        try:
            eth_data = eth_btc.get("data", [{}])[0]
            result["eth_btc"] = float(eth_data.get("last", 0))
            result["eth_btc_change"] = round(float(eth_data.get("open24h", 0)) and
                (float(eth_data["last"]) - float(eth_data["open24h"])) / float(eth_data["open24h"]) * 100, 2)
        except: result["eth_btc"] = 0

        try:
            sol_data = sol_btc.get("data", [{}])[0]
            result["sol_btc"] = float(sol_data.get("last", 0))
            result["sol_btc_change"] = round(float(sol_data.get("open24h", 0)) and
                (float(sol_data["last"]) - float(sol_data["open24h"])) / float(sol_data["open24h"]) * 100, 2)
        except: result["sol_btc"] = 0

        return result

    # ═══════════════ 核心分析 ═══════════════

    def analyze(self, force_refresh=False) -> Optional[dict]:
        """
        完整的五层宏观分析。
        返回值包含 risk_score, position_multiplier, signal_checks, layer_analysis
        """
        import time
        now = time.time()
        if not force_refresh and self._last_result and (now - self._last_time) < 60:
            return self._last_result

        if not getattr(config, "DEEPSEEK_API_KEY", ""):
            return None

        # ── 采集所有数据 ──
        btc = self.fetch_btc_price_and_change()
        oi = self.fetch_btc_oi()
        funding = self.fetch_funding_rate()
        dominance = self.fetch_btc_dominance()
        alts = self.fetch_altcoin_snapshot()
        liq = self.fetch_liquidation_pressure()

        # ── 构建分析 Prompt ──
        data_block = f"""
BTC 当前价格: ${btc['price']:.0f}
BTC 24h涨跌: {btc['change_24h_pct']:+.1f}%
BTC 24h高/低: ${btc['high_24h']:.0f} / ${btc['low_24h']:.0f}

BTC 合约持仓量: {liq.get('oi','?')}
BTC 当前资金费率: {liq.get('funding_rate','?')}
BTC 24h波动: {liq.get('volatility_24h','?')}
BTC 市占率: {dominance:.1f}%

清算压力评估: {liq.get('liq_pressure','?')}
清算信号: {'; '.join(liq.get('signals', []))}

山寨币对BTC汇率:
  ETH/BTC: {alts.get('eth_btc','?')} (24h: {alts.get('eth_btc_change','?')}%)
  SOL/BTC: {alts.get('sol_btc','?')} (24h: {alts.get('sol_btc_change','?')}%)
"""

        user_prompt = f"""以下是当前加密货币市场宏观数据：

{data_block}

请按五层框架逐层分析，每层用一句话给结论：

第1层 - 杠杆健康度：OI和资金费率是否显示高杠杆？多头是否在去杠杆？
第2层 - 机构流向：结合价格和市占率，ETF是否可能流出？
第3层 - 情绪面：市场信心如何？ETH/BTC 和 SOL/BTC 汇率是否反映山寨恐慌？
第4层 - 资金轮动：BTC市占率是否上升（山寨资金逃往BTC）？
第5层 - 技术结构：当前最重要的支撑/阻力位？

然后给出：
1. risk_score（0~1，0=极度安全，1=极度危险）
2. position_multiplier（0.0~1.0，当前应该用多少仓位，1=满仓，0=空仓）
3. signal_checks（4个信号，每个给 status: "pass"|"fail"|"watch"）：
   - BTC关键位收复（63k-65k）：pass/fail/watch
   - 60k有效守住：pass/fail/watch
   - ETF停止流出：pass/fail/watch
   - 合约去杠杆完成：pass/fail/watch
4. summary（30字以内中文总结）

输出严格JSON：
{{"risk_score": 0.7, "position_multiplier": 0.6, "signal_checks": [{{"signal":"BTC关键位收复","status":"fail"}},{{"signal":"60k有效守住","status":"watch"}},{{"signal":"ETF停止流出","status":"watch"}},{{"signal":"合约去杠杆完成","status":"watch"}}], "layers": {{"layer1":"...","layer2":"...","layer3":"...","layer4":"...","layer5":"..."}}, "summary":"..."}}"""

        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 500,
            "response_format": {"type": "json_object"},
        }

        try:
            resp = httpx.post(
                DEEPSEEK_URL,
                headers={
                    "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            result = json.loads(content)

            # 校验
            required = ["risk_score", "position_multiplier", "signal_checks", "layers", "summary"]
            for k in required:
                if k not in result:
                    logger.warning(f"宏观AI 缺少字段: {k}")
                    return None

            risk = result["risk_score"]
            mult = result["position_multiplier"]

            logger.info(
                f"宏观分析: 风险={risk:.0%} 仓位倍率={mult:.0%} | "
                f"{result['summary']} | 信号: {[s['status'] for s in result['signal_checks']]}"
            )

            self._last_result = result
            self._last_time = time.time()
            return result

        except httpx.TimeoutException:
            logger.warning("宏观AI 超时")
        except Exception as e:
            logger.warning(f"宏观AI 异常: {e}")
        return None

    def check_signals(self) -> dict:
        """
        快速信号检查（不调用AI，纯数据判断）。
        返回 4 个信号的 pass/fail/watch。
        用于高频循环，不用每次都调 AI。
        """
        btc = self.fetch_btc_price_and_change()
        funding = self.fetch_funding_rate()
        dominance = self.fetch_btc_dominance()

        signals = {}

        # 信号1: BTC关键位 — 63k-65k
        price = btc["price"]
        if price > 65000:
            signals["BTC关键位收复"] = "pass"
        elif price > 63000:
            signals["BTC关键位收复"] = "watch"
        else:
            signals["BTC关键位收复"] = "fail"

        # 信号2: 60k有效守住
        if price > 62000:
            signals["60k有效守住"] = "pass"
        elif price > 60000:
            signals["60k有效守住"] = "watch"
        else:
            signals["60k有效守住"] = "fail"

        # 信号3: ETF流出 — 用市占率代理（市占率↑=山寨资金逃往BTC=避险情绪）
        if dominance > 55:
            signals["ETF停止流出"] = "fail"
        elif dominance > 52:
            signals["ETF停止流出"] = "watch"
        else:
            signals["ETF停止流出"] = "pass"

        # 信号4: 合约去杠杆 — 资金费率负=杠杆清了
        fr = funding.get("funding_rate", 0) or 0
        if fr < 0:
            signals["合约去杠杆完成"] = "pass"
        elif fr < 0.0001:
            signals["合约去杠杆完成"] = "watch"
        else:
            signals["合约去杠杆完成"] = "fail"

        return signals


# ═══════════════════════════════════════════════════════════
# 集成层：给策略用的宏观调整
# ═══════════════════════════════════════════════════════════

def get_macro_adjustment(macro_result: dict = None) -> dict:
    """
    将宏观分析结果转换为策略参数调整。
    返回 dict 供 base_adaptive.py / trx_adaptive.py 使用。
    """
    if not macro_result:
        return {
            "macro_risk": 0.5,
            "position_multiplier": 1.0,
            "grid_width_mult": 1.0,
            "tp_mult": 1.0,
            "max_positions_mult": 1.0,
            "warnings": [],
        }

    risk = macro_result.get("risk_score", 0.5)
    mult = macro_result.get("position_multiplier", 1.0)
    signals = macro_result.get("signal_checks", [])

    # 高风险 → 网格放宽（减少被套）、止盈收紧（快跑）、仓位减
    # 低风险 → 网格收紧、止盈放远、仓位加
    grid_mult = 1.0 + (risk - 0.5) * 0.8       # risk=0.7 → 1.16x 宽
    tp_mult = 1.0 - (risk - 0.5) * 0.6          # risk=0.7 → 0.88x 近
    max_pos_mult = 1.0 - (risk - 0.5) * 1.0      # risk=0.7 → 0.8x 少

    # 收集警告
    warnings = [s["signal"] for s in signals if s.get("status") == "fail"]

    return {
        "macro_risk": round(risk, 2),
        "position_multiplier": round(mult, 2),
        "grid_width_mult": round(grid_mult, 2),
        "tp_mult": round(tp_mult, 2),
        "max_positions_mult": round(max_pos_mult, 2),
        "warnings": warnings,
    }


# ═══════════════════════════════════════════════════════════
# CLI 测试
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    mi = MacroIntelligence()

    print("📡 快速信号检查（不调AI）：")
    signals = mi.check_signals()
    for k, v in signals.items():
        emoji = {"pass": "✅", "fail": "❌", "watch": "⚠️"}.get(v, "❓")
        print(f"  {emoji} {k}: {v}")

    print("\n🧠 完整宏观分析（调AI）：")
    result = mi.analyze()
    if result:
        print(f"  风险评分: {result['risk_score']:.0%}")
        print(f"  仓位倍率: {result['position_multiplier']:.0%}")
        print(f"  总结: {result['summary']}")
        print(f"\n  五层分析:")
        for k, v in result.get("layers", {}).items():
            print(f"    {k}: {v}")
        print(f"\n  4个信号:")
        for s in result.get("signal_checks", []):
            emoji = {"pass": "✅", "fail": "❌", "watch": "⚠️"}.get(s["status"], "❓")
            print(f"    {emoji} {s['signal']}: {s['status']}")
    else:
        print("  分析失败（API 不可用）")

    print(f"\n⚙️ 集成转换:")
    adj = get_macro_adjustment(result)
    print(f"  {json.dumps(adj, indent=2, ensure_ascii=False)}")
"""
基本面数据源
- 恐惧贪婪指数 (alternative.me)
- 市场宏观数据 (CoinGecko)
- OKX 资金费率/持仓量 (已有 client)
"""
import asyncio
import logging
import httpx

logger = logging.getLogger("fundamentals")

_TIMEOUT = 10  # 秒


def get_fear_greed() -> dict | None:
    """恐惧贪婪指数。返回 {value: 0-100, classification: str} 或 None"""
    try:
        r = httpx.get("https://api.alternative.me/fng/", timeout=_TIMEOUT)
        d = r.json()
        item = d["data"][0]
        return {
            "value": int(item["value"]),
            "classification": item["value_classification"],
        }
    except Exception as e:
        logger.warning(f"恐惧贪婪指数获取失败: {e}")
        return None


def get_market_overview() -> dict | None:
    """市场宏观数据。返回总市值/成交量/BTC占比/ETH占比"""
    try:
        r = httpx.get("https://api.coingecko.com/api/v3/global", timeout=_TIMEOUT)
        d = r.json()["data"]
        return {
            "total_mcap_t": round(d["total_market_cap"]["usd"] / 1e12, 2),
            "volume_24h_b": round(d["total_volume"]["usd"] / 1e9, 0),
            "btc_dominance": round(d["market_cap_percentage"]["btc"], 1),
            "eth_dominance": round(d["market_cap_percentage"]["eth"], 1),
        }
    except Exception as e:
        logger.warning(f"市场宏观数据获取失败: {e}")
        return None


def get_funding_rate(client, symbol: str) -> float | None:
    """获取永续合约资金费率"""
    try:
        return client.get_funding_rate(symbol + "-USDT-SWAP")
    except Exception:
        return None


def build_fundamental_context(client=None, ccy: str = "") -> str:
    """组装基本面上下文文本，供 AI prompt 使用"""
    parts = []

    fg = get_fear_greed()
    if fg:
        emoji = {"Extreme Fear": "😱", "Fear": "😨", "Neutral": "😐", "Greed": "😊", "Extreme Greed": "🤑"}
        parts.append(
            f"  恐惧贪婪指数: {fg['value']}/100 ({fg['classification']} {emoji.get(fg['classification'], '')})"
        )

    mkt = get_market_overview()
    if mkt:
        parts.append(
            f"  市场总市值: ${mkt['total_mcap_t']}T | 24h成交量: ${mkt['volume_24h_b']}B | "
            f"BTC占比: {mkt['btc_dominance']}% | ETH占比: {mkt['eth_dominance']}%"
        )

    if client and ccy:
        try:
            base = ccy.split("_")[0]
            fr = get_funding_rate(client, base)
            if fr is not None:
                direction = "做多付钱" if fr < 0 else "做空付钱"
                parts.append(f"  {base} 永续资金费率: {fr*100:+.4f}% ({direction})")
        except Exception:
            pass

    if not parts:
        return "[基本面数据暂不可用]"

    return "[基本面层]\n" + "\n".join(parts) + "\n"
