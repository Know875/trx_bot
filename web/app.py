"""FastAPI dashboard — 支持 TRX / ETH / SOL 多币种"""
import sys, os, time, secrets
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from okx_client import OKXClient
from market_analyzer import parse_candles, detect_regime, calc_indicators
from tracker import Tracker

STATIC  = Path(__file__).parent / "static"
BOT_DIR = Path(__file__).parent.parent

app = FastAPI(title="Multi-Coin Bot Dashboard")
# 仅允许本地和同源请求，敏感接口需要认证
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8090", "http://127.0.0.1:8090"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# ── 认证中间件 ─────────────────────────────────────────────────
_USERNAME = getattr(config, "DASHBOARD_USERNAME", "admin")
_PASSWORD = getattr(config, "DASHBOARD_PASSWORD", "admin123")
# 简单 session token（重启后失效，够用）。value = 过期时间戳
_SESSION_TOKENS: dict[str, float] = {}

# 使用默认弱口令时告警（GET 接口默认放行，泄露账户财务状态风险高）
if _PASSWORD == "admin123":
    import logging as _logging
    _logging.getLogger("dashboard").warning(
        "⚠️ Dashboard 仍在使用默认密码 admin123，请设置环境变量 DASHBOARD_PASSWORD"
    )

# 受保护的路径前缀（GET 只读接口除外，避免 dashboard 加载卡死）
_PROTECTED_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """对写操作（POST/DELETE 等）和敏感 GET 接口强制认证。
    静态资源和普通 GET（status/stats/candles 等）无需认证，方便只读监控。
    """
    method = request.method.upper()
    path = request.url.path

    # 静态资源、WebSocket、根路径跳过
    if path.startswith("/static") or path == "/ws" or path == "/":
        return await call_next(request)

    # GET 请求默认放行（只读监控），但 logs 路径需要认证（泄露策略信息）
    if method == "GET" and not path.startswith("/api/logs"):
        return await call_next(request)

    # 需要认证：检查 Authorization header 或 token cookie
    auth = request.headers.get("Authorization", "")
    token = request.cookies.get("dashboard_token", "")
    valid = False

    if auth.startswith("Basic "):
        import base64
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, _, pwd = decoded.partition(":")
            if secrets.compare_digest(user, _USERNAME) and secrets.compare_digest(pwd, _PASSWORD):
                valid = True
        except Exception:
            pass
    elif token:
        expire = _SESSION_TOKENS.get(token)
        if expire and time.time() < expire:
            valid = True
        elif expire:
            # 已过期 → 清理
            _SESSION_TOKENS.pop(token, None)

    if not valid:
        return JSONResponse(
            status_code=401,
            content={"detail": "需要登录", "need_auth": True},
            headers={"WWW-Authenticate": 'Basic realm="Dashboard"'},
        )

    return await call_next(request)


@app.post("/api/auth/login")
async def login(request: Request):
    """登录接口，返回 session token"""
    import base64
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        raise HTTPException(status_code=401, detail="需要 Basic Auth")
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, _, pwd = decoded.partition(":")
        if secrets.compare_digest(user, _USERNAME) and secrets.compare_digest(pwd, _PASSWORD):
            token = secrets.token_hex(32)
            _SESSION_TOKENS[token] = time.time() + 86400  # 24h 有效
            return {"ok": True, "token": token}
    except Exception:
        pass
    raise HTTPException(status_code=401, detail="用户名或密码错误")


def _cfg(ccy: str) -> dict:
    return config.COIN_CONFIG.get(ccy, config.COIN_CONFIG["TRX"])

# ── OKXClient 连接池：复用 HTTP 连接，避免每次请求重建 ──────────
_CLIENTS: dict = {}

def _client(ccy: str) -> OKXClient:
    if ccy not in _CLIENTS:
        _CLIENTS[ccy] = OKXClient(symbol=_cfg(ccy)["symbol"])
    return _CLIENTS[ccy]

# ── TTL 缓存：减少重复 OKX API 调用 ────────────────────────────
_CACHE: dict = {}   # key → (value, expire_timestamp)

def _cache_get(key: str):
    entry = _CACHE.get(key)
    if entry and time.time() < entry[1]:
        return entry[0], True
    return None, False

def _cache_set(key: str, value, ttl: float):
    _CACHE[key] = (value, time.time() + ttl)

def _cache_del(*prefixes: str):
    for k in list(_CACHE.keys()):
        if any(k.startswith(p) for p in prefixes):
            del _CACHE[k]

def _tracker(ccy: str) -> Tracker:
    return Tracker(_cfg(ccy)["initial_capital"], ccy=ccy)

def _running_hours(tracker) -> float:
    """从首笔交易时间计算运行小时数"""
    records = [r for r in tracker.records if r["strategy"] != "cleanup"]
    if not records:
        return 0.0
    try:
        from datetime import datetime
        first = datetime.strptime(records[0]["time"], "%Y-%m-%d %H:%M:%S")
        return round((datetime.now() - first).total_seconds() / 3600, 1)
    except Exception:
        return 0.0

def _log_file(ccy: str) -> Path:
    p = BOT_DIR / f"bot_{ccy}.log"
    return p if p.exists() else BOT_DIR / "bot.log"


@app.get("/")
async def root():
    return FileResponse(str(STATIC / "index.html"))


@app.get("/api/status")
async def get_status():
    latest = 0.0
    for ccy in config.COINS:
        lf = _log_file(ccy)
        if lf.exists():
            latest = max(latest, lf.stat().st_mtime)
    if latest:
        return {"running": time.time() - latest < 960, "last_active": int(latest)}
    return {"running": False, "last_active": None}


def _tracker_stats(tracker, initial):
    trades   = [r for r in tracker.records if r["strategy"] != "cleanup"]
    cleanups = [r for r in tracker.records if r["strategy"] == "cleanup"]
    wins     = [r for r in trades if r["pnl"] > 0]
    best     = max((r["pnl"] for r in trades), default=0.0)
    worst    = min((r["pnl"] for r in trades), default=0.0)
    avg_t    = sum(r["pnl"] for r in trades) / len(trades) if trades else 0.0
    return {
        "initial_capital":  initial,
        "realized_pnl":     round(tracker.realized_pnl, 4),
        "trade_count":      len(trades),
        "cleanup_count":    len(cleanups),
        "win_count":        len(wins),
        "loss_count":       len(trades) - len(wins),
        "win_rate":         round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "best_trade":       round(best, 4),
        "worst_trade":      round(worst, 4),
        "avg_trade":        round(avg_t, 4),
    }


@app.get("/api/stats")
async def get_stats(ccy: str = Query("TRX")):
    key = f"stats:{ccy}"
    cached, hit = _cache_get(key)
    if hit:
        return cached

    cfg     = _cfg(ccy)
    tracker = _tracker(ccy)
    initial = cfg["initial_capital"]
    base    = _tracker_stats(tracker, initial)

    # ── 合约模式 ──────────────────────────────────────────────
    if cfg.get("mode") == "futures":
        balance = upl = contracts = avg_px = liq_px = margin = 0.0
        lever   = config.FUTURES_LEVERAGE
        pos_side = ""
        price    = 0.0
        dd       = 0.0
        try:
            client  = _client(ccy)
            ticker  = client.get_ticker()
            price   = float(ticker["last"])
            balance = client.get_balance("USDT")
            pos     = client.get_futures_position()
            dd      = tracker.drawdown(balance)
            if pos:
                contracts = float(pos.get("pos", 0))
                avg_px    = float(pos.get("avgPx") or 0)
                liq_px    = float(pos.get("liqPx") or 0)
                margin    = float(pos.get("margin") or 0)
                lever     = int(float(pos.get("lever") or config.FUTURES_LEVERAGE))
                pos_side  = pos.get("posSide", "net")
                # contracts=0 说明无实际仓位，OKX 可能短暂保留 upl 旧值，强制清零
                upl       = float(pos.get("upl") or 0) if contracts != 0 else 0.0
        except Exception:
            pass

        total_pnl    = round(tracker.realized_pnl + upl, 4)
        total_return = round(total_pnl / initial * 100, 4) if initial else 0.0
        result = {
            **base,
            "ccy":              ccy,
            "mode":             "futures",
            "balance":          round(balance, 4),
            "price":            price,
            "contracts":        contracts,
            "pos_side":         pos_side,
            "coin_avg_px":      round(avg_px, 6),
            "liq_px":           round(liq_px, 6),
            "margin":           round(margin, 4),
            "lever":            lever,
            "unrealized_pnl":   round(upl, 4),
            "total_pnl":        total_pnl,
            "total_return_pct": total_return,
            "drawdown":         round(dd * 100, 2),
        }
        _cache_set(key, result, ttl=8)
        return result

    # ── 现货模式 ──────────────────────────────────────────────
    price = balance = coin_avail = 0.0
    dd    = 0.0
    try:
        client     = _client(ccy)
        ticker     = client.get_ticker()
        price      = float(ticker["last"])
        balance    = client.get_balance("USDT")
        coin_avail = client.get_available_balance(ccy)
        dd         = tracker.drawdown(balance)
    except Exception:
        pass

    coin_total = coin_avail
    avg_px     = 0.0
    try:
        detail     = client.get_currency_details(ccy)
        coin_total = float(detail.get("cashBal") or coin_avail)
        avg_px     = float(detail.get("avgPx") or 0)
        if avg_px == 0 and coin_total > 0:
            avg_px = client.get_avg_cost()
    except Exception:
        pass

    coin_value     = round(coin_total * price, 4)
    unrealized_pnl = round(coin_total * (price - avg_px), 4) if coin_total > 0 and avg_px > 0 else 0.0
    total_pnl      = round(tracker.realized_pnl + unrealized_pnl, 4)
    total_return   = round(total_pnl / initial * 100, 4) if initial else 0.0

    result = {
        **base,
        "ccy":              ccy,
        "mode":             "spot",
        "balance":          round(balance, 4),
        "price":            price,
        "coin_avail":       round(coin_avail, 4),
        "coin_total":       round(coin_total, 4),
        "coin_avg_px":      round(avg_px, 6),
        "coin_value":       coin_value,
        "total_equity":     round(balance + coin_value, 4),
        "unrealized_pnl":   unrealized_pnl,
        "total_pnl":        total_pnl,
        "total_return_pct": total_return,
        "drawdown":         round(dd * 100, 2),
    }
    _cache_set(key, result, ttl=8)
    return result


@app.get("/api/trades")
async def get_trades(ccy: str = Query("TRX")):
    tracker = _tracker(ccy)
    return {"records": list(reversed(tracker.records))}


@app.get("/api/candles")
async def get_candles(ccy: str = Query("TRX")):
    key = f"candles:{ccy}"
    cached, hit = _cache_get(key)
    if hit:
        return cached
    try:
        client = _client(ccy)
        raw    = client.get_candles()
        df     = parse_candles(raw)
        ind    = calc_indicators(df)
        regime, _ = detect_regime(df)

        close  = df["close"]
        ema20s = close.ewm(span=20, adjust=False).mean()
        ema60s = close.ewm(span=60, adjust=False).mean()

        candles = [
            {
                "time":   int(row["ts"].timestamp()),
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": float(row["vol"]),
                "ema20":  round(float(ema20s.iloc[i]), 8),
                "ema60":  round(float(ema60s.iloc[i]), 8),
            }
            for i, (_, row) in enumerate(df.iterrows())
        ]
        result = {
            "candles": candles,
            "regime":  regime,
            "indicators": {
                k: round(float(v), 6) if isinstance(v, (int, float)) else v
                for k, v in ind.items()
            },
        }
        _cache_set(key, result, ttl=30)
        return result
    except Exception as e:
        return {"error": str(e), "candles": [], "regime": "wait", "indicators": {}}


@app.get("/api/orders")
async def get_orders(ccy: str = Query("TRX")):
    key = f"orders:{ccy}"
    cached, hit = _cache_get(key)
    if hit:
        return cached
    try:
        client    = _client(ccy)
        orders    = client.get_pending_orders()
        grid_step = config.GRID_RANGE_PCT / config.GRID_COUNT
        for o in orders:
            if o.get("side") == "buy" and o.get("px"):
                o["est_sell_price"] = round(float(o["px"]) * (1 + grid_step), 8)
            else:
                o["est_sell_price"] = None
        result = {"orders": orders}
        _cache_set(key, result, ttl=5)
        return result
    except Exception as e:
        return {"error": str(e), "orders": []}


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str, ccy: str = Query("TRX")):
    try:
        _client(ccy).cancel_order(order_id)
        _cache_del(f"orders:{ccy}", f"stats:{ccy}")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/orders")
async def cancel_all_orders(ccy: str = Query("TRX")):
    try:
        _client(ccy).cancel_all_orders()
        _cache_del(f"orders:{ccy}", f"stats:{ccy}")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/position")
async def get_position(ccy: str = Query("TRX")):
    key = f"position:{ccy}"
    cached, hit = _cache_get(key)
    if hit:
        return cached
    try:
        client = _client(ccy)
        ticker = client.get_ticker()
        price  = float(ticker["last"])
        avail  = client.get_available_balance(ccy)
        detail = client.get_currency_details(ccy)
        total  = float(detail.get("cashBal") or avail)
        avg_px = float(detail.get("avgPx") or 0)
        if avg_px == 0 and total > 0:
            avg_px = client.get_avg_cost()
        unrealized = round(total * (price - avg_px), 4) if total > 0 and avg_px > 0 else 0.0
        result = {
            "ccy":            ccy,
            "avail":          round(avail, 6),
            "total":          round(total, 6),
            "avg_px":         round(avg_px, 6),
            "price":          price,
            "value":          round(total * price, 4),
            "unrealized_pnl": unrealized,
        }
        _cache_set(key, result, ttl=8)
        return result
    except Exception as e:
        return {"error": str(e), "ccy": ccy, "avail": 0, "total": 0,
                "avg_px": 0, "price": 0, "value": 0, "unrealized_pnl": 0}


@app.post("/api/position/close")
async def close_position(ccy: str = Query("TRX")):
    _cache_del(f"position:{ccy}", f"stats:{ccy}")
    cfg = _cfg(ccy)
    try:
        client   = _client(ccy)
        avail    = client.get_available_balance(ccy)
        min_size = cfg["min_order_size"]
        if avail < min_size:
            raise HTTPException(status_code=400, detail=f"可用 {ccy} 不足 {min_size}，当前={avail}")
        dec = cfg["size_decimals"]
        if dec == 0:
            import math
            size = math.floor(avail)
        else:
            import math
            factor = 10 ** dec
            size = math.floor(avail * factor) / factor
        ticker = client.get_ticker()
        price  = float(ticker["last"])
        order_id = client.place_order("sell", price, size, order_type="market")
        return {"ok": True, "order_id": order_id, "size": size, "price": price}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/logs")
async def get_logs(lines: int = 80, ccy: str = Query("TRX")):
    lf = _log_file(ccy)
    if not lf.exists():
        return {"lines": []}
    with open(lf, "r", errors="replace") as f:
        all_lines = f.readlines()
    return {"lines": [ln.rstrip() for ln in all_lines[-lines:]]}


@app.get("/api/futures/position")
async def get_futures_position(ccy: str = Query("TRX_SWAP")):
    key = f"fpos:{ccy}"
    cached, hit = _cache_get(key)
    if hit:
        return cached
    try:
        client = _client(ccy)
        ticker = client.get_ticker()
        price  = float(ticker["last"])
        pos    = client.get_futures_position()
        if not pos:
            result = {
                "ccy": ccy, "contracts": 0, "pos_side": "", "avg_px": 0,
                "price": price, "upl": 0, "liq_px": 0, "margin": 0,
                "lever": config.FUTURES_LEVERAGE,
            }
        else:
            result = {
                "ccy":       ccy,
                "contracts": float(pos.get("pos", 0)),
                "pos_side":  pos.get("posSide", "net"),
                "avg_px":    round(float(pos.get("avgPx") or 0), 6),
                "price":     price,
                "upl":       round(float(pos.get("upl") or 0), 4),
                "liq_px":    round(float(pos.get("liqPx") or 0), 6),
                "margin":    round(float(pos.get("margin") or 0), 4),
                "lever":     int(float(pos.get("lever") or config.FUTURES_LEVERAGE)),
            }
        _cache_set(key, result, ttl=8)
        return result
    except Exception as e:
        return {
            "error": str(e), "ccy": ccy, "contracts": 0, "pos_side": "",
            "avg_px": 0, "price": 0, "upl": 0, "liq_px": 0, "margin": 0,
            "lever": config.FUTURES_LEVERAGE,
        }


@app.post("/api/futures/position/close")
async def close_futures_position(ccy: str = Query("TRX_SWAP")):
    _cache_del(f"fpos:{ccy}", f"stats:{ccy}")
    try:
        client = _client(ccy)
        result = client.close_futures_position()
        if result is None:
            raise HTTPException(status_code=400, detail="当前无合约持仓")
        return {"ok": True, "detail": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/futures/order")
async def place_futures_order(
    ccy:        str   = Query("TRX_SWAP"),
    side:       str   = Query(...),          # "buy" | "sell"
    size:       float = Query(...),          # 张数
    order_type: str   = Query("market"),     # "market" | "limit"
    price:      float = Query(None),
):
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side 必须为 buy 或 sell")
    if size <= 0:
        raise HTTPException(status_code=400, detail="size 必须大于 0")
    try:
        client   = _client(ccy)
        order_id = client.place_futures_order(
            side=side,
            size=int(size),
            order_type=order_type,
            price=price,
        )
        return {"ok": True, "order_id": order_id, "side": side, "size": int(size)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/futures/leverage")
async def set_futures_leverage(
    ccy:      str = Query("TRX_SWAP"),
    lever:    int = Query(...),
    mgn_mode: str = Query("isolated"),
):
    if not (1 <= lever <= 125):
        raise HTTPException(status_code=400, detail="杠杆范围 1~125")
    try:
        _client(ccy).set_leverage(lever=lever, mgnMode=mgn_mode)
        return {"ok": True, "lever": lever, "mgn_mode": mgn_mode}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/futures/funding")
async def get_funding_rate(ccy: str = Query("TRX_SWAP")):
    key = f"funding:{ccy}"
    cached, hit = _cache_get(key)
    if hit:
        return cached
    try:
        data = _client(ccy).get_funding_rate()
        def _safe_float(v, default=0.0):
            try: return float(v) if v not in (None, "") else default
            except (ValueError, TypeError): return default
        rate      = _safe_float(data.get("fundingRate", 0))
        next_rate = _safe_float(data.get("nextFundingRate", 0))
        fund_time = int(data.get("fundingTime", 0))
        result = {
            "funding_rate":      round(rate * 100, 6),
            "next_funding_rate": round(next_rate * 100, 6),
            "funding_time":      fund_time,
        }
        _cache_set(key, result, ttl=60)
        return result
    except Exception as e:
        return {"error": str(e), "funding_rate": 0, "next_funding_rate": 0, "funding_time": 0}


@app.get("/api/futures/fills")
async def get_futures_fills(ccy: str = Query("TRX_SWAP"), limit: int = Query(30)):
    key = f"fills:{ccy}:{limit}"
    cached, hit = _cache_get(key)
    if hit:
        return cached
    try:
        fills = _client(ccy).get_futures_fills(limit=limit)
        result = []
        for f in fills:
            result.append({
                "trade_id":  f.get("tradeId", ""),
                "side":      f.get("side", ""),
                "fill_px":   float(f.get("fillPx", 0)),
                "fill_sz":   float(f.get("fillSz", 0)),
                "pnl":       float(f.get("pnl", 0)),
                "fee":       float(f.get("fee", 0)),
                "fee_ccy":   f.get("feeCcy", ""),
                "ts":        int(f.get("ts", 0)),
            })
        _cache_set(key, {"fills": result}, ttl=8)
        return {"fills": result}
    except Exception as e:
        return {"error": str(e), "fills": []}


@app.get("/api/futures/overview")
async def get_futures_overview():
    """一次返回所有合约品种的持仓摘要"""
    cached, hit = _cache_get("futures_overview")
    if hit:
        return cached
    result = {}
    for ccy in ("TRX_SWAP", "ETH_SWAP", "SOL_SWAP"):
        try:
            client = _client(ccy)
            ticker = client.get_ticker()
            price  = float(ticker["last"])
            pos    = client.get_futures_position()
            result[ccy] = {
                "price":     price,
                "contracts": float(pos.get("pos", 0)) if pos else 0,
                "pos_side":  pos.get("posSide", "") if pos else "",
                "upl":       round(float(pos.get("upl", 0)), 4) if pos else 0,
                "liq_px":    round(float(pos.get("liqPx", 0)), 6) if pos else 0,
                "lever":     int(float(pos.get("lever", config.FUTURES_LEVERAGE))) if pos else config.FUTURES_LEVERAGE,
            }
        except Exception:
            result[ccy] = {"price": 0, "contracts": 0, "pos_side": "", "upl": 0, "liq_px": 0, "lever": 0}
    _cache_set("futures_overview", result, ttl=8)
    return result


@app.get("/api/overview")
async def get_overview():
    """全币种总览：一次返回所有币种的关键指标"""
    cached, hit = _cache_get("overview")
    if hit:
        return cached

    result = {}
    for ccy in config.COINS:
        cfg = _cfg(ccy)
        tracker = _tracker(ccy)
        trades = [r for r in tracker.records if r["strategy"] != "cleanup"]
        wins   = [r for r in trades if r["pnl"] > 0]
        win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0

        entry = {
            "ccy":              ccy,
            "mode":             cfg.get("mode", "spot"),
            "initial_capital":  cfg["initial_capital"],
            "realized_pnl":     round(tracker.realized_pnl, 4),
            "trade_count":      len(trades),
            "win_rate":         win_rate,
            "max_drawdown":     round(tracker.max_drawdown * 100, 2),
            "total_return_pct": round(tracker.realized_pnl / cfg["initial_capital"] * 100, 2) if cfg["initial_capital"] else 0,
            "running_hours":    _running_hours(tracker),
            "price":            0.0,
            "change24h":        0.0,
            "regime":           "wait",
            "balance":          0.0,
            "position":         None,
        }

        try:
            client = _client(ccy)
            ticker = client.get_ticker()
            price = float(ticker["last"])
            open24h = float(ticker.get("open24h") or price)
            entry["price"] = price
            entry["change24h"] = round((price - open24h) / open24h * 100, 3) if open24h else 0.0

            if cfg.get("mode") == "futures":
                entry["balance"] = client.get_balance("USDT")
                pos = client.get_futures_position()
                if pos and float(pos.get("pos", 0)) != 0:
                    entry["position"] = {
                        "contracts": float(pos.get("pos", 0)),
                        "pos_side":  pos.get("posSide", ""),
                        "upl":       round(float(pos.get("upl", 0)), 4),
                        "liq_px":    round(float(pos.get("liqPx", 0)), 6),
                        "lever":     int(float(pos.get("lever", config.FUTURES_LEVERAGE))),
                    }
            else:
                coin_avail = client.get_available_balance(ccy)
                detail = client.get_currency_details(ccy)
                coin_total = float(detail.get("cashBal") or coin_avail)
                entry["balance"] = client.get_balance("USDT")
                entry["position"] = {
                    "total": round(coin_total, 6),
                    "avail": round(coin_avail, 6),
                    "value": round(coin_total * price, 4),
                }

            # 行情判定（轻量：只看最新 candle 组缓存）
            raw = client.get_candles()
            df = parse_candles(raw)
            regime, _ = detect_regime(df)
            entry["regime"] = regime
        except Exception:
            pass

        result[ccy] = entry

    _cache_set("overview", result, ttl=15)
    return result


async def _fetch_one_ticker(ccy: str, client: OKXClient):
    try:
        t = await asyncio.wait_for(asyncio.to_thread(client.get_ticker), timeout=5)
        last     = float(t["last"])
        open24h  = float(t.get("open24h") or last)
        change24h = round((last - open24h) / open24h * 100, 3) if open24h else 0.0
        return ccy, {
            "price":     last,
            "change24h": change24h,
            "high24h":   float(t.get("high24h", 0)),
            "low24h":    float(t.get("low24h", 0)),
            "vol24h":    float(t.get("volCcy24h", 0)),
        }
    except Exception:
        return ccy, None


# ═══════════════════════════════════════════════════════════════
# 系统状态 API — guard / lock / strategy / audit
# ═══════════════════════════════════════════════════════════════

@app.get("/api/system/status")
async def get_system_status():
    """统一系统状态：账户防护 + 进化锁 + 策略守卫 + 审计 + 心跳"""
    result = {
        "bot_running": False,
        "bot_process": "stopped",
        "last_heartbeat_sec": 9999,
        "account_guard": {"status": "unknown", "daily_pnl": 0, "daily_pnl_pct": 0},
        "evolution_lock": {"locked": False, "reason": "ok", "manual_lock": False},
        "strategy_guard": {},
        "extreme_market": {"is_extreme": False, "reasons": []},
        "audit_24h": {"total": 0, "rollback_ok": 0, "blocked": 0, "non_rollback_ok": 0, "status": "safe"},
        "coins_cooldown": {},
    }
    try:
        # Bot running + heartbeat
        latest_log = 0.0
        for ccy in config.COINS:
            lf = _log_file(ccy)
            if lf.exists():
                latest_log = max(latest_log, lf.stat().st_mtime)
        result["bot_running"] = time.time() - latest_log < 960 if latest_log else False

        hb_path = BOT_DIR / ".bot_heartbeat"
        if hb_path.exists():
            hb_sec = int(time.time() - hb_path.stat().st_mtime)
            result["last_heartbeat_sec"] = hb_sec
            if hb_sec < 90:
                result["bot_process"] = "running"
            elif hb_sec < 180:
                result["bot_process"] = "stale"
            else:
                result["bot_process"] = "stopped"
        else:
            result["bot_process"] = "stopped"
            result["last_heartbeat_sec"] = 9999
    except Exception:
        pass

    # Account guard
    try:
        from account_guard import Guard
        # 与实盘 guard 统一口径：用 config.TOTAL_CAPITAL（支持 .env 的 TOTAL_CAPITAL 覆盖）
        g = Guard()
        status = g.status_report()
        result["account_guard"] = {
            "status": status.get("status", "unknown"),
            "daily_pnl": status.get("daily_pnl", 0),
            "daily_pnl_pct": status.get("daily_pnl_pct", 0),
            "total_capital": status.get("total_capital", 0),
        }
        for coin, info in status.get("per_coin", {}).items():
            if info.get("cooled"):
                result["coins_cooldown"][coin] = {
                    "pnl": info.get("pnl", 0),
                    "consecutive_losses": info.get("consecutive_losses", 0),
                }
    except Exception:
        pass

    # Evolution lock
    try:
        from evolution_lock import can_evolve, manual_lock_exists, _load_lock_log
        ok, reason = can_evolve("web.dashboard")
        result["evolution_lock"] = {
            "locked": not ok,
            "reason": reason,
            "manual_lock": manual_lock_exists(),
        }
        # 审计摘要（轻量：从日志文件直接读）
        data = _load_lock_log()
        writes = data.get("writes", [])
        now_ts = __import__('time').time()
        cutoff = now_ts - 86400
        recent = [w for w in writes if _ts_from_iso(w.get("timestamp", "")) >= cutoff]
        rb_ok = sum(1 for w in recent if w["success"] and "rollback" in w.get("entry", "").lower())
        nr_ok = sum(1 for w in recent if w["success"] and "rollback" not in w.get("entry", "").lower())
        blk = sum(1 for w in recent if not w["success"])
        audit_status = "safe" if nr_ok == 0 else ("warning" if blk > 0 else "alert")
        result["audit_24h"] = {
            "total": len(recent), "rollback_ok": rb_ok,
            "blocked": blk, "non_rollback_ok": nr_ok,
            "status": audit_status,
        }
    except Exception:
        pass

    # Strategy guard
    try:
        from strategy_guard import StrategyGuard
        sg = StrategyGuard()
        for coin in config.COINS:
            from strategy_guard import StrategyGuard as SG
            strats = SG._get_coin_strategies(coin)
            if not strats:
                continue
            coin_status = {}
            for sname in strats:
                r = sg.check(coin, sname)
                coin_status[sname] = r
            result["strategy_guard"][coin] = coin_status
    except Exception:
        pass

    # Extreme market
    try:
        from param_score import is_extreme_market
        extreme = is_extreme_market()
        result["extreme_market"] = extreme
    except Exception:
        pass

    return result


def _ts_from_iso(ts: str) -> float:
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0


@app.websocket("/ws")
async def ws_price(ws: WebSocket):
    """每 3 秒推送所有币种的最新 ticker"""
    await ws.accept()
    # 复用全局连接池，避免每个 WS 连接新建 6 个 OKXClient 放大 API 调用
    clients = {ccy: _client(ccy) for ccy in config.COIN_CONFIG}
    try:
        while True:
            results = await asyncio.gather(
                *[_fetch_one_ticker(ccy, client) for ccy, client in clients.items()]
            )
            data = {ccy: val for ccy, val in results if val is not None}
            if data:
                await ws.send_json(data)
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        pass
