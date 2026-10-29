
# **文档一：交易信号系统技术方案（v8.2）**

## **0. 定位与铁律**

一个**信号服务**：每晚收盘后计算点位、入库；盘中价格触达点位提醒；信号过期或前提作废时撤销提醒。不下单、不连交易接口、不接 LLM、不建模真实仓位、不记账、不知道你的账户。纯后端 CLI 驱动，无定时任务自动推送，无按钮回传。

四条铁律：

1. **pool 是关注范围，不是持仓清单。**池内每只票所有适用 role 全算全呈现；系统不知道、不推断你的实际持仓。同一只票同一晚可以同时有 entry 和 stop，两条都给，由人对号入座。
2. **信号一生只有四个状态：ACTIVE → TRIGGERED / EXPIRED / INVALIDATED。**系统只记录客观触发，不记录或推断人工执行。
3. **策略就是 strategies.yaml 里的几段配置。**每条策略必须写全 premise / y / z / w 四键，缺一加载即报错。加打法＝加一段 YAML。
4. **报告只投影事实，不重算业务分支。**触发、定性、熔断覆盖、收盘结算、保护行暂停与恢复，必须先在业务层落库，renderer 不得自行发明第二套判断逻辑。

### **0.1 日期与快照口径**

- `T = as_of_trade_date`：报告的数据基准交易日；
- `R = plan_for = next_trade_day(T)`：计划服务的次一交易日；
- `report --date R`：读取 T 日 settle 与 evening 完成后的快照，生成 R 日计划。

例如《2026-10-28 交易计划》的基准日是 2026-10-27。

| 槽位 | 时间口径 |
|---|---|
| A0 触发回顾 | T 日真实发生的 TRIGGERED 事件，读触发时快照 |
| 破位区 | T 日触发的 stop 及其 T 日结算结果 |
| 收盘处理 | T 日 stop 结算与观察哨结案汇总 |
| 判决区 | closed_at 属于 T 日的 retest_confirm |
| 候选区 | T 晚生成、valid_for=R 的 ACTIVE entry |
| 池内票区 | T 晚 evening 后供 R 日使用的信号快照 |
| 环境行 | T 日行情与 context_marks 快照 |

约束：

- T 日 settle/evening 未完成时只能生成 preview，并标记“未结算”；
- A0 不要求与 T 晚最终 ACTIVE 集一致：旧 stop 盘中触发、晚间重建新 stop，是正常生命周期；
- 历史报告不得用 signals 当前行覆盖触发时或结算时事实；
- 新旧信号通过 `signal_id、created_at、valid_for、closed_at、root_id` 解释接替关系。

## **1. 行为建模**

全部行为按触发源分三类。时钟为建议时刻，实际全部由人工 CLI 触发。

**时钟触发（routines）：**

| 时刻 | 行为 | 输出 |
|---|---|---|
| 9:15–9:25 | auction_call 竞价判定 | 形态分类 → 作废当日 BUY 或快照供人确认 |
| 9:30–15:00 | watch 盘中盯梢 | 触发检查 → 定性 → 提醒；context_marks 更新 triggered_at |
| 14:30 | 尾盘快照 | 写 tail_snapshots；ma_tomorrow 系 entry 触发窗口开启 |
| 14:30 起 | restore_check T 复原倒计时 | 当日有效、未配对 T 的提醒 |
| 15:05 | settle 收盘清点 | EXPIRED / INVALIDATED、stop 结算、retest 生成与到期、纠错事件 |
| 15:30 | evening 收盘生成 | 次一交易日信号入库、保护行生成或更新、前提重检 |
| evening 后 | report | 只读投影 |

**行情触发（watch 内部分支）：**

- **entry 触发**：单点触碰，且满足 `valid_for` 与 trigger_window。ma_tomorrow 系仅在有效日 14:30–15:00 接受触发；窗口外触碰忽略、不推送。first_board_dip / volume_breakout 全天有效。
- **stop/profit/t 触发**：单点触碰即触发；t 须处于 `valid_for`。
- **破位定性**：两级查询板块、指数 context_marks，叠加封板豁免与量价事实，写入原始 `break_qualifier`。
- **stop 当日收复**：仅非熔断日的 DELAY/DIVERGENCE 分支产生纠错事件。
- **EXECUTE_NOW 后反抽**：反抽回原止损线附近产生纠错事件，不生成观察哨。
- **前提破坏**：作废并产生撤销提醒。
- **retest_confirm 判定**：按 §7.5 形成三终态及判决事实。

**人触发（可选，不驱动状态机）：**t-size；pool 编辑；config 编辑；push-file 人工推送；replay 历史回放。

## **2. 项目结构**

```text
pointtable/
├── config/
│   ├── strategies.yaml / config.yaml / theme_map.yaml
├── pointtable/
│   ├── models/
│   │   ├── signal.py / journal.py / settlement.py / correction.py
│   │   ├── pool.py / context_mark.py / snapshot.py
│   ├── data/  akshare.py / guard.py / mock.py
│   ├── core/  ma / trend / volume / auction / lhb / sector
│   │        selector.py / generator.py / lifecycle.py
│   ├── jobs/  evening / auction_call / watch / restore_check / settle / replay
│   ├── notify/ push.py / render.py
│   ├── stats.py / cli.py
└── tests/
```

`core/` 为纯判断与纯生成，不 import data/db/notify，CI 强制。

## **3. 数据模型**

### **3.1 signals**

| 字段 | 说明 |
|---|---|
| id / symbol / strategy_id | 主键 / 标的 / 策略 |
| side | BUY / SELL / NEUTRAL |
| role | entry / stop / profit / t / retest_confirm |
| zone_low / zone_high | 强制区间，low < high |
| size_hint / premise / trend | 仓位提示 / 前提快照 / 趋势 |
| break_qualifier | 可空，仅 stop；保存盘中原始定性，不被结算覆盖 |
| trigger_window_start / end | 可空；ma_tomorrow 系 entry 为 14:30–15:00 |
| valid_policy | intraday / until_stopped / fixed_window |
| created_trade_date | 生成所属交易日 |
| **valid_for** | **entry、t 必填；允许触发的交易日** |
| window_days | fixed_window 使用，生成时从 config 快照 |
| status / status_reason | 四状态 / hit、eod、premise_broken、retest_pass、retest_fail、stop_triggered |
| parent_id / root_id | 指向最近 TRIGGERED enter，无则 `pool:<symbol>`；root 跨 reentry 保留 |
| created_at / closed_at | 创建与终态时间 |

**有效期语义：**

| role | 生成 | 有效 | 到期 |
|---|---|---|---|
| entry | D 日 evening | R=valid_for 当日 | R 日 settle 未触发则 EXPIRED |
| t | D 日 evening | R=valid_for 当日 | R 日 settle 处理到期与未配对 |
| stop/profit | evening 或滚动更新 | 直至触发、作废或被替换 | 不按单日 eod |
| retest_confirm | 破位结算时 | fixed_window | 按三终态结案 |

硬约束：

- watch、restore_check、settle 均按 `valid_for` 判断是否属于当前交易日；
- 旧 SAME_DAY 的 T 数据兼容读取，新信号一律写 `valid_for`；
- 终态不可逆；已终态 stop 不得再执行 zone_update，需要新点位时创建新 signal_id；
- 同 symbol 同时刻最多一条 ACTIVE stop。

### **3.2 journal**

| 字段 | 说明 |
|---|---|
| signal_id / record_type | transition / zone_update / qualifier |
| event / reason / qualifier | 迁移事件 / 原因 / 原始定性 |
| old/new_zone | zone_update 使用 |
| **event_source** | **live_watch / live_settle / replay / backfill** |
| occurred_at / recorded_at | 业务发生时间 / 实际落库时间 |
| zone_snapshot_id | 触发或迁移时区间快照 |
| run_id / code_version / input_snapshot_id | 回放与审计信息 |

规则：transition 每次迁移记一条；zone_update 仅 ACTIVE stop/profit 滚动时记；qualifier 在 stop 触发后写原始定性时记；T 每晚新建不记 zone_update；重放不得改写 occurred_at 冒充实时执行。

### **3.3 stop_settlements**

stop 收盘结算独立落库：

| 字段 | 说明 |
|---|---|
| signal_id / trade_date | 对应 stop / 结算交易日 |
| raw_qualifier | 原始定性快照 |
| effective_qualifier | 弱市覆盖后的有效定性 |
| circuit_day | 当日是否熔断日 |
| close_price / zone_snapshot_id | 结算收盘价 / 使用区间快照 |
| settlement_outcome | EXECUTE_NOW / RECOVERED_CORRECTION / CREATE_RETEST / LIMIT_EXEMPT |
| data_status | OK / STALE / MISSING / ANOMALOUS |
| correction_scene | 可空；当日收复时为 same_day_recover |
| event_source / occurred_at / recorded_at | 来源与时间 |
| code_version / input_snapshot_id | 审计版本 |

约束：同一信号同一交易日只有一个有效 live_settle 结果；重复 settle 幂等；数据不足时 outcome 暂空、报告标“待补数据”，不得默认走 DELAY_OBSERVE。

### **3.4 correction_events**

纠错是事件，不是 Signal，不是状态机。

| 字段 | 说明 |
|---|---|
| source_signal_id | 来源 stop 或 retest_confirm |
| scene | same_day_recover / after_execute_now / retest_invalidated |
| trade_date / occurred_at | 所属交易日 / 条件成立时间 |
| notice_status | pending / pushed / suppressed / failed |
| dedup_key / event_source | 幂等 / 来源 |

只有三种场景属于纠错。当日执行、生成观察哨、LIMIT_EXEMPT 都不是纠错。只有 `notice_status=pushed` 才能写“已推送”。

### **3.5 其余实体**

- **pool**：date / symbol / theme_label。关注范围，不是持仓清单。
- **tail_snapshots**：date / symbol / price，14:30 写入；ma_tomorrow 优先取它，fallback 收盘价须标注口径。
- **context_marks**：date / context_type / code / level_low / level_high / label / triggered_at / **data_status**。evening 生成，watch 更新 triggered_at；不接 lifecycle，不进分策略统计；**过期 context 不参与破位两级查询**。
- **market_snapshots**：trade_date、指数收盘与趋势、量能与档位、**circuit_day**、circuit_rule_version、两源对账状态。circuit_day 是市场交易日级事实，同一交易日同一规则版本只有一个有效值。
- **zone_snapshots**：初始区间、每次滚动区间、触发时区间、结算使用区间。

指数显示名固定映射：`sh000001=上证指数，sz399001=深证成指，sh000688=科创50`。

### **3.6 role 定义对照表**

| role | side | zone_policy | pair | parent | 有效与触发窗口 | 触发后行为 |
|---|---|---|---|---|---|---|
| entry | BUY | 次日单点区间 | 否 | 无 | valid_for=R；ma_tomorrow 系 14:30–15:00，其他全天 | 成为保护行 parent 锚点＋统计分母 |
| stop | SELL | 按趋势滚动 | 否 | enter 或 pool | 全天 | 写原始定性，进入收盘结算 |
| profit | SELL | fixed 或 trailing | 否 | enter 或 pool | 全天 | 无 |
| t | BUY/SELL | fixed，次日有效 | 是 | enter 或 pool | valid_for=R，全天 | 当日配对提示 |
| retest_confirm | NEUTRAL | window(n) | 否 | enter 或 pool | 条件判定，全天 | 链终止、纠错或恢复保护行 |

加新 role 前先填此表，原子组合与现有五个不同才允许增加。

## **4. 判断与生成模块（core/）**

- **ma.py**：`ma_tomorrow(closes, n, est_close)`。
- **trend.py**：收盘高于 20 日线且 5 日线斜率向上为 UP，其余 DOWN。
- **volume.py**：输出原始定性与数据状态。缺量比、主动性等必要事实时返回“定性待定＋data_status”，**不得把数据不足默认当成 DELAY_OBSERVE**；客观价格触发已成立时 stop 仍可 TRIGGERED，但结算标记待补数据。
- **auction.py**：weak_to_strong / strong_to_weak / limit_up_break / neutral。
- **lhb.py**：命中砸盘席位或集中度过阈，次日 BUY 不生成；池内票预警。
- **sector.py**：主线/轮动/退潮；池内去弱择强；theme_map 读取。
- **selector.py**：每策略一个纯函数，独立并行、无优先级排序。
- **generator.py**：
  - 同票多策略不去重，全部输出全部推送；
  - 过三个前置检查（§7.7）与 premise 校验；
  - **EV_NEG 只打标、不过滤**；
  - entry 生成时确保同 symbol 最多一条 ACTIVE stop：已有则重锚 parent，没有则按 Z/W 同生新建；已终态 stop 不参与重锚或滚动；
  - 按 §7.4 矩阵生成池内信号；UP 且无暂停条件时生成 stop/profit/t，T 未生成必须记录结构化原因；
  - 生成 context_marks 与 event_calendar 行。

**区间不变量：**

1. 所有信号 `zone_low < zone_high`；
2. entry 失效阈值不得高于买入区间下沿；失效文案必须说明数值来源（明日 MA／区间下沿／MA×系数），不得张冠李戴；
3. T_BUY 与 T_SELL 分别由支撑、压力独立计算，不得由另一方机械拼接；T 区间宽度受配置上限约束；
4. stop 盘中 TRIGGERED 后，同 symbol 未触发的 ACTIVE T_BUY 连坐 INVALIDATED（reason=stop_triggered），restore_check 同步停止；
5. entry 与池内 stop 区间重合允许存在，报告提示“空仓看买点，池内保护看 stop”。

**lifecycle.py**：唯一 `transition(...)` 入口，四状态终态不可逆，只感知单信号、不感知链。

## **5. 策略与配置**

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
    y: { type: ma_tomorrow, n: 5, direction: descending, when: tail_snapshot }
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
  applies_to: pool
  valid_session: next_trade_day
  rows:
    - { side: BUY,  role: t, zone: support_minus_1pct }
    - { side: SELL, role: t, zone: resistance_minus_tick }

market_context:
  indices: [sh000001, sz399001, sh000688]
  sectors: pool 内票的 theme_map 归属，每板块一支撑一压力
```

**size_hint 修饰器（两个）**：量能 LOW 档全部适用 BUY 减半；T_BUY 档位通过 CLI `t-size` 落库。LOW 只调仓位提示，不删除候选，不与 EV_NEG 混用。

```yaml
watchlist_max: 25
retest_window_days: 3
retest_volume_min: 1.2
reentry_rules:
  same_ma_cooldown_days: 2
stats:
  entry_reachability_mode: tail_approx
  tail_approx_tolerance: 0.003
guard:
  two_source_deviation_max: 0.005
  quote_sanity: enabled
circuit_breaker:
  volume_tier: LOW
  index_trend: DOWN
  benchmark_index: sh000001
report:
  require_settled: true
```

无杠杆字段。已删除 `dedup_zone_overlap`、`min_confirm_duration`。

## **6. 三层建模**

**第一层：实体与四状态机。**五种 Signal＋PoolEntry＋ContextMark，见 §3。

**第二层：因果链。**parent_id 连接保护行与最近 TRIGGERED enter；root_id 跨 reentry 保留；settle/generator 产生边；lifecycle 不感知链；收盘处理与纠错事件都不是新状态机。

**第三层：报告槽位投影。**`report --date` 只读不写，按 §8 投影。主报告与审计报告只有排版详略差异，不得有两套业务计算。

## **7. 破位—反抽—再进场链**

### **7.1 盘中原始定性**

STOP 单点触碰即 TRIGGERED，写入原始 `break_qualifier` 与 qualifier journal。

| 原始定性 | 推送文案 | 初步处理 |
|---|---|---|
| EXECUTE_NOW | 破位，撤 | 当天执行，不立哨 |
| DELAY_OBSERVE | 缩量破位，疑似被动 | 非熔断日等收盘 |
| DIVERGENCE | 下跌但外盘占优，有承接 | 非熔断日等收盘 |
| LIMIT_EXEMPT | 封板附近失真 | 不判，不立哨 |

### **7.2 熔断日统一分支**

```python
def resolve_effective_qualifier(raw, circuit_day):
    if circuit_day and raw in {"DELAY_OBSERVE", "DIVERGENCE"}:
        return "EXECUTE_NOW"
    return raw
```

- `circuit_day` 从 market_snapshots 读取，按 config 的量能档位＋基准指数趋势计算；
- 同一市场日不得按股票或运行时刻得出不同结论；
- **熔断日 DELAY/DIVERGENCE 无论收盘仍破或收复，均按 EXECUTE_NOW 结算：不立观察哨、不走普通当日收复纠错**；
- 原始 break_qualifier 不被覆盖，结算记录保留 raw/effective/circuit_day 供审计。

### **7.3 收盘结算**

```python
def settle_stop(stop, close, day):
    raw = stop.break_qualifier
    effective = resolve_effective_qualifier(raw, day.circuit_day)
    if effective == "EXECUTE_NOW":
        return "EXECUTE_NOW"
    if raw == "LIMIT_EXEMPT":
        return "LIMIT_EXEMPT"
    if close < stop.zone_low:
        return "CREATE_RETEST"
    return "RECOVERED_CORRECTION"
```

| settlement_outcome | 后续动作 |
|---|---|
| EXECUTE_NOW | 当日执行，不立哨，不产生当日收复纠错 |
| CREATE_RETEST | 生成 retest_confirm，zone 取规定快照，parent 指向原 enter 或 pool |
| RECOVERED_CORRECTION | 当日假破位，生成 same_day_recover 纠错事件 |
| LIMIT_EXEMPT | 不立哨，次日晚正常生成保护行 |

收复判定口径为 `close >= zone_low`，报告写“收复止损下沿”，不写“收复整个止损区”。

### **7.4 池内保护行矩阵（唯一定稿）**

| 前序状态或结算结果 | 观察哨 | 下一 evening 的保护行策略 |
|---|---|---|
| ACTIVE retest_confirm | 存续 | 暂停普通 stop/profit/t |
| 非熔断 DELAY/DIVERGENCE 收盘仍破 | 新建 | 按观察哨存续处理 |
| 非熔断 DELAY/DIVERGENCE 收盘收复 | 不建 | 恢复正常生成 |
| 原始或有效 EXECUTE_NOW | 不建 | **暂停普通保护行，等待新 enter** |
| LIMIT_EXEMPT | 不建 | 恢复正常生成 |
| retest TRIGGERED | 已结案 | 暂停普通保护行，等待新 enter |
| retest EXPIRED | 已结案 | 暂停普通保护行，等待新 enter |
| **retest INVALIDATED** | 已结案 | **恢复正常生成，不强制等待新 enter** |
| 新 enter TRIGGERED | 无存续观察哨 | 确保 stop 存在并关联，恢复普通生成 |

约束：

- “恢复正常生成”指重新进入普通规则，不保证所有 role 必然生成；趋势或数据条件不满足时记录原因；
- 等待新 enter 期间，新 entry 仍可按 Z/W 同生 stop；
- 所有重建使用新 signal_id，旧终态 stop 不原地更新；
- 同 symbol 同时刻最多一条 ACTIVE stop；
- 暂停与恢复由统一的纯函数判定，输出 `NORMAL / WATCHING / WAIT_REENTRY / BLOCKED` 及结构化 reason，供 evening 与报告共用。

### **7.5 retest_confirm 三终态**

| 终态 | 判定依据 | 结案者 | 必须保存的判决事实 |
|---|---|---|---|
| TRIGGERED | 曾进入观察区间，收盘未站上区间上沿 | watch | 观察窗口、期间最高价、入区事实、结案日收盘与上沿 |
| INVALIDATED | 收盘站上区间上沿且量比 ≥ retest_volume_min | watch | 收盘价、上沿、量比、观察窗口 |
| EXPIRED | 窗口到期，期间从未进入观察区间 | settle | 起止交易日、期间最高价、从未入区事实 |

“未形成有效反抽”不能单独作为区分 TRIGGERED 与 EXPIRED 的依据。当日结案按 `closed_at 日期=T` 查询。

> 实现注记：TRIGGERED / INVALIDATED 的判定依据均为**收盘事实**（收盘价、收盘量比），收盘处理时点才可得，故三终态统一由 settle 收盘结案；watch 盘中只累积入区事实与期间最高价（`premise.retest_observation`），不迁移状态。终态归属与判决事实口径与上表一致。

### **7.6 纠错统一**

| scene | 触发条件 | 报告归属 |
|---|---|---|
| same_day_recover | 非熔断日 DELAY/DIVERGENCE 当日收复 | 破位条目＋收盘处理汇总 |
| after_execute_now | EXECUTE_NOW 后反抽回原止损线附近 | 破位区纠错附项 |
| retest_invalidated | retest 放量站回判 INVALIDATED | 判决区对应条目 |

共同边界：不迁移原 stop 状态；不生成 entry；不因纠错本身生成观察哨；不推断人工买回；重复 watch/settle/report 不重复推送、不重复统计。

### **7.7 判决后重新选股：三个前置检查**

```python
# 检查一：观察期存续，不出买点
if has_active_retest_confirm(symbol):
    skip_buy()

# 检查二：同均线档位近 N 日两次 STOP，暂停该均线家族
if same_ma_stop_count(symbol, ma_n=5, days=cfg.reentry_rules.same_ma_cooldown_days) >= 2:
    exclude(["first_move_5d", "first_move_drift"])

# 检查三：结案及价格位置，满足前提的策略自然出现
```

冷却归族口径：按 stop 的 z 配置均线周期或关联 entry 策略家族确定；报告中的“对应池内”是 parent 来源，不是 strategy_id；合成 parent 链不进收益统计，但其止损事实可用于生成层冷却；EV_NEG 不参与过滤。

## **8. 报告形态与槽位投影**

| 槽位 | 投影规则 |
|---|---|
| 环境行 | T 日指数、有效 context_marks、事件日历及数据异常 |
| 候选区 | valid_for=R 的 ACTIVE entry；同票多策略不合并；附失效依据、冷却、仓位提示 |
| 池内票区 | T 晚供 R 日使用的 stop/profit/t；缺失时显示结构化原因 |
| A0 触发回顾 | T 日真实 TRIGGERED 事件，读触发时快照；主报告可汇总，审计版列明细 |
| 破位区 | T 日触发 stop，依次展示原始定性、有效定性、收盘结算 |
| 收盘处理 | T 日 stop 结算与观察哨结案；主报告默认计数与分组汇总，审计版列明细 |
| 观察区 | T 晚仍 ACTIVE 的 retest_confirm |
| 判决区 | closed_at 属于 T 日的 retest 终态及判决事实 |

关键约束：

1. 候选区与池内票区不折叠；
2. 同一 retest_confirm 不得同时出现在观察区与判决区；
3. 判决区只显示当天结案，回看走“近期已结案”；
4. **收盘处理不等于纠错**；不设顶级“纠错区”；
5. 破位区与收盘处理区不得在主报告中重复整表明细；
6. A0 与最终 ACTIVE 集不同不构成错误。

## **9. 统计**

**信号质量层：**

- 区分 `reachable`（行情可达）、`price_touched`（价格入区）、`TRIGGERED`（状态机迁移）、`unreachable`（统计不可达却有触发记录）；
- **有效日尚未结束时，只显示基准收盘与区间位置，不输出未触达/不可达结论**；
- entry（ma_tomorrow 系）主口径：有效日收盘价处于 zone ± tail_approx_tolerance；辅口径：有效日最低价 ≤ zone_high，并列输出；其余 role 按 v8.1 口径；
- 两源对账异常日，unreachable 率不纳入审计；
- 分策略假想期望满 30 笔起算，滚动 50 笔期望≤0 标 EV_NEG；**EV_NEG 主体只能是 strategy_id**，不得是“池内”或 `pool:<symbol>`；合成 parent 链不进分策略收益统计；
- STOP 后 5 日卖飞率/避损率使用 closed_at 时点 zone 快照；
- replay/backfill 与 live 分开统计，不混入正式触发质量报表。

**纠错统计：**只读取 correction_events，按 scene 分类。收盘处理记录总数不得作为纠错次数；重复报告不增加事件数；推送成功率按 notice_status 单独统计。

**行为层：**做 T 增值粗算、复原违规次数、池子质量报表。明确不做账户盈亏、连续亏损、真实仓位违规计数、杠杆提醒、浮盈计算。

## **10. 测试与验收**

| 验收锚点 | 测试文件 |
|---|---|
| 非熔断日仍破立哨、收复纠错 | test_settle_branches |
| 熔断日 DELAY/DIVERGENCE 在收盘仍破与收复两种输入下均 EXECUTE_NOW，不立哨、不走普通收复纠错 | test_circuit_day_uniform |
| EXECUTE_NOW 不立哨，等待新 enter；后反抽只纠错 | test_settle_branches / test_pool_protection_matrix |
| 保护行矩阵逐项暂停/恢复，含 INVALIDATED 恢复 | test_pool_protection_matrix |
| 终态 stop 不原地滚动；同票最多一条 ACTIVE stop | test_stop_terminal_immutable |
| T 晚生成、次日可触发、次日 settle 到期；重复/补跑/跨节假日不误伤；旧 SAME_DAY 兼容 | test_t_next_session_validity |
| stop 触发后不再推送冲突 T_BUY | test_zone_invariants |
| entry 失效阈值不高于区间下沿，文案与数值一致 | test_zone_invariants |
| 三种纠错场景独立；当日执行不计纠错；收盘处理数与纠错数分离；重复报告不重复统计 | test_correction_projection |
| retest 三终态均有充分判决事实 | test_retest_verdict_facts |
| 观察期零买点；同均线两次止损后该家族暂停；结案且价格到位后 10 日线出现 | test_reentry_checks |
| 同票多策略全部输出；EV_NEG 打标前后候选集合不变；EV_NEG 主体为 strategy_id | test_selector / test_ev_neg_subject |
| 脏报价拦截；过期 context 不参与定性；缺失事实不默认 DELAY | test_guard / test_data_quality_gate |
| entry 窗口外忽略；first_board_dip/volume_breakout 全天有效 | test_trigger_window |
| A0 读触发时快照；池内区读 evening 后快照；T 显示 side 与配对，不解析 signal_id 字符串 | test_report_as_of_snapshot |
| 重放不推送、不进正式 A0 与统计；同输入重复回放结果一致 | test_live_replay_isolation |
| 可达性只评价已结束有效期的信号 | test_reachability |
| 无非法状态迁移 | test_lifecycle |

连续两日全链路验收在隔离数据库执行：同一初始快照 → T 日 watch → settle → evening → R 日 watch → settle → evening，校验 signal_id、parent、root、zone_snapshot、settlement、correction_event、event_source，而不是比较两份报告表面点位是否一致。

## **11. 系统边界**

不做：选股/题材判断；支撑压力精密算法；日内纠错决策；利润或纪律二选一；个股与大盘压力冲突优先级；全市场板块扫描；账户追踪/杠杆提醒；定时任务/按钮回传/自动推送；双周期确认/盘中计时确认；**报告端重新推导业务分支**；**将历史重放冒充真实触发**；LLM/网格回测/真实仓位建模。

## **12. 废弃与变更**

| 项 | 处理 |
|---|---|
| depth、dedup_zone_overlap、双周期确认、journal.confirmed、min_confirm_duration | 沿用 v8.1 废弃结论 |
| `holding:<symbol>`、“entry 产生持仓上下文” | 沿用 v8.1 更名 |
| T 依赖 SAME_DAY＋执行时序表达有效期 | 改为显式 valid_for，旧数据兼容读取；**当前正常路径结果不变，不属于提前过期缺陷** |
| “判决三终态后一律暂停保护行” | 拆分为：TRIGGERED/EXPIRED 等待新 enter，INVALIDATED 下一 evening 恢复 |
| 熔断日仅“收盘仍破”才视同 EXECUTE_NOW | 改为 DELAY/DIVERGENCE 整分支视同，收复也不例外 |
| 当日执行计入纠错、“纠错区”混合全部结算 | 改为“收盘处理”区，纠错仅三场景独立计数 |
| “池内”作为 EV_NEG 主体 | 仅绑定 strategy_id；EV_NEG 不过滤候选 |
| 缺失数据默认 DELAY_OBSERVE、过期 context 参与定性 | 数据质量与业务定性分离 |
| A0 必须等于前日最终 ACTIVE 集、用当前行重绘历史 | 使用触发时事件与区间快照 |
| replay/backfill 混入正式 A0 | 真实触发与重放隔离 |
| T 行只显示“做T” | 显示结构化 side 与配对，不解析 signal_id 命名 |

## **13. 实施状态与迁移**

v8.1 已实现基线保留：市场输入与尾盘快照、selector 独立选策、同票多策略不去重、A0、EV_NEG 记录、节假日、一字首开、破位四路定性、retest 生成/到期/判决、观察哨禁买、同均线冷却、settle/watch CLI、market_snapshots 与 zone_snapshots、stats 历史区间快照。

v8.2 迁移顺序：

1. settle：熔断日统一分支＋stop_settlements 落库；
2. evening：保护行矩阵集中实现，输出结构化暂停/恢复原因；
3. 模型：signals 增加 valid_for，T 迁移并保留 SAME_DAY 兼容；
4. 报告：A0 触发时快照、T side/配对、收盘处理区、失效与可达性文案、指数映射；
5. 事件：correction_events 与纠错统计分离；event_source 隔离重放；
6. 隔离环境连续两日回放，通过 §10 全部验收后，再处理历史报告补显示。

历史报告只补显示当时已存在的事实；事后重建的触发必须标记 replay/backfill，不得覆盖原始生产记录。
