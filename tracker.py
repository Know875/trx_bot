"""收益追踪：记录每笔盈亏，计算总体表现
- 只跟踪盈亏，不依赖账户余额（防止多币种共用余额导致回测算错）
- 回撤计算基于累计盈亏的峰值
"""
import json
import os
import time
import shutil
import logging
from datetime import datetime

logger = logging.getLogger("tracker")

LEGACY_FILE = "trade_records.json"


class Tracker:
    def __init__(self, initial_capital, ccy=""):
        self.initial_capital = initial_capital
        self.realized_pnl = 0.0
        self.records = []
        self._peak_pnl = 0.0  # 累计盈亏高水位线，用于回撤计算
        self.max_drawdown = 0.0  # 历史最大回撤（相对初始资金）
        if ccy:
            self.record_file = f"trade_records_{ccy}.json"
            if ccy == "TRX" and not os.path.exists(self.record_file) and os.path.exists(LEGACY_FILE):
                shutil.copy(LEGACY_FILE, self.record_file)
        else:
            self.record_file = LEGACY_FILE
        self._load()

    def _load(self):
        if os.path.exists(self.record_file):
            try:
                with open(self.record_file) as f:
                    data = json.load(f)
            except Exception as e:
                # 文件损坏（如写入中途被 kill / 并发读到半截）→ 备份后从空开始
                logger.warning(f"{self.record_file} 读取失败，备份后重建: {e}")
                try:
                    os.rename(self.record_file, f"{self.record_file}.corrupted.{int(time.time())}")
                except Exception:
                    pass
                return
            self.realized_pnl = data.get("realized_pnl", 0.0)
            self.records = data.get("records", [])
            if self.records:
                peak_pnl_list = [r["total_pnl"] for r in self.records]
                self._peak_pnl = max(max(peak_pnl_list), 0.0)
                # 从历史记录重建最大回撤
                cap = self.initial_capital or 1
                peak = 0.0
                max_dd = 0.0
                for r in self.records:
                    total = r.get("total_pnl", 0)
                    if total > peak:
                        peak = total
                    dd = (peak - total) / cap
                    if dd > max_dd:
                        max_dd = dd
                self.max_drawdown = max_dd

    def _save(self):
        # 原子写入：临时文件 + os.replace，防止写入中途被 kill 或被并发读到半截
        tmp = f"{self.record_file}.tmp"
        with open(tmp, "w") as f:
            json.dump({"realized_pnl": self.realized_pnl, "records": self.records}, f, indent=2)
        os.replace(tmp, self.record_file)

    def record(self, pnl, strategy, note=""):
        if pnl == 0 and not note:
            return
        self.realized_pnl += pnl
        if self.realized_pnl > self._peak_pnl:
            self._peak_pnl = self.realized_pnl
        # 追踪历史最大回撤
        cap = self.initial_capital or 1
        dd = (self._peak_pnl - self.realized_pnl) / cap
        if dd > self.max_drawdown:
            self.max_drawdown = dd
        self.records.append({
            "time":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "strategy": strategy,
            "pnl":      round(pnl, 6),
            "total_pnl": round(self.realized_pnl, 6),
            "note":     note,
        })
        self._save()

    def summary(self):
        total_return = self.realized_pnl / self.initial_capital * 100 if self.initial_capital else 0.0
        trades   = [r for r in self.records if r["strategy"] != "cleanup"]
        cleanups = [r for r in self.records if r["strategy"] == "cleanup"]
        wins     = [r for r in trades if r["pnl"] > 0]
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        lines = [
            f"\n{'='*40}",
            f"  初始资金:   {self.initial_capital:.2f} USDT",
            f"  累计盈亏:   {self.realized_pnl:+.4f} USDT",
            f"  总收益率:   {total_return:+.4f}%",
            f"  交易次数:   {len(trades)}",
            f"  胜率:       {win_rate:.1f}%",
        ]
        if cleanups:
            lines.append(f"  启动清理:   {len(cleanups)} 次（盈亏未计入）")
        lines.append(f"{'='*40}")
        return "\n".join(lines)

    def drawdown(self):
        """基于初始资金计算回撤（更贴合实际风控）"""
        cap = self.initial_capital or 1
        return max((self._peak_pnl - self.realized_pnl) / cap, 0.0)

    def effective_drawdown(self, floating_pnl=0.0):
        """把持仓未实现盈亏一并计入的回撤。
        floating_pnl<0（浮亏）会放大回撤，使风控能感知"持仓正在流血"。"""
        cap = self.initial_capital or 1
        equity_pnl = self.realized_pnl + floating_pnl
        return max((self._peak_pnl - equity_pnl) / cap, 0.0)

    def reset_drawdown_reference(self):
        """重置回撤基准到当前账户水平（冷却后使用，避免死循环）"""
        self._peak_pnl = self.realized_pnl
        self.max_drawdown = 0.0  # 冷却后重置历史最大回撤，从新起点开始统计
