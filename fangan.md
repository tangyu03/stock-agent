# 文档一：交易信号系统技术方案（v8.1）



## 0. 定位与铁律

一个**信号服务**：每晚收盘后计算点位、入库；盘中价格触达点位提醒；信号过期或前提作废撤销提醒。不下单、不连交易接口、不接 LLM、不建模真实仓位、不记账、不知道你的账户。纯后端 CLI 驱动，无定时任务自动推送，无按钮回传。

三条铁律：

1. **pool 是关注范围，不是持仓清单。** 池内每只票所有适用 role 全算全呈现；系统不知道、不推断你的实际持仓。同一只票同一晚可以同时有 entry 和 stop——5 日线对空仓者是买点、对持有者是风控线，两条都给，你自己对号入座。
2. **信号一生只有四个状态：ACTIVE → TRIGGERED / EXPIRED / INVALIDATED。** 人的动作（veto、回执）是对信号的标注，不是状态机的一环。
3. **策略就是 strategies.yaml 里的几段配置。** 每条策略必须写全 premise / y / z / w 四键，缺一加载即报错。加打法 = 加一段 YAML。

## 1. 行为建模

系统的全部行为按触发源分三类。时钟列为**建议时刻**，实际全部由人工 CLI 触发。

**时钟触发（routines）：**

| 时刻 | 行为 | 输出 |
|---|---|---|
| 15:30 | evening 收盘生成 | 信号入库＋汇总报告（含零预案判定）＋ ACTIVE entry 的 premise 重检 |
| 9:15–9:25 | auction_call 竞价判定 | 形态分类 → 作废当日 BUY 或快照供人确认 |
| 9:30–15:00 | watch 盘中盯梢 | 触发检查 → 定性 → 提醒；context_marks 的 triggered_at 记录（每次触发更新为最近） |
| 14:30 | 尾盘快照 | 把此刻价格写入 tail_snapshots，供晚间 ma_tomorrow 使用；**ma_tomorrow 系 entry 的触发窗口从此刻开启** |
| 14:30 起 | restore_check T 复原倒计时 | 未配对 T 提醒（当日配对窗口） |
| 15:05 | settle 收盘清点 | EXPIRED / INVALIDATED ＋ retest_confirm 生成（按 §7.2 四路分支）＋ Z 滚动重算 |

**行情触发（watch 内部分支）：**

- **entry 触发**：单点触碰即触发，**但受 trigger_window 约束**——ma_tomorrow 系 entry 仅 14:30–15:00 接受触发，窗口外触碰直接忽略、不记 touched_only、不推送；first_board_dip / volume_breakout 的 entry 全天有效。**activation 对 entry 不再是“仅文案提示”，而是真实触发约束。**
- **stop/profit/t 触发**：单点触碰即触发，全天有效（intraday 语义按 §3.1 valid_policy 说明）。
- **破位定性**：两级查询（板块 → 指数，查 context_marks）＋封板豁免＋分歧规则。
- **STOP 当日收复**：纠错提醒。
- **前提破坏**：作废＋撤销提醒。
- **retest_confirm 盘中触达**：按 §7.3 判定终态。

**人触发（全部可选，不驱动状态机）：** veto；receipt（executed / ignored，可带 size_filled）；t-size；pool 编辑；config 编辑；push-file 人工单向推送。



## 2. 项目结构

```
pointtable/
├── README.md / ARCHITECTURE.md / CHANGELOG.md / requirements.txt
├── config/
│   ├── strategies.yaml         # 全部策略（四键校验，y.when=tail_snapshot 触发 trigger_window）
│   ├── config.yaml             # webhook、量能档位、监管清单、砸盘席位、阈值、节假日日历、retest 参数、reentry_rules、guard 对账阈值
│   └── theme_map.yaml          # 股票→主板块 人工 overrides
├── pointtable/
│   ├── models/
│   │   ├── signal.py           # Signal + 四状态枚举 + zone校验(low<high) + role/side 枚举 + break_qualifier + trigger_window
│   │   ├── journal.py          # JournalEntry（record_type: transition / zone_update / qualifier）
│   │   ├── receipt.py          # Receipt（executed/ignored/veto + size_filled）
│   │   ├── pool.py             # PoolEntry（date/symbol/theme_label）
│   │   └── context_mark.py     # ContextMark（index/sector 参考区间＋最近 triggered_at）
│   ├── db/session.py
│   ├── data/
│   │   ├── akshare.py          # 行情/竞价/龙虎榜/指数/板块指数/两融/主力资金流
│   │   ├── guard.py            # DataFreshnessGuard + 报价合理性检查 + 量比×换手口径交叉校验 + 两源对账
│   │   └── mock.py
│   ├── core/                   # 纯判断与纯生成：不 import data/db/notify（CI 强制）
│   │   ├── ma.py / trend.py / volume.py / auction.py / lhb.py / sector.py
│   │   ├── selector.py         # 策略前提检查（每策略一个纯函数，独立并行）
│   │   ├── generator.py        # 前提满足集 × pool → 信号行（Z/W 同生＝确保存在并关联、三个前置检查）
│   │   └── lifecycle.py        # 单信号四状态迁移唯一入口
│   ├── jobs/
│   │   ├── evening.py / auction_call.py / watch.py / restore_check.py / settle.py
│   ├── notify/
│   │   ├── push.py
│   │   └── render.py
│   ├── stats.py
│   └── cli.py
├── legacy/
└── tests/
    ├── fixtures/               # 川环26.5 / 博创42.3 / 兆龙60.5
    ├── test_ma.py / test_trend.py / test_volume.py / test_lifecycle.py
    ├── test_ordering.py / test_replay.py / test_deps.py / test_selector.py
    ├── test_settle_branches.py / test_reentry_checks.py
    ├── test_guard.py           # 脏报价拦截 + 两源对账
    ├── test_reachability.py    # 尾盘近似可达 + unreachable 标记
    └── test_trigger_window.py  # entry 时间窗硬约束
```

## 3. 数据模型（五表 + context_marks）

### 3.1 signals（核心表）

| 字段 | 说明 |
|---|---|
| id | 主键 |
| symbol | 标的 |
| strategy_id | 策略 ID |
| side | BUY / SELL / NEUTRAL（可从 role 推导，只有 t 显式写） |
| role | entry / stop / profit / t / retest_confirm（仅五个，见 §3.6 定义表） |
| zone_low / zone_high | 强制区间（low < high） |
| size_hint | 仓位提示 |
| premise | 生成时前提快照（含 premise_facts） |
| trend | UP / DOWN |
| **break_qualifier** | **可空，仅 role=stop 用**：EXECUTE_NOW / DELAY_OBSERVE / DIVERGENCE / LIMIT_EXEMPT |
| **trigger_window_start / trigger_window_end** | **可空**：触发窗口起止时刻；空表示全天有效。ma_tomorrow 系 entry 填 14:30–15:00 |
| activation | 仅 stop/profit/t 文案提示；**对 entry 不再是仅文案，由 trigger_window 承担真实约束** |
| valid_policy | intraday / until_stopped / fixed_window（语义见下） |
| window_days | 仅 fixed_window 用，生成时从 config 快照 |
| status | ACTIVE / TRIGGERED / EXPIRED / INVALIDATED |
| status_reason | 封闭枚举：hit / eod / premise_broken / human_veto / retest_pass / retest_fail |
| parent_id | stop/profit/t/retest_confirm 必填，指向最近一次 TRIGGERED 的 enter；无则 `pool:<symbol>` |
| root_id | 链起点，可选（跨 reentry 保留） |
| created_at / closed_at | “当日结案”查 signals.closed_at |

**valid_policy 语义**：

| 值 | 对 entry 的语义 | 对 t 的语义 | 对 stop/profit 的语义 |
|---|---|---|---|
| intraday | **次一交易日有效**：T 晚生成，T+1 settle 未触发才 EXPIRED | **当日有效**：T 日生成，T 日 settle 未触发即 EXPIRED | — |
| until_stopped | — | — | 持续有效，直至触发或 premise_broken / human_veto |
| fixed_window | — | — | retest_confirm 用；window_days 期有效 |

**关键删除**：`journal.confirmed` 字段（v8.0 已删）、双周期确认逻辑（v8.0 已删）。

### 3.2 journal

| 字段 | 说明 |
|---|---|
| id / signal_id | 主键 / 关联信号 |
| record_type | **transition / zone_update / qualifier（新增）** |
| event | 仅 transition：hit / eod / premise_broken / human_veto |
| reason | 迁移原因（同 status_reason 枚举） |
| **qualifier** | **新增，仅 record_type=qualifier**：四路定性值 |
| old_zone_low / old_zone_high | 仅 zone_update |
| new_zone_low / new_zone_high | 仅 zone_update |
| created_at | 时间戳 |

**三条记录规则**：

- **transition**：每次状态迁移记一条；
- **zone_update**：stop/profit 每晚滚动更新 zone 时记一条；t 不记（t 是 intraday 当日新建，不原地更新）；
- **qualifier**：stop 触发后写入 break_qualifier 时记一条，供 settle 读取和审计追溯。

### 3.3 receipts

id / signal_id / verdict（executed / ignored / veto）/ size_filled / created_at。

**T 配对**：不加 pair_id。restore_check 用“同 symbol + 同日 + T_BUY executed + T_SELL 无 executed”查询时推导。**配对窗口 = 当日**；T_SELL 次日触发不回溯配对。

**一个 signal 只有一个有效 receipt**：允许补录历史记录，但最新一条为 effective，旧记录标记 superseded 留 audit trail。

### 3.4 pool

date / symbol / theme_label。**关注范围，不是持仓清单。**

### 3.5 tail_snapshots

date / symbol / price。14:30 由 watch 写入。ma_tomorrow 的 est_close 优先取它；无快照才 fallback 当日收盘价，生成信号日志必须标注口径。

### 3.6 role 定义对照表

| role | side | zone_policy | pair_required | parent | 触发方式 | 触发窗口 | 触发后行为 |
|---|---|---|---|---|---|---|---|
| entry | BUY | 当日有效（次一交易日） | 否 | 无 | 单点 | **ma_tomorrow 系 14:30–15:00；其他全天** | **成为后续保护行的 parent 锚点＋统计分母** |
| stop | SELL | rolling（每日上移） | 否 | enter 或 pool | 单点 | 全天 | 产生 retest_confirm |
| profit | SELL | **fixed 或 trailing（按策略 w 配置）** | 否 | enter 或 pool | 单点 | 全天 | 无 |
| t | BUY/SELL | fixed（当日） | 是（当日配对） | enter 或 pool | 单点 | 全天 | 配对提示 |
| retest_confirm | NEUTRAL | window(n) | 否 | enter 或 pool | 条件判定 | 全天 | 链终止或纠错 |

**加新 role 的规则**：先填此表，原子组合与现有五个不同才配加。

### 3.7 context_marks（新增，替代原 signals 的 index_context/sector_context）

| 字段 | 说明 |
|---|---|
| date | 日期 |
| context_type | index / sector |
| code | 指数代码或板块代码 |
| level_low / level_high | 参考区间 |
| label | index：UP/DOWN；sector：主线/轮动/退潮 |
| **triggered_at** | **最近一次进入区间的时间戳**；每次触发都更新为最近，不保留首次 |
| created_at | 生成时间 |

**生命周期**：evening 每天生成 ACTIVE；watch 盘中检测进入区间，每次更新 triggered_at 为当前时刻；次日 evening 覆盖或置 INACTIVE。**不接 lifecycle.py，不产生 journal 迁移记录，不进 stats，不进报告买卖区。**

**两级查询**：`break_qualifier` 查“近 30 分钟有无 triggered_at 且时间戳在 30 分钟内”。

### 3.8 time_trigger

不进 signals，留在 event_calendar 驱动 evening 提示。

## 4. 判断模块（core/）

**ma.py**：`ma_tomorrow(closes, n, est_close)`。est_close 是尾盘预估收盘价。

**trend.py**：收盘>20日线且5日线斜率向上 → UP，其余 DOWN。

**volume.py**：`break_qualifier` 四路输出：

```python
def break_qualifier(change_pct, drive, vgear, context_marks) -> str:
    if limit_exempt(change_pct, code, name): return "LIMIT_EXEMPT"
    # 两级查询：先板块 context_marks 近30分钟有无 triggered_at，再指数
    if 放量 and 主动:        return "EXECUTE_NOW"
    if 下跌 but 外盘占优:     return "DIVERGENCE"
    return "DELAY_OBSERVE"
```

**auction.py**：四类分类——weak_to_strong / strong_to_weak / limit_up_break / neutral。

**lhb.py**：命中砸盘席位或卖方集中度过阈 → 次日 BUY 不生成；池内票 push 预警。

**sector.py**：主线/轮动/退潮三态判定；池内去弱择强排序；theme_map 读取。

**selector.py**：策略前提检查，每策略一个纯函数，独立并行、无优先级排序：

```python
def check_first_board_dip(facts) -> bool: ...
def check_volume_breakout(facts) -> bool: ...
def check_first_move_5d(facts) -> bool: ...
def check_first_move_10d(facts) -> bool: ...
def check_first_move_drift(facts) -> bool: ...
```

**generator.py**：

- 收集全部前提满足的策略 → **同票不去重，全部输出全部推送**；
- 过三个前置检查（见 §7.5）；
- premise 校验（监管/龙虎榜/量能档位）；
- ENTER 生成时：
  - 若该 symbol 已有 ACTIVE stop → **不新建，把 enter 记为该 stop 的关联锚点（stop.parent_id 更新为指向新 enter）**；
  - 若没有 → 按 Z/W 同生新建一条 stop；
  - **同 symbol 同时刻只有一条 ACTIVE stop**（A4 硬约束）；
- 池内票按趋势生成/更新保护行与 T 行（见下）；
- context_marks 生成；
- time_trigger 行。

**池内票每晚生成规则（含 A5 过渡规则）**：

| 情形 | 生成/更新 |
|---|---|
| 池内票、趋势 UP | stop zone 滚动上移（**原地更新，记 zone_update**）＋ t（**每晚新建，旧行 eod EXPIRED**）＋ profit（原地更新或 trailing，记 zone_update） |
| 池内票、趋势 DOWN | stop zone 更新为支撑位，不生成 BUY 行 |
| UP→DOWN 翻转当晚 | 原 T_BUY 连坐 INVALIDATED，stop zone 更新为支撑位 |
| **观察哨存续期间（有 ACTIVE retest_confirm）** | **暂停生成 stop / profit / t**；保护语义由 retest_confirm 承担 |
| **判决三终态落地后至新 enter 前** | **仍暂停生成**（价格未到下一档均线，没有保护对象） |
| **新 enter 触达后** | **恢复正常生成** |

**parent 规则**：parent_id 指向该 symbol 最近一次 TRIGGERED 的 enter；无则 `pool:<symbol>`。

**lifecycle.py**：唯一 `transition(signal, event, reason)` 入口，三参数显式。四状态终态不可逆。**lifecycle 只感知单信号，不感知链。**

## 5. 策略与配置

```yaml
strategies:
  - id: first_move_5d
    side: BUY
    premise: 热门题材 + 5日线上行 + 未进监管 + 榜不烂
    y: { type: ma_tomorrow, n: 5, when: tail_snapshot }      # when 触发 trigger_window = 14:30–15:00
    z: { type: ma_break, n: 5 }
    w: { type: pressure_zone, action: sell_part }

  - id: first_move_10d
    side: BUY
    premise: 热门题材 + 回调深但10日线方向未死 + 未进监管
    y: { type: ma_tomorrow, n: 10, when: tail_snapshot }
    z: { type: ma_break, n: 10 }
    w: { type: pressure_zone, action: sell_part }

  - id: first_move_drift
    side: BUY
    premise: 热门题材 + 5日线正在下移 + 尾盘突破明日5日线位置
    y: { type: ma_tomorrow, n: 5, direction: descending, when: tail_snapshot }
    z: { type: ma_break, n: 5 }
    w: { type: pressure_zone, action: sell_part }

  - id: first_board_dip
    side: BUY
    premise: 首板(人工标注,仅标注日次日有效) + 分时回踩 + 未进监管
    y: { type: intraday_support }                            # 无 when，全天有效
    z: { type: ma_break, n: 5 }

  - id: volume_breakout
    side: BUY
    premise: 放量站上关键位 + 收阳量能确认 + 未进监管
    y: { type: pullback, ratio: 0.98 }                       # 无 when，全天有效
    z: { type: close_below, level: breakout_price }
    w: { type: next_resistance, trailing: true }             # profit 的 trailing 语义来源

t_overlay:
  applies_to: pool
  rows:
    - { side: BUY,  role: t, zone: support_minus_1pct }
    - { side: SELL, role: t, zone: resistance_minus_tick }

market_context:
  indices: [sh000001, sz399001, sh000688]
  sectors: pool 内票的 theme_map 归属，每板块一支撑一压力
```

**size_hint 修饰器（两个）**：① 量能 LOW 档全部 BUY 减半；② T_BUY 档位通过 CLI `t-size` 落库。浮盈切10日线修饰器已删除。

**size_hint 与报告的关系**：量能 LOW 减半时在候选行尾附 `仓位提示：减半（量能LOW）`；T_BUY 的 t-size 结果附在池内票区 T 行尾。

**config.yaml**：

```yaml
market_volume_tiers: ...
regulator_list: ...
lhb_dumpers: ...
poll_intervals: ...           # 仅作人工触发频率建议
watchlist_max: 25
holiday_calendar: ...
retest_window_days: 3
retest_volume_min: 1.2

reentry_rules:
  same_ma_cooldown_days: 2

# 新增
stats:
  entry_reachability_mode: tail_approx   # 尾盘近似（默认）；日 K 全天可达率并列输出
  tail_approx_tolerance: 0.003           # 收盘价 ± 0.3% 视为在 zone 内
guard:
  two_source_deviation_max: 0.005        # 盘中最后报价 vs 日K收盘价，偏差超 0.5% 标记异常
  quote_sanity: enabled                  # 涨跌幅限制/价格为0/跳变异常拦截
```

无杠杆字段。已删除 `dedup_zone_overlap`、`min_confirm_duration`。

## 6. 建模：三层结构

### 6.1 第一层：实体与四状态机

| 实体 | 状态 | 说明 |
|---|---|---|
| Signal（role=entry） | ACTIVE → TRIGGERED / EXPIRED / INVALIDATED | 候选买点，触达即终态；ma_tomorrow 系仅在 trigger_window 内触发 |
| Signal（role=stop） | 同上 | 池内票风控线，破位即 TRIGGERED；break_qualifier 记录定性 |
| Signal（role=profit） | 同上 | 池内票止盈参考 |
| Signal（role=t） | 同上 | 池内票做T参考；配对靠回执推导；每晚新建、当日有效 |
| Signal（role=retest_confirm） | 同上 | 观察哨，三终态即“判决” |
| PoolEntry | in / out | 关注关系 |
| Receipt | executed / ignored / veto | 标注 |
| ContextMark | ACTIVE / INACTIVE | 轻量环境标记 |

```python
TRANSITIONS = {
    ("ACTIVE", "hit"):            "TRIGGERED",
    ("ACTIVE", "eod"):            "EXPIRED",
    ("ACTIVE", "premise_broken"): "INVALIDATED",
    ("ACTIVE", "human_veto"):     "INVALIDATED",
}
```

**TRIGGERED 语义**：

- **stop / profit / t**：单点触碰即触发，全天有效；
- **entry**：单点触碰即触发，**但受 trigger_window 约束**——ma_tomorrow 系仅 14:30–15:00 接受触发，窗口外触碰直接忽略、不记 touched_only、不推送；first_board_dip / volume_breakout 全天有效；
- 防重复推送由状态机终态保证——TRIGGERED 不可逆。

### 6.2 第二层：因果链

- 边由 parent_id 表示：stop/profit/t/retest_confirm 指向最近一次 TRIGGERED 的 enter；无则 `pool:<symbol>`；
- 边的产生由 settle / generator 实现；
- lifecycle.py 不感知链；
- S0–S7 是报告叙事章节，不是状态机。

### 6.3 第三层：报告槽位投影

`report --date` 只读不写，按 §8 投影。

## 7. 破位-反抽-再进场链的因果规则

### 7.1 破位当天定性

STOP 单点触碰即 TRIGGERED，随后 `break_qualifier` 四路输出，**定性写入 signals.break_qualifier，并记 journal record_type=qualifier**。

| 定性 | 推送文案 | 当天动作建议 |
|---|---|---|
| EXECUTE_NOW | “破位，撤” | 当天走 |
| DELAY_OBSERVE | “缩量破位，疑似被动，收盘定生死” | 等收盘 |
| DIVERGENCE | “下跌但外盘占优，有承接，看不懂，收盘复核” | 等收盘 |
| LIMIT_EXEMPT | “封板附近失真，不定性” | 不判 |

**熔断日特殊处理**：DELAY_OBSERVE / DIVERGENCE 直接按 EXECUTE_NOW 处理，不等收盘定生死。

### 7.2 收盘定生死（settle.py 四路分支）

```python
for stop in today_triggered_stops:
    q = stop.break_qualifier          # 读 break_qualifier 字段，不读 status_reason
    if q in ("DELAY_OBSERVE", "DIVERGENCE"):
        if close < stop.zone_low:
            create_retest_confirm(stop)      # 收盘仍破 → 生成
        else:
            push_correction_notice(stop)     # 收盘收复 → 纠错提醒，不生成
    elif q == "EXECUTE_NOW":
        create_retest_confirm(stop)          # 当天执行也生成：给纠错留门
    # LIMIT_EXEMPT → 不生成，次日晚上正常生成新 STOP
```

**四条硬约束**：EXECUTE_NOW 也生成；DELAY/DIVERGENCE 同一段逻辑双向；LIMIT_EXEMPT 不生成；生成时 zone 取原 STOP 的 zone，parent 指向原 enter 或 `pool:<symbol>`，valid_policy=fixed_window，window_days 从 config 快照。

### 7.3 retest_confirm 三终态

| 终态 | 判定依据 | 结案时刻 | 判定者 |
|---|---|---|---|
| TRIGGERED | 盘中摸进观察区间、收盘没站上区间上沿 | 盘中某次 watch（事件） | watch |
| INVALIDATED | 收盘站上区间上沿且量比 ≥ `retest_volume_min` | 盘中最后一次 watch 或收盘判定 | watch |
| EXPIRED | 窗口到期，价格从没摸进区间 | 到期日 15:05 settle | settle |

**“当日结案”查法**：`status in 终态 且 signals.closed_at 日期 = 报告日期`。

### 7.4 纠错提醒统一

两条路径共用 `push_correction_notice()`：破位当日盘中收复（watch）、retest_confirm 放量站回（watch 判 INVALIDATED）。文案一致、不迁移原 STOP 状态、不发新买点。

### 7.5 判决后重新选股：三个前置检查

```python
# 检查一：观察期存续 → 不出买点
if has_active_retest_confirm(symbol):
    skip_buy()

# 检查二：同均线档位近 N 日两次 STOP → 该均线家族暂停
if same_ma_stop_count(symbol, ma_n=5, days=cfg.reentry_rules.same_ma_cooldown_days) >= 2:
    exclude(["first_move_5d", "first_move_drift"])

# 检查三：观察期结案 + 价格位置
# 结案 → 选股恢复；价格在哪根均线附近，哪个策略前提自然满足就出哪个
```

“破5卖→10日再买”是检查三在价格走到10日线时的自然结果。

### 7.6 因果规则表

| 触发 | 条件 | 产生 | parent_id | 实现 |
|---|---|---|---|---|
| enter TRIGGERED | 回执入场 | 该 stop 的 parent 锚点更新为新 enter（如无 stop 则新建） | — | evening |
| 池内票，每晚 evening | 趋势 UP/DOWN 且**无 ACTIVE retest_confirm** | 更新 stop、t、profit | → 最近 TRIGGERED enter 或 pool | generator |
| 池内票，观察哨存续 | 有 ACTIVE retest_confirm | **暂停生成 stop/profit/t** | — | generator |
| 池内票，判决后至新 enter 前 | 无 ACTIVE stop 且价格未到均线 | **暂停生成** | — | generator |
| stop TRIGGERED | 写入 break_qualifier | 记 journal record_type=qualifier | — | watch |
| stop TRIGGERED | EXECUTE_NOW（或熔断日 DELAY/DIVERGENCE 视同） | retest_confirm | → 原 enter 或 pool | settle |
| stop TRIGGERED | DELAY/DIVERGENCE 且收盘<zone_low | retest_confirm | 同上 | settle |
| stop TRIGGERED | DELAY/DIVERGENCE 且收盘收复 | 纠错提醒（无新信号） | — | settle |
| stop TRIGGERED | LIMIT_EXEMPT | 不产生 retest_confirm | — | settle |
| retest_confirm TRIGGERED | 反抽不过 | 链终止，等新 enter | — | watch |
| retest_confirm INVALIDATED | 放量站回 | 纠错提醒；人买回需新 enter | — | watch |
| retest_confirm EXPIRED | 到期无反抽 | 链终止，等新 enter | — | settle |
| 任意时刻 | 三个前置检查通过＋价格到位＋（ma_tomorrow 系）14:30–15:00 窗口内 | 新 enter | 无（新链起点） | generator |

## 8. 报告形态与槽位投影

| 槽位 | 投影规则 |
|---|---|
| A0 今日执行回顾 | 当日进入 TRIGGERED 的全部 Signal ＋ receipts ＋机械 vs 实际差异 |
| 环境行 | context_marks（ACTIVE）＋ event_calendar |
| 候选区 | ACTIVE 的 entry，经三个前置检查过滤；同票多策略全部列出不合并；超 watchlist 上限标 truncated；ma_tomorrow 系附 `触发窗口：14:30–15:00` |
| 池内票区 | 池内票的 ACTIVE stop/profit/t（原“持仓区”改名） |
| 破位区 | 当日进入 TRIGGERED 的 stop，附 break_qualifier |
| 观察区 | ACTIVE 的 retest_confirm |
| 判决区 | 当日 signals.closed_at 的 retest_confirm 终态 |

**关键约束**：

1. **同一只票可同时在候选区和池内票区出现，不折叠。**
2. **观察区与判决区互斥**。
3. **判决区只显示当天刚结案的**；回看走“近期已结案”小节。

**零预案日两个变体**：市场熔断型（LOW+指数DOWN，**破位定性取消延迟判决**）；节假日型（长假前最后一交易日，停止新 BUY）。

## 9. 统计（两层）

**信号质量层（纯行情可算）**：

- **触达率 = 可达信号数 / 生成数**。可达判定按 role 分规则：

| role | 可达判定 |
|---|---|
| entry（ma_tomorrow 系） | **主口径：收盘价 ∈ [zone_low, zone_high] ± tail_approx_tolerance（尾盘近似）**；辅口径：当日最低价 ≤ zone_high（全天可达率，并列输出） |
| entry（first_board_dip / volume_breakout） | 当日最低价 ≤ zone_high（全天有效） |
| stop | 当日最低价 ≤ zone_low |
| profit | 当日最高价 ≥ zone_low |
| t | 按 side 套 entry/profit 规则 |
| retest_confirm | 当日最高价 ≥ zone_low |

- **不可达但盘中触发的，标 `unreachable`**：不进假想期望分子分母，单独统计“误触发率”。
- **两源对账异常日，unreachable 率不纳入审计**。
- 分策略假想期望：触发价机械执行、满30笔起算、滚动50笔期望≤0 → EV_NEG。
- **卖飞率 vs 避损率**：STOP 后追踪5日，**5日内收盘收复 zone_high 判定时使用 stop 终态时刻（closed_at）的 zone 快照，不取当前 zone**（zone_update journal 提供历史值）。
- 纠错提醒命中率。
- 池子质量报表。
- 合成 parent（`pool:` 前缀）的信号链不进统计。

**行为层（回执驱动，覆盖率<50% 明示“数据不足”）**：纪律执行率；做T增值粗算；复原违规次数。

**明确删除**：账户盈亏、连续亏损、违规计数、杠杆提醒、浮盈计算。

## 10. 测试与验收

test_ma / test_trend / test_volume / test_selector / test_lifecycle / test_ordering / test_replay / test_deps / test_settle_branches / test_reentry_checks / test_guard / test_reachability / **test_trigger_window（新增）**。

**验收锚点—测试文件映射**：

| 验收锚点 | 测试文件 |
|---|---|
| 缩量/分歧收盘仍破 → 生成 retest_confirm；收盘收复 → 只发纠错 | test_settle_branches |
| EXECUTE_NOW 当天执行仍生成 retest_confirm | test_settle_branches |
| LIMIT_EXEMPT 不生成，次日正常生成 STOP | test_settle_branches |
| 熔断日 DELAY/DIVERGENCE 视同 EXECUTE_NOW | test_settle_branches |
| 纠错两条路径文案一致、不迁移状态、不发买点 | test_lifecycle |
| TRIGGERED / INVALIDATED＋纠错 / EXPIRED 各一条 | test_lifecycle |
| 观察期存续时选股对该票零输出 | test_reentry_checks |
| 结案且价格到位后 first_move_10d 自动出现 | test_reentry_checks |
| 同均线两次止损后 5日线家族暂停、10日线不受影响 | test_reentry_checks |
| 同票多策略全部输出全部推送，无合并 | test_selector |
| 同一条信号跑多次 watch 只推送一次 | test_lifecycle |
| 脏报价被 guard 拦截不产生触发 | test_guard |
| 两源对账异常日 unreachable 率不纳入审计 | test_guard |
| 日 K 不可达但盘中触发 → 标 unreachable，不进假想期望 | test_reachability |
| entry 尾盘近似可达：上午穿过、尾盘未回 → 判不可达 | test_reachability |
| **ma_tomorrow 系 entry 在 14:30 前触碰 → 忽略、不推送** | **test_trigger_window** |
| **first_board_dip / volume_breakout 全天触发不受 window 限制** | **test_trigger_window** |
| **同 symbol 同时刻只有一条 ACTIVE stop** | **test_reentry_checks** |
| **观察哨存续期不生成 stop/profit/t** | **test_reentry_checks** |
| **break_qualifier 写入与 settle 读取** | **test_settle_branches** |
| depth 删除后无残留调用 | test_deps |
| 无状态机非法迁移 | test_lifecycle |

## 11. 系统边界

| 不做 | 原因 |
|---|---|
| 选股/题材判断 | “没啥指标，就一眼感觉”。pool 落表并审计质量，不判断 |
| 支撑压力精密算法 | 机械代理＋premise 标注，人可 veto |
| 日内纠错决策 | 只发收复提醒，追不追是人 |
| 破位后“利润或纪律”的二选一 | 系统不知道你的利润厚度 |
| 个股与大盘压力冲突优先级 | “人是活的” |
| 全市场板块扫描 | 只做池内板块 context_marks |
| 账户追踪/自报/熔断器/杠杆提醒 | 系统不知道账户。唯一约束是市场熔断 |
| 定时任务/按钮回传/自动推送 | 纯 CLI 人工触发 |
| 双周期确认 / 盘中计时确认 | 采样粒度由人控制；防毛刺由 guard 承担 |
| LLM / 网格回测 / 基本面闸门 / 真实仓位建模 | 将来以新 role 或新报表增量加入 |

## 12. 废弃项

| 废弃项 | 原因 |
|---|---|
| depth 参数 | 选股由三个前置检查＋价格位置决定 |
| `dedup_zone_overlap` | 生成层不合并 |
| “三道闸/闸门”叫法 | 统一为“前置检查” |
| 浮盈切10日线修饰器 | 系统不知道真实利润厚度 |
| 双周期确认 | 纯 CLI 采样粗；保留会产生系统性漏报 |
| `journal.confirmed` 字段 | touched_only 概念整体消失 |
| `first_in_zone_at` / `min_confirm_duration` | 时间戳方案作废 |
| signals 的 index_context / sector_context / time_trigger | 剥到 context_marks / event_calendar |
| signals.tag 8 值平铺 | 改为 role 5 值 + side 双字段 |
| **`holding:<symbol>` 前缀** | **改 `pool:<symbol>`，与铁律一“pool 是关注范围”一致** |
| **“熔断日 STOP 收紧为破位即 SELL”** | **改写为“熔断日破位定性取消延迟判决”** |
| **“entry 产生持仓上下文”措辞** | **改为“成为后续保护行的 parent 锚点＋统计分母”** |

## 13. 当前实施状态

已实现：市场输入与尾盘快照、selector 按事实独立选策、volume_breakout 独立 assignment、同票多策略不去重全部推送、A0 今日执行回顾、零预案日严格报告、EV_NEG 报告记录、节假日日历、一字首开分类、`stats --date` 接线 correction/restore、破位四路定性、retest_confirm 生成/到期/放量收复判决、观察哨存续禁买、同均线家族冷却、`settle/watch` CLI 接线。

**v8.1 新增待实现**：

- **A1**：signals.break_qualifier 字段 + journal record_type=qualifier；settle 改读 break_qualifier；
- **A2**：signals.trigger_window_start/end 字段；strategies.yaml 的 y.when=tail_snapshot 触发 14:30–15:00 硬窗口；watch 对 ma_tomorrow 系 entry 判窗口；test_trigger_window 用例；
- **A3**：stats entry 可达性改尾盘近似，日 K 全天可达率并列；test_reachability 用例；
- **A4**：generator Z/W 同生改为“确保存在并关联”，同 symbol 同时刻一条 ACTIVE stop；
- **A5**：generator 加观察哨存续期暂停规则；
- **B1**：role 对照表 profit 改 fixed/trailing；
- **B2**：t 不记 zone_update，每日新建；
- **B3**：`holding:` → `pool:` 全文替换；
- **B4**：熔断日改写为取消延迟判决；
- **B5**：valid_policy 语义拆分（entry 次一日 / t 当日）；
- **B6**：context_marks.triggered_at 每次更新为最近；
- **B7**：卖飞率 zone 定格到 closed_at；
- **B8**：措辞改“parent 锚点＋统计分母”；
- **C2**：guard 两源对账。

---