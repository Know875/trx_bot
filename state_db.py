"""
SQLite 持久化 — 替代 JSON 状态文件，天然支持跨线程/跨进程并发。

原理：WAL 模式 + 立即事务提交，SQLite 本身提供文件级写锁。
迁移：首次启动自动从 .json 读入 SQLite，零停机。

表结构：
  guard_state     — key-value（原 guard_state.json）
  strategy_state  — key-value（原 strategy_guard_state.json）
  trades          — 交易记录（原 guard.state["trades"]）

用法：
  from state_db import GuardStateDB, StrategyStateDB
  db = GuardStateDB()       # 自动迁移 guard_state.json
  db.get("daily_pnl")       # → float
  db.set("daily_pnl", 12.5) # 立即写入
  db.get_json("per_coin")   # → dict
"""
import json, os, sqlite3, threading, time, logging
from pathlib import Path

logger = logging.getLogger("state_db")

ROOT = Path(__file__).parent
DB_PATH = str(ROOT / "bot_state.db")

# 全局连接池（每线程一个连接，SQLite 线程安全模式=1 自动分配）
_connections: dict[int, sqlite3.Connection] = {}
_conn_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """获取当前线程的 SQLite 连接（线程安全）。"""
    tid = threading.get_ident()
    with _conn_lock:
        # 定期清理已终止线程的连接（超过50个时触发）
        if len(_connections) > 50:
            alive = {t.ident for t in threading.enumerate() if t.ident}
            dead = [t for t in _connections if t not in alive]
            for t in dead:
                try:
                    _connections[t].close()
                except Exception:
                    pass
                del _connections[t]
        if tid not in _connections:
            conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")   # WAL 下 NORMAL 足够安全
            conn.execute("PRAGMA busy_timeout=5000")    # 5s 忙等待
            conn.row_factory = sqlite3.Row
            _connections[tid] = conn
        return _connections[tid]


def _ensure_tables(conn):
    """建表（幂等）。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS guard_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS strategy_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS guard_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin TEXT NOT NULL,
            pnl REAL NOT NULL DEFAULT 0,
            time TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_guard_trades_date ON guard_trades(date(time));
    """)
    conn.commit()


# ═══════════════════════════════════════════════════════════
# Guard 状态数据库
# ═══════════════════════════════════════════════════════════

_GUARD_JSON = str(ROOT / "guard_state.json")


class GuardStateDB:
    """替代 guard_state.json 的 SQLite 后端。API 与原来的 state dict 兼容。"""

    def __init__(self):
        self.conn = _get_conn()
        _ensure_tables(self.conn)
        self._migrate_if_needed()

    def _migrate_if_needed(self):
        """首次启动：从 JSON 迁移到 SQLite。"""
        if not os.path.exists(_GUARD_JSON):
            return
        # 检查是否已迁移（表中已有数据）
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM guard_state").fetchone()
        if row["cnt"] > 0:
            return  # 已迁移
        try:
            with open(_GUARD_JSON) as f:
                data = json.load(f)
            logger.info("从 guard_state.json 迁移数据到 SQLite...")
            self.set("date", data.get("date", ""))
            self.set("total_capital", str(data.get("total_capital", 0)))
            self.set("daily_pnl", str(data.get("daily_pnl", 0)))
            self.set("status", data.get("status", "normal"))
            self.set_json("per_coin", data.get("per_coin", {}))
            self.set_json("coin_cooled_until", data.get("coin_cooled_until", {}))
            self.set_json("alerts", data.get("alerts", []))
            for trade in data.get("trades", []):
                self.add_trade(trade.get("coin", ""), trade.get("pnl", 0),
                               trade.get("time", ""))
            # 迁移后备份旧 JSON
            os.rename(_GUARD_JSON, _GUARD_JSON + ".migrated")
            logger.info("guard_state.json → bot_state.db 迁移完成")
        except Exception as e:
            logger.warning(f"guard_state.json 迁移失败（将重建）: {e}")

    def get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM guard_state WHERE key=?", (key,)).fetchone()
        if row:
            return row["value"]
        return default

    def set(self, key: str, value):
        self.conn.execute(
            "INSERT INTO guard_state(key,value,updated_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (key, str(value))
        )
        self.conn.commit()

    def get_json(self, key: str, default=None):
        val = self.get(key)
        if val is None:
            return default
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return default or {}

    def set_json(self, key: str, data):
        self.set(key, json.dumps(data, ensure_ascii=False, default=str))

    def get_float(self, key: str, default=0.0):
        try:
            return float(self.get(key, str(default)))
        except (ValueError, TypeError):
            return default

    def get_int(self, key: str, default=0):
        try:
            return int(self.get(key, str(default)))
        except (ValueError, TypeError):
            return default

    def add_trade(self, coin: str, pnl: float, time_str: str = ""):
        self.conn.execute(
            "INSERT INTO guard_trades(coin,pnl,time) VALUES (?,?,?)",
            (coin, pnl, time_str or datetime_str())
        )
        self.conn.commit()

    def get_today_trades(self) -> list:
        today = datetime_str()[:10]
        rows = self.conn.execute(
            "SELECT * FROM guard_trades WHERE date(time)=? ORDER BY id DESC",
            (today,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_today_trades(self) -> int:
        today = datetime_str()[:10]
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM guard_trades WHERE date(time)=?",
            (today,)
        ).fetchone()
        return row["cnt"] if row else 0


# ═══════════════════════════════════════════════════════════
# 策略状态数据库
# ═══════════════════════════════════════════════════════════

_STRATEGY_JSON = str(ROOT / "strategy_guard_state.json")


class StrategyStateDB:
    """替代 strategy_guard_state.json 的 SQLite 后端。"""

    def __init__(self):
        self.conn = _get_conn()
        _ensure_tables(self.conn)
        self._migrate_if_needed()

    def _migrate_if_needed(self):
        if not os.path.exists(_STRATEGY_JSON):
            return
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM strategy_state").fetchone()
        if row["cnt"] > 0:
            return
        try:
            with open(_STRATEGY_JSON) as f:
                data = json.load(f)
            logger.info("从 strategy_guard_state.json 迁移到 SQLite...")
            for key, value in data.items():
                self.set_json(key, value)
            os.rename(_STRATEGY_JSON, _STRATEGY_JSON + ".migrated")
            logger.info("strategy_guard_state.json → bot_state.db 迁移完成")
        except Exception as e:
            logger.warning(f"strategy_guard_state.json 迁移失败（将重建）: {e}")

    def get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM strategy_state WHERE key=?", (key,)).fetchone()
        if row:
            return row["value"]
        return default

    def set(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO strategy_state(key,value,updated_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (key, value)
        )
        self.conn.commit()

    def get_json(self, key: str, default=None):
        val = self.get(key)
        if val is None:
            return default
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return default or {}

    def set_json(self, key: str, data):
        self.set(key, json.dumps(data, ensure_ascii=False, default=str))

    def setdefault(self, key: str, factory):
        """dict.setdefault 兼容：key 不存在时用 factory() 创建。"""
        val = self.get_json(key)
        if val is None:
            val = factory()
            self.set_json(key, val)
        return val

    def keys(self) -> list:
        rows = self.conn.execute("SELECT key FROM strategy_state").fetchall()
        return [r["key"] for r in rows]

    def items(self):
        rows = self.conn.execute("SELECT key, value FROM strategy_state").fetchall()
        for r in rows:
            yield r["key"], r["value"]

    def pop(self, key: str, default=None):
        val = self.get_json(key, default)
        self.conn.execute("DELETE FROM strategy_state WHERE key=?", (key,))
        self.conn.commit()
        return val

    def clear(self):
        self.conn.execute("DELETE FROM strategy_state")
        self.conn.commit()


# ═══════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════

def datetime_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
