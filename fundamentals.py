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
