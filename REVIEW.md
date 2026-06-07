# 代码审查记录 (REVIEW.md)

本文件记录历次代码审查的结论、已修复项、遗留项与核心风险。新一轮审查请在顶部追加。

---

## 当前状态（截至最近一轮）

- **工程/风控/可验证性**：良好。多层熔断、SQLite 并发地基、测试套件 + CI、看门狗冗余、回测含手续费+滑点。
- **盈利能力 / 边际**：**仍未经证实**。网格+趋势是商品化低边际策略；精致的基础设施不创造 alpha。
- **结论**：这是一台"造得好、不易爆、仪表齐全"的机器；**能不能赚钱要靠模拟盘数据回答，不是靠继续加功能**。

> 上实盘前：① `.env` 设 `TOTAL_CAPITAL=真实入金`；② `DASHBOARD_PASSWORD=强密码`；③ `python measure.py` 给出 GO 再小资金上。

---

## 测量优先（从"建造"切到"测量"）

```
模拟盘(OKX_FLAG=1, 别动参数) 跑 2–4 周
        ↓
python measure.py        # 汇总裁决：GO候选 / WATCH / NO-GO / 数据不足
python edge_research.py  # 历史上有没有可利用边际（会直说"无"）
python paper_eval.py     # 实盘成交是否正期望、样本够不够（硬闸）
python backtest_calibrate.py  # 回测比实盘乐观了多少（折扣率）
```
判读：`edge_research` 说"接近随机/网格跑不赢买入持有" + `paper_eval` NO-GO → 别上钱，转 carry 研究或当学习平台。

---

## 已修复项（按主题）

### 资金 / 风控正确性
- **本金口径统一**：`config.TOTAL_CAPITAL`（`.env` 可覆盖）贯穿 实盘 guard / web 面板 / 一致性检查。
  曾两度被改成写死 `sum(initial_capital)=17000` 绕过 `.env`，已修回。
- **账户熔断单调性**：`_guard_mult` 中 protect(3.5%) 一度比 warn(2%) 更宽松（亏更多反而恢复开仓），
  已改为单调收紧（normal→warn→protect→halt 的 can_open 与 cap_mult 均不放宽），并加单调性测试。
- **回撤纳入持仓浮亏**：`_floating_pnl` 把现货/合约未实现盈亏计入回撤风控，不再对在途亏损失明。
- **策略级熔断真正生效**：`_strat_gate` 让 StrategyGuard 的暂停/禁用/仓位乘数落到实际选策略与仓位。
- **交易所止损单**：修 `place_algo_order` 参数错误（此前 TRX 趋势止损单从未真正挂出）+ 合约 tdMode。
- **连亏切网格兜底**：修 `GridStrategy/FuturesGridStrategy` 构造缺参数、`start()` 签名错（触发即崩）。

### 并发 / 持久化
- JSON 状态文件 → **SQLite (WAL)**（`state_db.py`），跨线程/进程安全 + 自动迁移；死线程连接清理。
- `tracker` / `strategy_guard` / `strategy_report`：原子写 + 读容错 + 损坏备份。
- `account_guard` alerts 裁剪到最近 50 条（曾无界膨胀）。

### 回测保真度
- `simulate_grid` / `simulate_trend` 加入手续费 + 滑点（`config.BACKTEST_FEE_RATE/SLIPPAGE`）。
- 实盘盈亏结算全部扣双边手续费（grid / futures_grid / trend / futures_trend / trx_adaptive(_futures)）。
- `backtest_calibrate.py`：回测 vs 实盘折扣率。

### 部署 / 可移植 / 安全
- `requirements.txt` 补 `httpx / scikit-learn / joblib / optuna`（曾缺 httpx 导致服务器启动 ImportError）。
- 跨平台进程锁（Windows msvcrt / Unix fcntl）。
- Dashboard：**拒绝弱口令**（无密码即只读，拒 admin123）、session token 过期校验、Bearer token、登录界面。
- 撤单/全部撤单 DELETE 补认证头（曾遗漏 → 启用认证后 401）。

### AI / 进化
- `ai_tuner --apply-safe` 致命崩溃修复（`_auto_apply_param` 漏 return）；自动改参登记进回滚队列。
- `safe_ai_advice`：AI 只能降仓不能加仓。
- `ml_regime.py`：ML 行情分类增强层（带置信度门槛，缺模型/依赖自动回退规则，绝不中断实盘）。
- `optimize.py`：Optuna 贝叶斯 + walk-forward 样本外验证 + 邻域稳定性（替代离散扫参，缺 optuna 回退）。

### 性能 / Web
- 前端可见性感知轮询（后台标签页暂停）、`api()` 超时+容错、`/api/system/status` TTL 缓存。
- 决策中心面板（测量裁决 + 资金费率套利），后台计算+缓存+秒回，不阻塞。

---

## 遗留 / 注意项（未改或可接受）

| 级别 | 项 | 说明 |
|---|---|---|
| 🟢 | 前端登录用户名写死 `admin` | 若 `.env` 设了自定义 `DASHBOARD_USERNAME`，前端登录会失败。多数用默认 admin 无影响。 |
| 🟢 | 跨进程读改写残留竞态 | web 与 bot 是不同进程；资金关键值 `daily_pnl` 只由 bot 写、有锁 → 安全；受影响的仅 status/alerts 等非关键字段，可接受。 |
| 🟢 | `strategy_report` 手续费列恒为 0 | tracker 不透传 fee；盈亏本身已扣费（净额），报表净/毛两列相等。纯展示，不影响决策。 |
| 🟢 | carry 年化模型偏乐观 | 一次性成本当年化摊销、未建模费率反转/基差/强平。仅作监控雷达，别尽信数字。 |
| ⚠️ | 双进化大脑 | `brain` 与 `ai_tuner` 都能调参；已共享回滚队列安全共存，但**建议只调度一个**做主动调参，另一个仅跑 `rollback`。 |

---

## 核心风险（结构性，无法靠工程消除）

1. **过拟合**：所有优化都在历史上拟合；walk-forward 缓解但 72h 回滚是滞后的。
2. **回测仍偏乐观**：合约资金费率成本、部分成交、订单拒绝未完全建模。
3. **无护城河**：网格/趋势低边际；扣成本后期望可能为负。
4. **复杂度**：活动部件多，靠测试 + CI 兜底，但仍是单人维护的负担。

---

## 审查方法备注
- 每次改动后：`python -m py_compile`（全量）+ 前端 `node --check` + `pytest`（CI 已配）。
- 资金关键路径改动务必跑/补 `tests/`。
