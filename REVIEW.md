# 代码审查记录 (REVIEW.md)

本文件记录历次代码审查的结论、已修复项、遗留项与核心风险。新一轮审查请在顶部追加。

---

## 2026-06-26 整体复审（CI 失效 + 合约网格仓位上限静默失效）

端到端复审。核心资金/风控链（多级熔断、SQLite WAL、strategy_guard peek 只读、
单调收紧）核对正确。发现并修复 3 个真问题 + 2 项清理。

**🔴 已修 — CI 一直是红的，安全网没在跑**
`param_score.py` 已并入 `brain.py`（见 brain.py「merged from param_score.py」）并删文件，
但 `.github/workflows/ci.yml` 语法检查步仍 `python -m py_compile param_score.py`（无 `|| true`）
→ 每次 CI 在该步 `[Errno 2] No such file` 直接失败，后续 pytest 根本没机会判定。
本仓库反复强调的「测试 + CI 兜底」实际断裂——下面两个回归正因此没被自动拦住。
修复：从 CI 列表删除该行（brain.py 已单独 py_compile）。

**🔴 已修 — `tests/test_evolution_lock.py` patch 了已不存在的模块**
3 处 `patch("param_score.is_extreme_market")`，但 `import param_score` 现已 ModuleNotFoundError
（符号搬到 `brain.py`，`evolution_lock` 在调用时 `from brain import is_extreme_market`）。
修复：patch 目标改为 `brain.is_extreme_market`。

**🔴 已修 — 合约网格仓位上限静默失效（ETH_SWAP 裸奔）**
`FuturesGridStrategy._coin` 用「反查 COIN_CONFIG[*].symbol == 入参」解析，但三处构造点
（main.py 的 ranging 启动 / 停滞重组 / 连亏回退）都传 `symbol=ccy`=币种键（"ETH_SWAP"），
与配置里的交易对符号（"ETH-USDT-SWAP"）永不相等 → `self._coin` 恒为空 →
所有 `if self._coin:` 守卫的 `SWAP_POSITION_CAP_PCT`（0.6/0.5/0.4×本金市值上限）成死代码。
SOL_SWAP 尚有 `_max_entries=3` 兜底，**ETH_SWAP 既无档位上限又无市值上限**，唯一保护是
12% 浮亏止损。这是 2026-06-10「config 取值口径对了但调用方没同步」教训的同类新实例。
修复：`_coin` 解析优先「入参直接是 COIN_CONFIG 键则采用」，否则才反查交易对符号（兼容两种口径）。
新增 `tests/test_futures_grid_cap.py`：锁定 _coin 解析 + start() 超仓位上限不挂买单（补上 CI 漏掉的集成点）。

**🔸 已修（清理）**
- `main.py` 当日交易上报块：`reported` 集合读取 + 去重整段复制粘贴了两遍（第一遍结果被第二遍覆盖），删去冗余一份。
- `main.py` 启动阻断心跳：一行无效语句 `Path(__file__).parent / ".bot_heartbeat"`（构造路径但丢弃），删除。

**遗留（未改，文档漂移）**：README 为空；本文件「测量优先」段指引的 `measure.py / edge_research.py /
optimize.py` 已不存在（功能并入 `system_status.py` / `backtest_calibrate.py` / `brain.py`），照抄会 No such file。

---

## 2026-06-10 复审（Web 安全 + 杠杆链路 + carry 对冲）

对全新拉取的最新版（`6af5664`）做端到端复审，重点 Web 安全、合约杠杆落地、本次新改的网格间距死循环逻辑。**核心资金链（资金乘数、熔断单调性、浮亏纳入回撤、carry 回滚、网格回退）核对正确**；发现并修复 3 项问题 + 1 次要项。

**🔴 已修 — `web/app.py` 引用未定义的 `logger`**
全文件无 `import logging`/无 `logger=getLogger`，但第 41 行在 `if not _AUTH_ENABLED:` 分支里调用 `logger.error(...)`。
→ 当 `DASHBOARD_PASSWORD` 未设置或为弱口令时，import 阶段直接 `NameError`，**面板起不来**——本该「降级为只读+强制认证」的保护分支反而把面板搞崩，首次未配密码的部署必踩。
修复：顶部 `import logging` + `logger = logging.getLogger("web")`。（dashboard 与交易主进程分离，不影响实盘交易。）

**🟠 已修 — per-coin 杠杆未真正落到交易所，TRX_SWAP 实跑 5x 而非配置 3x**
`okx_client.set_leverage()` 默认值 `config.get_leverage(self.symbol)`，但 `self.symbol` 是交易对符号（`TRX-USDT-SWAP`），`get_leverage` 的键是币种键（`TRX_SWAP`）→ 永远查不到 → 退回默认 5。所有交易路径均无参调用 `set_leverage()`。
→ 下单算张数用的是币种键（3x 正确），但交易所实际杠杆被设 5x → **保证金按 5x 锁定、强平价更近、爆仓缓冲被削**，正好打在波动最大、明确要低杠杆的 TRX 上。ETH/SOL 配置本就是 5 恰好蒙对。
这是上轮「`get_leverage(self.ccy)` 修复」遗漏的一环：修了算张数取值，没修真正设交易所杠杆的默认值；`test_get_leverage` 只测 config 函数本身、未覆盖 set_leverage 路径。
修复：`set_leverage` 无参时按 `self.symbol` 反查 `COIN_CONFIG` 取币种键杠杆，查不到才退默认。

**🟡 已修 — CORS `allow_origins=["*"]` 与注释「仅同源」矛盾**
配上「GET 免认证（只读监控）」后，`/api/position`、`/api/futures/position`、`/api/overview`、`/api/carry` 等暴露持仓/余额/盈亏 → 任意外站可跨域 JS 读取账户财务（泄露策略）。
修复：`allow_origins` 收敛为环境变量 `DASHBOARD_ORIGINS`（默认本地）。面板前后端同源，同源请求不受 CORS 限制，收紧不影响面板自身，但阻断外站跨域读取。

**🔸 已修（次要）— carry 对冲张数 `max(1, round(got/ct_val))`**
小额下不足 1 张被强制凑成 1 张 → 空腿大于现货 → 净空头裸仓。改为 `math.floor`，不足 1 张则回滚现货并放弃开仓。（carry 仍默认 dry-run。）

**✅ 核对正常**：网格间距死循环修复（适配后过小→回退原始配置→仍不足才冷却3tick，不会无限重启）；`_guard_mult` 单调；`_floating_pnl` 浮亏纳入回撤；carry 两腿同 flag + 第二腿失败回滚；okx_client 重试退避 + 订单不存在计数防死循环 + get_avg_cost 分页结算。

**遗漏教训**：再次出现「config 改对了取值口径，但真正调用 API 的默认参数没同步」——建议为 set_leverage 加一条断言/集成测试（设完查回杠杆比对）。

---

## 2026-06-08 全流程梳理（端到端核对）

把启动 → 每币主循环 → 选策略/下单 → 风控 → 持久化 → Web → 辅助工具 整条链路逐环节追了一遍。
结论：**逻辑正确、自洽**。资金乘数链、风控单调性、浮亏回撤、SQLite 并发、鉴权均核对通过。

**🔴 已修 — 启动安全检查是死代码**
`main.py` 顶层未 `import datetime`，且引用了不存在的 `evolution_lock.LockManager`
→ 整个启动安全检查块每次直接异常被吞 → "账户 HALT 时只监控不交易"从未生效、
启动极端行情告警也从未触发（`_startup_blocked` 永远 False）。
修复：① 补 `from datetime import datetime, timezone`；② 极端检查改用真实 API
`_load_lock_log()["blocks"]`；③ 账户 HALT 检查独立 try，不被极端检查异常带崩。
（运行中每 tick 的 `_guard.check()` 仍在 halt 时拒开新仓，故影响有限，但确是死的安全特性）

**✅ 核对正常 / 上轮修复保持**
- `self.coin` 修复完好，且被服务器 `4f75f9c` 加固为"子类未设则 raise"（fail-loud）。
- carry_executor 安全重写保持，服务器又加了开仓轮询确认 + 平仓交叉校验。
- 资金乘数链完整：本金 × strat_gate × guard_cap × ai_safety × 波动率自适应 × 宏观。
- per-tick `effective_drawdown(含浮亏)`、`_guard_mult` 单调、SQLite 原子写、Web 鉴权 均正确。
- 全量 `py_compile` 通过。

**⚠️ 运维红线**：`carry_executor` 默认 dry-run（安全），但 AUTO_CARRY_COINS 与主 bot 币种重叠
→ **做币种隔离前只能 dry-run，禁开 `CARRY_LIVE=1`**（否则与主 bot 在同币种互相平仓）。

---

## 2026-06-08 复审（波动率自适应 + carry 执行器）

审查对象：昨日新增的 `carry_executor.py`、`volatility_adapter.py`、per-coin 杠杆、前端崩溃修复等。

**🔴 已修 — 致命：`BaseAdaptiveStrategy` 缺 `self.coin`**
波动率自适应集成在 `base_adaptive._start_grid` 用了 `self.coin`，但该属性从未赋值
→ TRX / TRX_SWAP 自适应策略每次启动网格即 `AttributeError`，被 main try/except 吞掉
→ **旗舰币 TRX 网格一直没真正运行**。新增的 9 个测试只测 volatility_adapter 本身，未覆盖集成点。
修复：base 默认 `self.coin="TRX"`，两个子类显式赋值（TRX / TRX_SWAP）。

**🔴 已修 — 危险：`carry_executor.py` 安全重写**
原版真实下单路径有：① 调不存在的 `get_positions()` → 现货先卖后崩 → 裸空；
② 永续硬编码 `flag="1"`(模拟盘)、现货用默认 flag → 真假盘错配；③ 限价单腿风险。
重写：默认 dry-run（实盘需 `CARRY_LIVE=1`）、两腿统一 flag、市价双腿原子开仓（失败回滚）、
先平空再卖现货、按实际成交量对冲、成交校验。
⚠️ 仍受运维约束：AUTO_CARRY_COINS 与主 bot 币种重叠 → 当前**只能 dry-run**，
实盘前必须做币种隔离（否则 run_swap_coin 启动清理会平掉空腿）。

**✅ 核对正常**：`volatility_adapter` 逻辑自洽且有上下界；per-coin 杠杆（TRX_SWAP=3/ETH_SWAP=5）
让上轮的 `get_leverage(self.ccy)` 修复真正生效；前端 JS 崩溃修复后 node --check 通过；
撤单认证、TOTAL_CAPITAL 口径一致性保持。

**教训**：再次出现"加功能未覆盖集成测试 → 旗舰币停摆""下单逻辑未经 dry-run 即入库"。
建议：① 任何新下单逻辑先 dry-run/模拟验证；② 新策略加一条最简"实例化 + start grid"集成测试。

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
