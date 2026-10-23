# 文档一：交易信号系统技术方案（v8.0）

> 本版相对 v7.0 的实质变化：**双周期确认删除，BUY 与 STOP 统一为单点触发；pool 语义改为“关注范围”而非“持仓清单”；signals 的 role 收敛为五个并附定义对照表；context 剥离为 context_marks 轻量表；stop/profit/t 改为原地更新 zone 并记 journal。** 三层建模（单信号四状态机 / parent_id 因果链 / 报告投影）保留。

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
| 15:30 | evening 收盘生成 | 信号入库＋汇总报告（含零预案判定）＋ ACTIVE enter 的 premise 重检 |
| 9:15–9:25 | auction_call 竞价判定 | 形态分类 → 作废当日 BUY 或快照供人确认 |
| 9:30–15:00 | watch 盘中盯梢 | 触发检查 → 定性 → 提醒；context_marks 的 triggered_at 记录 |
| 14:30 | 尾盘快照 | 把此刻价格写入 tail_snapshots，供晚间 ma_tomorrow 使用 |
| 14:30 起 | restore_check T 复原倒计时 | 未配对 T 提醒（当日配对窗口） |
| 15:05 | settle 收盘清点 | EXPIRED / INVALIDATED ＋ retest_confirm 生成（按 §7.2 四路分支）＋ Z 滚动重算 |

**行情触发（watch 内部分支）：** 价格入区间 → **单点触碰即 TRIGGERED**（BUY 与 STOP 统一，不再双周期确认）；破位 → 两级定性（板块 → 指数，查 context_marks）＋封板豁免＋分歧规则；STOP 当日收复 → 纠错提醒；前提破坏 → 作废＋撤销提醒；retest_confirm 盘中触达 → 按 §7.3 判定终态。

**人触发（全部可选，不驱动状态机）：** veto；receipt（executed / ignored，可带 size_filled）；t-size；pool 编辑（唯一天然会被给的人工输入——不更新池子系统今晚没活干）；config 编辑；push-file 人工单向推送。

## 2. 项目结构

```
pointtable/
├── README.md / ARCHITECTURE.md / CHANGELOG.md / requirements.txt
├── config/
│   ├── strategies.yaml         # 全部策略（四键校验）
│   ├── config.yaml             # webhook、量能档位、监管清单、砸盘席位、阈值、节假日日历、retest 参数、reentry_rules
│   └── theme_map.yaml          # 股票→主板块 人工 overrides
├── pointtable/
│   ├── models/
│   │   ├── signal.py           # Signal + 四状态枚举 + zone校验(low<high) + role/side 枚举
│   │   ├── journal.py          # JournalEntry（record_type: transition / zone_update）
│   │   ├── receipt.py          # Receipt（executed/ignored/veto + size_filled）
│   │   ├── pool.py             # PoolEntry（date/symbol/theme_label）
│   │   └── context_mark.py     # ContextMark（index/sector 参考区间＋triggered_at）
│   ├── db/session.py           # 连接与持久化
│   ├── data/
│   │   ├── akshare.py          # 行情/竞价/龙虎榜/指数/板块指数/两融/主力资金流
│   │   ├── guard.py            # DataFreshnessGuard + 报价合理性检查 + 量比×换手口径交叉校验
│   │   └── mock.py             # 测试假数据源
│   ├── core/                   # 纯判断与纯生成：不 import data/db/notify（CI 强制）
│   │   ├── ma.py               # ma_tomorrow
│   │   ├── trend.py            # UP/DOWN
│   │   ├── volume.py           # 量比档/换手分位/主动差/break_qualifier/封板豁免/分歧规则
│   │   ├── auction.py          # 竞价形态分类（含 limit_up_break 一字首开）
│   │   ├── lhb.py              # 烂榜特征
│   │   ├── sector.py           # 板块三态 + 池内排序 + theme_map 读取
│   │   ├── selector.py         # 策略前提检查（每策略一个纯函数，独立并行）
│   │   ├── generator.py        # 前提满足集 × pool → 信号行（ENTRY/Z/W同生、三个前置检查）
│   │   └── lifecycle.py        # 单信号四状态迁移唯一入口
│   ├── jobs/
│   │   ├── evening.py / auction_call.py / watch.py / restore_check.py / settle.py
│   ├── notify/
│   │   ├── push.py             # 人工触发单向通知
│   │   └── render.py           # 槽位报告（见 §8）
│   ├── stats.py                # 两层报表
│   └── cli.py                  # receipt / veto / t-size / stats / holdings / pool / report / push-file
├── legacy/
└── tests/
    ├── fixtures/               # 川环26.5 / 博创42.3 / 兆龙60.5
    ├── test_ma.py / test_trend.py / test_volume.py / test_lifecycle.py
    ├── test_ordering.py / test_replay.py / test_deps.py / test_selector.py
    ├── test_settle_branches.py # settle 四路分支
    ├── test_reentry_checks.py  # 三个前置检查
    ├── test_guard.py           # 脏报价被 guard 拦截
    └── test_reachability.py    # 日 K 线可达校验 + unreachable 标记
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
| premise | 生成时前提快照（含 premise_facts 结构化字段） |
| trend | UP / DOWN |
| activation | 仅文案提示 |
| valid_policy | intraday / until_stopped / fixed_window |
| window_days | 仅 fixed_window 用，生成时从 config 快照 |
| status | ACTIVE / TRIGGERED / EXPIRED / INVALIDATED |
| status_reason | hit / eod / premise_broken / human_veto / retest_pass / retest_fail |
| parent_id | stop/profit/t/retest_confirm 必填，指向最近一次 TRIGGERED 的 enter；无则 `holding:<symbol>` |
| root_id | 链起点，可选（跨 reentry 保留） |
| created_at / closed_at | closed_at 为 signals 表冗余字段，“当日结案”查此字段 |

**关键变更**：**删除 `journal.confirmed` 字段**（touched_only 概念整体消失）；**删除双周期确认逻辑**；status_reason 封闭枚举不再包含 confirmed/touched_only。

### 3.2 journal

| 字段 | 说明 |
|---|---|
| id | 主键 |
| signal_id | 关联信号 |
| record_type | transition / zone_update |
| event | 仅 transition：hit / eod / premise_broken / human_veto |
| reason | 迁移原因（同 status_reason 枚举） |
| old_zone_low / old_zone_high | 仅 zone_update |
| new_zone_low / new_zone_high | 仅 zone_update |
| created_at | 时间戳 |

**zone_update 记录**：stop/profit/t 每晚滚动更新 zone 时记一条，保证历史 zone 可追溯。stats 的卖飞率/避损率、回测都靠它。

### 3.3 receipts

| 字段 | 说明 |
|---|---|
| id / signal_id | 主键 / 关联信号 |
| verdict | executed / ignored / veto |
| size_filled | 可空，默认全额 |
| created_at | 时间戳 |

**T 配对**：不加 pair_id 字段。restore_check 用“同 symbol + 同日 + T_BUY executed + T_SELL 无 executed”查询时推导。配对窗口 = 当日；T_SELL 次日触发不回溯配对。

### 3.4 pool

date / symbol / theme_label。**关注范围，不是持仓清单。** 池内每只票所有适用 role 全算全呈现；系统不判断持没持。

### 3.5 tail_snapshots

date / symbol / price。14:30 由 watch 顺手写入。ma_tomorrow 的 est_close 优先取它；无快照才 fallback 当日收盘价，生成信号日志必须标注口径（`est_close=tail_snapshot` 或 `fallback=close`）。

### 3.6 role 定义对照表（新增，争议收口）

| role | side | zone_policy | pair_required | parent | 触发方式 | 触发后行为 |
|---|---|---|---|---|---|---|
| entry | BUY | 当日有效 | 否 | 无 | 单点 | 产生持仓上下文 |
| stop | SELL | rolling（每日上移） | 否 | enter 或 holding | 单点 | 产生 retest_confirm |
| profit | SELL | fixed（目标位不跑） | 否 | enter 或 holding | 单点 | 无 |
| t | BUY/SELL | fixed（当日） | 是（当日配对） | enter 或 holding | 单点 | 配对提示 |
| retest_confirm | NEUTRAL | window(n) | 否 | enter 或 holding | 条件判定 | 链终止或纠错 |

**加新 role 的规则**：先填此表，原子组合（zone_policy / pair_required / 触发后行为）与现有五个不同才配加。

### 3.7 context_marks（新增，替代原 signals 的 index_context/sector_context）

| 字段 | 说明 |
|---|---|
| date | 日期 |
| context_type | index / sector |
| code | 指数代码或板块代码 |
| level_low / level_high | 参考区间（非触发区间） |
| label | index：UP/DOWN；sector：主线/轮动/退潮 |
| triggered_at | 可空，watch 盘中记录进入区间的时间戳 |
| created_at | 生成时间 |

**生命周期**：evening 每天生成一条 ACTIVE；watch 盘中检测进入区间则写 triggered_at；次日 evening 覆盖或置 INACTIVE。**不接 lifecycle.py，不产生 journal 迁移记录，不进 stats，不进报告买卖区。** `break_qualifier` 的两级查询改查这张表——“近30分钟 triggered_at 是否存在”。

### 3.8 time_trigger

不进 signals，留在 event_calendar 驱动 evening 提示（周五收摊、财报 T-1、节假日提前控仓）。不落信号表，不接状态机。

## 4. 判断模块（core/）

**ma.py**：`ma_tomorrow(closes, n, est_close)`。est_close 是尾盘预估收盘价，非事后真实收盘价。锚点：川环26.5 / 博创42.3 / 兆龙60.5，误差 <0.5%。

**trend.py**：收盘>20日线且5日线斜率向上 → UP，其余 DOWN。UP 挂 BUY（支撑=买点），DOWN 挂 SELL（跌破支撑=卖点不是买点）。

**volume.py**：`break_qualifier` 四路输出，两级查询改查 context_marks：

```python
def break_qualifier(change_pct, drive, vgear, context_marks) -> str:
    if limit_exempt(change_pct, code, name): return "LIMIT_EXEMPT"
    # 主动/被动两级查询：先板块 context_marks 近30分钟有无 triggered_at，再指数
    if 放量 and 主动:        return "EXECUTE_NOW"
    if 下跌 but 外盘占优:     return "DIVERGENCE"
    return "DELAY_OBSERVE"
```

**auction.py**：四类分类——weak_to_strong / strong_to_weak / limit_up_break（昨日涨停今日竞价打开，带竞价量比＋开盘涨幅快照供人确认，不写死阈值）/ neutral。

**lhb.py**：命中砸盘席位或卖方集中度过阈 → 次日 BUY 不生成；池内票 push 预警。

**sector.py**：主线/轮动/退潮三态判定；池内去弱择强排序；theme_map 读取。

**selector.py**：策略前提检查，每策略一个纯函数，独立并行、无优先级排序：

```python
def check_first_board_dip(facts) -> bool: ...      # 首板(人工标注,带日期) + 有支撑
def check_volume_breakout(facts) -> bool: ...      # 放量站上关键位 + 收阳
def check_first_move_5d(facts) -> bool: ...        # 5日线上行
def check_first_move_10d(facts) -> bool: ...       # 回调深但10日线方向未死
def check_first_move_drift(facts) -> bool: ...     # 5日线下移 + 尾盘突破明日5日线位置
```

**generator.py**：收集全部前提满足的策略 → **同票不去重，全部输出全部推送** → 过三个前置检查（见 §7.5）→ premise 校验（监管/龙虎榜/量能档位）→ ENTER 生成时 Z/W 行同生 → 池内票按趋势生成保护行与 T 行（原地更新 zone 或首次生成）→ context_marks 生成 → time_trigger 行。

**池内票每晚生成规则**：

| 趋势 | 生成/更新 |
|---|---|
| UP | stop zone 滚动上移（更新，记 zone_update journal）＋ T_BUY（支撑）＋ T_SELL（压力） |
| DOWN | stop zone 更新为支撑位，不生成 BUY 行 |
| UP→DOWN 翻转当晚 | 原 T_BUY 连坐 INVALIDATED，stop zone 更新为支撑位 |

**parent 规则**：parent_id 指向该 symbol **最近一次 TRIGGERED 的 enter**（created_at 最新）；无则 `holding:<symbol>`。**不要求 confirmed**（confirmed 字段已删）。

**lifecycle.py**：唯一 `transition(signal, event, reason)` 入口，三参数显式。四状态终态不可逆。lifecycle 只感知单信号，不感知链。

## 5. 策略与配置

```yaml
strategies:
  - id: first_move_5d
    side: BUY
    premise: 热门题材 + 5日线上行 + 未进监管 + 榜不烂
    y: { type: ma_tomorrow, n: 5, when: tail_snapshot }
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
    y: { type: ma_tomorrow, n: 5, direction: descending }
    z: { type: ma_break, n: 5 }
    w: { type: pressure_zone, action: sell_part }

  - id: first_board_dip
    side: BUY
    premise: 首板(人工标注,仅标注日次日有效) + 分时回踩 + 未进监管
    y: { type: intraday_support }
    z: { type: ma_break, n: 5 }

  - id: volume_breakout
    side: BUY
    premise: 放量站上关键位 + 收阳量能确认 + 未进监管
    y: { type: pullback, ratio: 0.98 }
    z: { type: close_below, level: breakout_price }
    w: { type: next_resistance, trailing: true }

t_overlay:
  applies_to: pool                              # 池内票全生成
  rows:
    - { side: BUY,  role: t, zone: support_minus_1pct }
    - { side: SELL, role: t, zone: resistance_minus_tick }

market_context:
  indices: [sh000001, sz399001, sh000688]
  sectors: pool 内票的 theme_map 归属，每板块一支撑一压力
```

**size_hint 修饰器（两个）**：① 量能 LOW 档全部 BUY 减半；② T_BUY 档位通过 CLI `t-size` 落库（同等/加倍/减半）。浮盈切10日线修饰器彻底删除。

**size_hint 与报告的关系**：size_hint 不在报告主槽位单独占行；量能 LOW 减半时在候选行尾附 `仓位提示：减半（量能LOW）`；T_BUY 的 t-size 结果附在池内票区 T 行尾。

**config.yaml**：

```yaml
market_volume_tiers: ...
regulator_list: ...
lhb_dumpers: ...
poll_intervals: ...           # 仅作人工触发频率建议，不驱动定时任务
watchlist_max: 25
holiday_calendar: ...         # 长假前最后交易日输出“建议空仓过节”，停止生成新 BUY

retest_window_days: 3         # 观察哨有效期（交易日）
retest_volume_min: 1.2        # 放量站回量比下限（工程近似，C级）

reentry_rules:
  same_ma_cooldown_days: 2    # 同均线档位两次止损后冷却窗口
```

无杠杆字段。**已删除 `dedup_zone_overlap`**。**已删除 `min_confirm_duration`**（双周期确认删除）。

## 6. 建模：三层结构

### 6.1 第一层：实体与四状态机（唯一的状态机层）

| 实体 | 状态 | 说明 |
|---|---|---|
| Signal（role=entry） | ACTIVE → TRIGGERED / EXPIRED / INVALIDATED | 候选买点，触达即终态 |
| Signal（role=stop） | 同上 | 池内票风控线，破位是它 TRIGGERED |
| Signal（role=profit） | 同上 | 池内票止盈参考 |
| Signal（role=t） | 同上 | 池内票做T参考，配对靠回执推导 |
| Signal（role=retest_confirm） | 同上 | 破位后由 settle 生成，三终态即“判决” |
| PoolEntry | in / out | 关注关系，不是状态机 |
| Receipt | executed / ignored / veto | 标注，不驱动迁移 |
| ContextMark | ACTIVE / INACTIVE | 轻量环境标记，不走 lifecycle |

`lifecycle.py` 的 `TRANSITIONS`：

```python
TRANSITIONS = {
    ("ACTIVE", "hit"):            "TRIGGERED",
    ("ACTIVE", "eod"):            "EXPIRED",
    ("ACTIVE", "premise_broken"): "INVALIDATED",
    ("ACTIVE", "human_veto"):     "INVALIDATED",
}
```

**TRIGGERED 语义**：**单点触碰即触发**。BUY 与 STOP 统一，不再双周期确认。防重复推送由状态机终态保证——TRIGGERED 不可逆，跑一百次 watch 也不会重复喊。

### 6.2 第二层：因果链（parent_id 接力，不是状态迁移）

- **边由 parent_id 表示**：stop/profit/t/retest_confirm 必须指向最近一次 TRIGGERED 的 enter；无 enter 履历用 `holding:<symbol>`。
- **边的产生由 settle / generator 实现**。
- **lifecycle.py 不感知链**。
- **S0–S7 是报告叙事章节，不是状态机**。竞价确认、持仓、破位当天、判决后选股不是信号状态。

### 6.3 第三层：报告槽位投影

`report --date` 只读不写，按 §8 投影。

## 7. 破位-反抽-再进场链的因果规则

### 7.1 破位当天定性

STOP 单点触碰即 TRIGGERED，随后 `break_qualifier` 四路输出。定性只写 journal 的 status_reason 和推送文案。

| 定性 | 推送文案 | 当天动作建议 |
|---|---|---|
| EXECUTE_NOW | “破位，撤” | 当天走 |
| DELAY_OBSERVE | “缩量破位，疑似被动，收盘定生死” | 等收盘 |
| DIVERGENCE | “下跌但外盘占优，有承接，看不懂，收盘复核” | 等收盘 |
| LIMIT_EXEMPT | “封板附近失真，不定性” | 不判 |

### 7.2 收盘定生死（settle.py 四路分支）

```python
for stop in today_triggered_stops:
    q = stop.status_reason
    if q in ("DELAY_OBSERVE", "DIVERGENCE"):
        if close < stop.zone_low:
            create_retest_confirm(stop)      # 收盘仍破 → 生成
        else:
            push_correction_notice(stop)     # 收盘收复 → 纠错提醒，不生成
    elif q == "EXECUTE_NOW":
        create_retest_confirm(stop)          # 当天执行也生成：给纠错留门
    # LIMIT_EXEMPT → 不生成，次日晚上正常生成新 STOP
```

**四条硬约束**：EXECUTE_NOW 也生成；DELAY/DIVERGENCE 同一段逻辑双向；LIMIT_EXEMPT 不生成；生成时 zone 取原 STOP 的 zone，parent 指向原 enter 或 `holding:<symbol>`，valid_policy=fixed_window，window_days 从 config 快照。

### 7.3 retest_confirm 三终态

| 终态 | 判定依据 | 结案时刻 | 判定者 |
|---|---|---|---|
| TRIGGERED | 盘中摸进观察区间、收盘没站上区间上沿 | 盘中某次 watch（事件） | watch |
| INVALIDATED | 收盘站上区间上沿且量比 ≥ `retest_volume_min` | 盘中最后一次 watch 或收盘判定 | watch |
| EXPIRED | 窗口到期，价格从没摸进区间 | 到期日 15:05 settle | settle |

**“当日结案”查法**：`status in 终态 且 signals.closed_at 日期 = 报告日期`。不查 journal。

### 7.4 纠错提醒统一

两条路径共用 `push_correction_notice()`：破位当日盘中收复（watch）、retest_confirm 放量站回（watch 判 INVALIDATED）。文案一致、不迁移原 STOP 状态、不发新买点。

### 7.5 判决后重新选股：三个前置检查（替代 depth）

```python
# 检查一：观察期存续 → 不出买点
if has_active_retest_confirm(symbol):
    skip_buy()

# 检查二：同均线档位近 N 日两次 STOP → 该均线家族暂停
if same_ma_stop_count(symbol, ma_n=5, days=cfg.reentry_rules.same_ma_cooldown_days) >= 2:
    exclude(["first_move_5d", "first_move_drift"])

# 检查三：观察期结案 + 价格位置
# 结案（TRIGGERED/EXPIRED/INVALIDATED 任一终态）→ 选股恢复
# 价格在哪根均线附近，哪个策略前提自然满足就出哪个
```

“破5卖→10日再买”是检查三在价格走到10日线时的自然结果。

| 路径 | 检查一 | 检查二 | 结果 |
|---|---|---|---|
| 破5 → 反抽摸不过 → 确认破位 → 跌到10日线 | 结案后放行 | 5日线档暂停中 | **10日线先手前提满足 → 新买点** ✓ |
| 破5 → 放量站回 → 人纠错买回 | 结案后放行 | 无两次止损记录 | 价格远离均线，前提不满足 → **不出信号，等** ✓ |
| 破5 → 观察期里价格横跳 | 存续中挡住 | — | **零买点输出** ✓ |

### 7.6 因果规则表

| 触发 | 条件 | 产生 | parent_id | 实现 |
|---|---|---|---|---|
| enter TRIGGERED | 回执入场 | 持仓上下文 | — | evening |
| 池内票，每晚 evening | 趋势 UP/DOWN | 更新 stop、t、profit 行 | → 最近 TRIGGERED enter | generator |
| stop TRIGGERED | EXECUTE_NOW | retest_confirm | → 原 enter 或 holding | settle |
| stop TRIGGERED | DELAY/DIVERGENCE 且收盘<zone_low | retest_confirm | 同上 | settle |
| stop TRIGGERED | DELAY/DIVERGENCE 且收盘收复 | 纠错提醒（无新信号） | — | settle |
| stop TRIGGERED | LIMIT_EXEMPT | 不产生 retest_confirm | — | settle |
| retest_confirm TRIGGERED | 反抽不过 | 链终止，等新 enter | — | watch |
| retest_confirm INVALIDATED | 放量站回 | 纠错提醒；人买回需新 enter | — | watch |
| retest_confirm EXPIRED | 到期无反抽 | 链终止，等新 enter | — | settle |
| 任意时刻 | 三个前置检查通过＋价格到位 | 新 enter | 无（新链起点） | generator |

## 8. 报告形态与槽位投影

| 槽位 | 投影规则 |
|---|---|
| A0 今日执行回顾 | 当日进入 TRIGGERED 的全部 Signal ＋ receipts ＋机械 vs 实际差异 |
| 环境行 | context_marks（ACTIVE）＋ event_calendar |
| 候选区 | ACTIVE 的 entry，经三个前置检查过滤；同票多策略全部列出不合并；超 watchlist 上限标 truncated |
| 池内票区 | 池内票的 ACTIVE stop/profit/t（原“持仓区”改名） |
| 破位区 | 当日进入 TRIGGERED 的 stop |
| 观察区 | ACTIVE 的 retest_confirm |
| 判决区 | 当日 closed_at 的 retest_confirm 终态 |

**关键约束**：

1. **同一只票可同时在候选区和池内票区出现**，不折叠。候选区说“这个位置是买点”，池内票区说“这个位置是保护线”，系统不知道你持没持，两条都给。
2. **观察区与判决区互斥**：同一条 retest_confirm 同日只在其中一处。
3. **判决区只显示当天刚结案的**；前一日结案的不再出现，回看走“近期已结案”小节。

**零预案日两个变体**：市场熔断型（LOW+指数DOWN，STOP 收紧为破位即 SELL）；节假日型（长假前最后交易日，停止新 BUY）。

## 9. 统计（两层）

**信号质量层（纯行情可算）**：

- **触达率 = 可达信号数 / 生成数**。可达判定用**当日完整日 K 线**，按 role/side 分规则：
  - entry（BUY）：当日最低价 ≤ zone_high
  - stop（SELL）：当日最低价 ≤ zone_low
  - profit（SELL）：当日最高价 ≥ zone_low
  - t：按 side 分别套 entry/profit 规则
  - retest_confirm：当日最高价 ≥ zone_low
- **不可达但盘中触发的，标 `unreachable`**：不进假想期望分子分母，单独统计“误触发率”（= unreachable / 总 TRIGGERED），作为 guard 效果审计指标。
- 分策略假想期望：触发价机械执行、满30笔起算、滚动50笔期望≤0 → EV_NEG 仅记录不推送。
- 卖飞率 vs 避损率：STOP 后追踪5日，5日内收盘收复 zone_high 计纠错命中，两数同出一张表。
- 纠错提醒命中率。
- 池子质量报表。
- 合成 parent（`holding:` 前缀）的信号链不进统计。

**行为层（回执驱动，覆盖率<50% 明示“数据不足”）**：纪律执行率；做T增值粗算；复原违规次数。

**明确删除**：账户盈亏、连续亏损、违规计数、杠杆提醒、浮盈计算。

## 10. 测试与验收

test_ma / test_trend / test_volume（四路＋封板＋分歧＋context_marks 两级查询）/ test_selector（五策略各一条正向、no_strategy、互不耦合）/ test_lifecycle（四状态全生命周期、T 连坐、合成 parent 不进统计）/ test_ordering / test_replay（无网络）/ test_deps（core 不得 import data/db/notify）/ test_guard（脏报价被拦截不触发）/ test_reachability（日 K 线可达校验 + unreachable 标记）/ test_settle_branches / test_reentry_checks。

**验收锚点—测试文件映射**：

| 验收锚点 | 测试文件 |
|---|---|
| 缩量/分歧收盘仍破 → 生成 retest_confirm；收盘收复 → 只发纠错 | test_settle_branches |
| EXECUTE_NOW 当天执行仍生成 retest_confirm | test_settle_branches |
| LIMIT_EXEMPT 不生成，次日正常生成 STOP | test_settle_branches |
| 纠错两条路径文案一致、不迁移状态、不发买点 | test_lifecycle |
| TRIGGERED / INVALIDATED＋纠错 / EXPIRED 各一条 | test_lifecycle |
| 观察期存续时选股对该票零输出 | test_reentry_checks |
| 结案且价格到位后 first_move_10d 自动出现 | test_reentry_checks |
| 同均线两次止损后 5日线家族暂停、10日线不受影响 | test_reentry_checks |
| 同票多策略全部输出全部推送，无合并 | test_selector |
| 同一条信号跑多次 watch 只推送一次 | test_lifecycle |
| 脏报价被 guard 拦截不产生触发 | test_guard |
| 日 K 线不可达但盘中触发 → 标 unreachable，不进假想期望 | test_reachability |
| depth 删除后无残留调用 | test_deps |
| 无状态机非法迁移 | test_lifecycle |

## 11. 系统边界

| 不做 | 原因 |
|---|---|
| 选股/题材判断 | “没啥指标，就一眼感觉”。pool 落表并审计质量，不判断 |
| 支撑压力精密算法 | 机械代理＋premise 标注，人可 veto |
| 日内纠错决策 | 只发收复提醒，追不追是人 |
| 破位后“利润或纪律”的二选一 | 系统不知道你的利润厚度，不做、不近似 |
| 个股与大盘压力冲突优先级 | “人是活的” |
| 全市场板块扫描 | 只做池内板块 context_marks |
| 账户追踪/自报/熔断器/杠杆提醒 | 系统不知道账户。唯一约束是市场熔断 |
| 定时任务/按钮回传/自动推送 | 纯 CLI 人工触发 |
| 双周期确认 / 盘中计时确认 | 采样粒度由人控制，秒级毛刺物理上看不见；防毛刺职能被 guard 承担 |
| LLM / 网格回测 / 基本面闸门 / 真实仓位建模 | 将来以新 role 或新报表增量加入，不动状态机和表结构 |

## 12. 废弃项

| 废弃项 | 原因 |
|---|---|
| depth 参数 | 选股由三个前置检查＋价格位置决定 |
| `dedup_zone_overlap` | 生成层不合并，同票多策略全部输出全部推送 |
| “三道闸/闸门”叫法 | 统一为“前置检查” |
| 浮盈切10日线修饰器 | 系统不知道真实利润厚度 |
| **双周期确认** | 纯 CLI 采样粗，防毛刺职能由 guard 承担；保留会产生系统性漏报 |
| **`journal.confirmed` 字段** | touched_only 概念整体消失 |
| **`first_in_zone_at` / `min_confirm_duration`** | 时间戳方案作废 |
| **signals 的 index_context / sector_context / time_trigger** | 剥到 context_marks / event_calendar |
| **signals.tag 8 值平铺** | 改为 role 5 值 + side 双字段 |

## 13. 当前实施状态

已实现：市场输入与尾盘快照、selector 按事实独立选策、volume_breakout 独立 assignment、同票多策略不去重全部推送、A0 今日执行回顾、零预案日严格报告、EV_NEG 报告记录、节假日日历、一字首开分类、`stats --date` 接线 correction/restore、破位四路定性、retest_confirm 生成/到期/放量收复判决、观察哨存续禁买、同均线家族冷却、`settle/watch` CLI 接线。

**v8.0 新增待实现**：删除双周期确认与 `journal.confirmed`；guard 报价合理性检查；stats 可达校验改日 K 线并加 unreachable 标记；signals 的 role 五值收敛 + side 双字段；context_marks 表 + break_qualifier 两级查询改造；stop/profit/t 原地更新 zone + zone_update journal；settle 四路分支写全；纠错提醒统一函数；三个前置检查在 generator 中并列实现；config 补 `retest_window_days` / `retest_volume_min` / `reentry_rules`；删除 `dedup_zone_overlap`；depth 残留清理；报告“持仓区”改“池内票区”，同一只票候选区与池内票区不折叠；判决区按 signals.closed_at 过滤。

---