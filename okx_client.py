"""
OKX API 客户端
- 所有 API 调用增加重试 + 指数退避
- get_order 增加最大查询次数限制，超出后抛出 MaxQueryExceeded 避免无限循环
"""
import time
import random
import logging
import functools

import okx.Trade as Trade
import okx.Account as Account
import okx.MarketData as MarketData
import okx.PublicData as PublicData
import config

logger = logging.getLogger("okx_client")

MAX_RETRIES = 3
BASE_DELAY = 1.0
MAX_DELAY = 10.0

# 订单不存在时的最大查询次数（之后不再继续查询）
ORDER_NOT_FOUND_MAX_QUERIES = 5


class MaxQueryExceeded(RuntimeError):
    """查询订单超过最大次数时抛出"""
    pass


def _retry(max_retries=MAX_RETRIES):
    """指数退避重试装饰器"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (RuntimeError, ConnectionError, TimeoutError) as e:
                    last_exc = e
                    msg = str(e).lower()
                    # 500 / timeout / Too Many Requests 才重试
                    if not any(x in msg for x in ["500", "timeout", "busy", "too many", "rate limit", "system"]):
                        raise  # 非限流/超时异常直接抛出
                    if attempt < max_retries:
                        delay = min(BASE_DELAY * (2 ** attempt) + random.uniform(0, 1), MAX_DELAY)
                        logger.warning(f"{func.__name__} 失败 (尝试 {attempt+1}/{max_retries}): {e}，{delay:.1f}s 后重试")
                        time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


class OrderNotFoundTracker:
    """跟踪某个订单的查询次数，防止无限循环"""
    def __init__(self):
        self._counts = {}

    def check(self, order_id: str) -> bool:
        """返回 True 表示还可以继续查询，False 表示超过上限"""
        # 防止长跑累积：超过 1000 个条目时清理已完成的（只保留仍在查询中的）
        if len(self._counts) > 1000:
            self._counts = {k: v for k, v in self._counts.items()
                           if v <= ORDER_NOT_FOUND_MAX_QUERIES}
        n = self._counts.get(order_id, 0) + 1
        self._counts[order_id] = n
        return n <= ORDER_NOT_FOUND_MAX_QUERIES

    def reset(self, order_id: str):
        self._counts.pop(order_id, None)


# 全局跟踪器，get_order_not_found_safe 使用
_order_not_found_tracker = OrderNotFoundTracker()


class OKXClient:
    def __init__(self, symbol=None, flag=None, market_flag=None):
        self.symbol = symbol or config.SYMBOL
        self.flag = flag if flag is not None else config.FLAG
        # market_flag 独立控制行情 API（模拟盘某些币数据异常时用实盘取价）
        self.market_flag = market_flag if market_flag is not None else self.flag
        args = dict(
            api_key=config.API_KEY,
            api_secret_key=config.SECRET_KEY,
            passphrase=config.PASSPHRASE,
            flag=self.flag,
        )
        self.trade = Trade.TradeAPI(**args)
        self.account = Account.AccountAPI(**args)
        self.market = MarketData.MarketAPI(flag=self.market_flag)
        self.public = PublicData.PublicAPI(flag=self.market_flag)

    # ── 公开 API 方法（全部带重试） ─────────────────────────────

    @_retry()
    def get_candles(self, symbol=None, bar=None, limit=None):
        symbol = symbol or self.symbol
        bar = bar or config.CANDLE_BAR
        limit = str(limit or config.CANDLE_LIMIT)
        result = self.market.get_candlesticks(instId=symbol, bar=bar, limit=limit)
        if result["code"] != "0":
            raise RuntimeError(f"get_candles error: {result['msg']}")
        return result["data"]

    def get_history_candles(self, bar=None, total=2000, symbol=None):
        """分页拉取历史 K 线（用于 ML 训练），按时间升序返回原始行。
        OKX 历史接口每页最多 100 根，向更早方向翻页。"""
        bar = bar or config.CANDLE_BAR
        symbol = symbol or self.symbol
        out = []
        after = None
        while len(out) < total:
            params = dict(instId=symbol, bar=bar, limit="100")
            if after:
                params["after"] = after
            try:
                result = self.market.get_history_candlesticks(**params)
            except Exception as e:
                logger.warning(f"get_history_candles 翻页失败: {e}")
                break
            if result.get("code") != "0":
                logger.warning(f"get_history_candles error: {result.get('msg')}")
                break
            data = result.get("data", [])
            if not data:
                break
            out.extend(data)
            after = data[-1][0]   # 本页最早一根的 ts，下一页取更早的
            if len(data) < 100:
                break
            time.sleep(0.12)      # 礼貌限频
        return out

    @_retry()
    def get_ticker(self, symbol=None):
        symbol = symbol or self.symbol
        result = self.market.get_ticker(instId=symbol)
        if result["code"] != "0":
            raise RuntimeError(f"get_ticker error: {result['msg']}")
        return result["data"][0]

    @_retry()
    def get_balance(self, ccy="USDT"):
        result = self.account.get_account_balance(ccy=ccy)
        if result["code"] != "0":
            raise RuntimeError(f"get_balance error: {result['msg']}")
        details = result["data"][0]["details"]
        for item in details:
            if item["ccy"] == ccy:
                return float(item["availBal"])
        return 0.0

    @_retry()
    def get_available_balance(self, ccy):
        result = self.account.get_account_balance(ccy=ccy)
        if result["code"] != "0":
            raise RuntimeError(f"get_available_balance error: {result['msg']}")
        details = result["data"][0]["details"]
        for item in details:
            if item["ccy"] == ccy:
                return float(item["availBal"])
        return 0.0

    @_retry()
    def get_currency_details(self, ccy):
        result = self.account.get_account_balance(ccy=ccy)
        if result["code"] != "0":
            raise RuntimeError(f"get_currency_details error: {result['msg']}")
        details = result["data"][0]["details"]
        for item in details:
            if item["ccy"] == ccy:
                return item
        return {}

    @_retry()
    def get_avg_cost(self, symbol=None, limit=100, max_pages=5):
        symbol = symbol or self.symbol
        # 分页拉取成交记录（每页最多 100 条），避免长期持仓成本基准被截断
        all_fills = []
        after = None
        for _ in range(max_pages):
            params = dict(instId=symbol, limit=str(limit))
            if after:
                params["after"] = after
            result = self.trade.get_fills(**params)
            if result["code"] != "0":
                break
            page = result["data"]
            if not page:
                break
            all_fills.extend(page)
            if len(page) < int(limit):
                break
            after = page[-1].get("billId")
            if not after:
                break
        if not all_fills:
            return 0.0
        # OKX 返回按时间倒序（新→旧），结算需正序（旧→新）
        fills = list(reversed(all_fills))
        position = 0.0
        total_cost = 0.0
        for f in fills:
            sz = float(f["fillSz"])
            px = float(f["fillPx"])
            if f["side"] == "buy":
                total_cost += sz * px
                position += sz
            else:
                if position > 0:
                    avg = total_cost / position
                    sold = min(sz, position)
                    total_cost -= sold * avg
                    position -= sold
                    if position <= 0:
                        position = 0.0
                        total_cost = 0.0
        return round(total_cost / position, 6) if position > 0 else 0.0

    @_retry()
    def place_order(self, side, price, size, order_type="limit"):
        params = dict(
            instId=self.symbol,
            tdMode="cash",
            side=side,
            ordType=order_type,
            sz=str(size),
        )
        if order_type == "limit":
            params["px"] = str(price)
        elif order_type == "market" and side == "buy":
            params["tgtCcy"] = "base_ccy"
        result = self.trade.place_order(**params)
        if result["code"] != "0":
            msg = result.get("data", [{}])[0].get("sMsg") or result.get("msg", str(result))
            raise RuntimeError(f"place_order error: {msg}")
        return result["data"][0]["ordId"]

    @_retry()
    def cancel_order(self, order_id):
        result = self.trade.cancel_order(instId=self.symbol, ordId=order_id)
        if result["code"] != "0":
            raise RuntimeError(f"cancel_order error: {result['msg']}")

    @_retry(max_retries=2)
    def get_order(self, order_id):
        """
        查询订单状态。
        如果返回 "Order does not exist" 超过 ORDER_NOT_FOUND_MAX_QUERIES 次，
        抛出 MaxQueryExceeded。
        """
        result = self.trade.get_order(instId=self.symbol, ordId=order_id)
        if result["code"] != "0":
            msg = result["msg"].lower()
            if "not exist" in msg or "not found" in msg or "does not exist" in msg:
                if _order_not_found_tracker.check(order_id):
                    raise RuntimeError(f"get_order error: {result['msg']}")
                else:
                    raise MaxQueryExceeded(
                        f"订单 {order_id} 已查询 {ORDER_NOT_FOUND_MAX_QUERIES} 次仍不存在，视为已完成"
                    )
            raise RuntimeError(f"get_order error: {result['msg']}")
        # 查询成功，重置计数器
        _order_not_found_tracker.reset(order_id)
        return result["data"][0]

    @_retry()
    def get_pending_orders(self):
        result = self.trade.get_order_list(instId=self.symbol)
        if result["code"] != "0":
            raise RuntimeError(f"get_pending_orders error: {result['msg']}")
        return result["data"]

    @_retry()
    def get_spot_position(self, base_ccy: str) -> float:
        result = self.account.get_account_balance(ccy=base_ccy)
        if result["code"] != "0":
            return 0.0
        details = result["data"][0]["details"]
        for item in details:
            if item["ccy"] == base_ccy:
                return float(item.get("availBal", item.get("cashBal", 0)))
        return 0.0

    @_retry()
    def place_algo_order(self, side: str, size: float, stop_px: float) -> str:
        """挂交易所侧条件止损单（触发后市价平仓）。
        现货用 cash 模式；永续用合约保证金模式并设 reduceOnly 仅减仓。
        """
        is_swap = "SWAP" in self.symbol
        params = dict(
            instId=self.symbol,
            tdMode=config.FUTURES_MARGIN_MODE if is_swap else "cash",
            side=side,
            ordType="conditional",
            sz=str(size),
            slTriggerPx=str(stop_px),
            slOrdPx="-1",
            slTriggerPxType="last",
        )
        if is_swap:
            params["reduceOnly"] = "true"
        result = self.trade.place_algo_order(**params)
        if result["code"] != "0":
            msg = result.get("data", [{}])[0].get("sMsg") or result.get("msg", str(result))
            raise RuntimeError(f"place_algo_order error: {msg}")
        return result["data"][0]["algoId"]

    def cancel_algo_orders(self):
        try:
            result = self.trade.order_algos_list(ordType="conditional", instId=self.symbol)
        except AttributeError:
            return
        if result["code"] != "0":
            return
        for o in result["data"]:
            try:
                self.trade.cancel_algo_order([{"instId": self.symbol, "algoId": o["algoId"]}])
            except Exception:
                pass

    @_retry()
    def cancel_all_orders(self):
        orders = self.get_pending_orders()
        errors = []
        for o in orders:
            try:
                self.cancel_order(o["ordId"])
            except Exception as e:
                errors.append(str(e))
        if errors:
            raise RuntimeError(f"部分撤单失败: {errors}")

    # ── 合约（永续）专用方法 ───────────────────────────────────

    @_retry()
    def get_ct_val(self):
        result = self.public.get_instruments(instType="SWAP", instId=self.symbol)
        if result["code"] == "0" and result["data"]:
            return float(result["data"][0].get("ctVal", 1))
        for cfg in config.COIN_CONFIG.values():
            if cfg.get("symbol") == self.symbol:
                return cfg.get("ct_val", 1.0)
        return 1.0

    @_retry()
    def set_leverage(self, lever=None, mgnMode=None):
        if lever is None:
            # self.symbol 是交易对符号(如 TRX-USDT-SWAP)，但 get_leverage 的键是币种键
            # (如 TRX_SWAP)。按 symbol 反查币种键，避免每次退回默认杠杆、忽略 per-coin 配置。
            lever = next(
                (config.get_leverage(k) for k, v in config.COIN_CONFIG.items()
                 if v.get("symbol") == self.symbol),
                config.FUTURES_DEFAULT_LEVERAGE,
            )
        lever = str(lever)
        mgnMode = mgnMode or config.FUTURES_MARGIN_MODE
        result = self.account.set_leverage(instId=self.symbol, lever=lever, mgnMode=mgnMode)
        if result["code"] != "0":
            raise RuntimeError(f"set_leverage error: {result['msg']}")

    @_retry()
    def place_futures_order(self, side, size, order_type="market", price=None, reduce_only=False):
        params = dict(
            instId=self.symbol,
            tdMode=config.FUTURES_MARGIN_MODE,
            side=side,
            ordType=order_type,
            sz=str(size),
        )
        if order_type == "limit" and price is not None:
            params["px"] = str(price)
        # 平仓单设 reduceOnly：仅减仓不反向开仓。防止止损单已触发后市价平仓
        # 反而开出反方向新仓（net 模式下尤其危险）。
        if reduce_only:
            params["reduceOnly"] = "true"
        result = self.trade.place_order(**params)
        if result["code"] != "0":
            msg = result.get("data", [{}])[0].get("sMsg") or result.get("msg", str(result))
            raise RuntimeError(f"place_futures_order error: {msg}")
        return result["data"][0]["ordId"]

    @_retry()
    def get_futures_position(self):
        result = self.account.get_positions(instType="SWAP", instId=self.symbol)
        if result["code"] != "0":
            raise RuntimeError(f"get_futures_position error: {result['msg']}")
        data = result["data"]
        return data[0] if data else {}

    @_retry()
    def close_futures_position(self):
        pos = self.get_futures_position()
        if not pos:
            return None
        params = dict(
            instId=self.symbol,
            mgnMode=config.FUTURES_MARGIN_MODE,
            autoCxl="true",
        )
        pos_side = pos.get("posSide", "net")
        if pos_side in ("long", "short"):
            params["posSide"] = pos_side
        result = self.trade.close_positions(**params)
        if result["code"] != "0":
            raise RuntimeError(f"close_futures_position error: {result['msg']}")
        return result["data"]

    @_retry()
    def get_funding_rate(self):
        result = self.public.get_funding_rate(instId=self.symbol)
        if result["code"] != "0":
            return {}
        return result["data"][0] if result["data"] else {}

    @_retry()
    def get_futures_fills(self, limit=30):
        result = self.trade.get_fills(instId=self.symbol, limit=str(limit))
        if result["code"] != "0":
            return []
        return result["data"]
