
# **文档三：信号全生命周期报告模板集（v4.2）**

## **0. 三层建模总览**

```text
第一层：实体与四状态机        第二层：因果链与结算事实       第三层：报告槽位
────────────────────         ─────────────────────         ────────────────
Signal: ACTIVE →             enter → stop →                report --date 读取
TRIGGERED/EXPIRED/           收盘结算 → retest_confirm →    指定时点不可变快照，
INVALIDATED                  恢复保护或新 enter             不产生状态、不重算分支
（唯一状态机）                （parent_id/root_id接力）
```

- 状态机只有第一层，四状态，不可逆；
- `break_qualifier` 是盘中原始定性，`effective_qualifier` 是熔断覆盖后的有效定性，`settlement_outcome` 是收盘结果，三者分开；
- **收盘处理不是纠错**，纠错只是其中特定场景；
- 报告是投影，只读不写。

**日期口径：**T=数据基准日，R=计划有效日。`report --date R` 读取 T 日 settle/evening 完成后的快照。

| 内容 | 使用快照 |
|---|---|
| A0 | T 日真实触发事件的触发时快照 |
| 破位区 | 触发时 stop 快照＋T 日结算记录 |
| 收盘处理 | T 日 stop 结算与观察哨结案事实 |
| 候选区 | T 晚生成、valid_for=R 的 entry |
| 池内票区 | T 晚 evening 后供 R 日使用的信号 |
| 判决区 | closed_at 属于 T 日的 retest_confirm |

A0 与池内票区可以引用不同 signal_id，不构成矛盾。未结算时只能输出 preview 并标记“未结算”。

## **1. 实体与四状态机**

| 实体 | 状态 | 说明 |
|---|---|---|
| Signal（entry） | ACTIVE → TRIGGERED / EXPIRED / INVALIDATED | D 晚生成，R 日有效；ma_tomorrow 系仅窗口内触发 |
| Signal（stop） | 同上 | 原始定性、有效定性与收盘结算分开 |
| Signal（profit） | 同上 | 池内止盈参考 |
| Signal（t） | 同上 | D 晚新建，R 日有效并配对 |
| Signal（retest_confirm） | 同上 | 观察哨，三终态即判决 |
| PoolEntry | in / out | 关注关系 |
| ContextMark | ACTIVE / INACTIVE | 环境标记，含数据质量状态 |

报告中不出现“S5 状态”“S7 迁移”。出现的是“观察期存续”“收盘处理”“等待新 enter”“假破位后恢复保护”。

## **2. role 定义对照表**

| role | side | zone_policy | pair | parent | 有效与触发窗口 | 触发后行为 |
|---|---|---|---|---|---|---|
| entry | BUY | 次日单点区间 | 否 | 无 | valid_for=R；ma_tomorrow 系 14:30–15:00，其他全天 | 成为保护行 parent 锚点＋统计分母 |
| stop | SELL | 按趋势滚动 | 否 | enter 或 pool | 全天 | 写原始定性并进入收盘结算 |
| profit | SELL | fixed 或 trailing | 否 | enter 或 pool | 全天 | 无 |
| t | BUY/SELL | fixed，次日有效 | 是 | enter 或 pool | valid_for=R，全天 | 配对提示 |
| retest_confirm | NEUTRAL | window(n) | 否 | enter 或 pool | 条件判定，全天 | 链终止、纠错或恢复保护行 |

## **3. 因果链规则表**

| 触发 | 条件 | 产生 | 后续保护行 |
|---|---|---|---|
| entry 生成 | 前提与前置检查通过 | R 日 entry，必要时同生 stop | 同票最多一条 ACTIVE stop |
| entry TRIGGERED | 有效日、窗口内触达 | parent 重锚或新建 stop | 恢复普通池内生成 |
| 每晚 evening | 池内 UP、无暂停条件 | R 日 stop/profit/t | DOWN 不生成 BUY 行 |
| stop TRIGGERED | 单点触碰 | 原始 break_qualifier | 同票未触发 T_BUY 连坐作废 |
| 熔断日结算 | DELAY/DIVERGENCE | 视同 EXECUTE_NOW（不论收盘是否收复） | 不立哨；等待新 enter |
| 非熔断仍破 | DELAY/DIVERGENCE，收盘低于下沿 | retest_confirm | 观察期暂停普通保护行 |
| 非熔断收复 | DELAY/DIVERGENCE，收盘收复下沿 | 当日收复＋纠错事件 | 次日晚恢复生成 |
| EXECUTE_NOW | 原始或有效定性 | 当日执行 | 不立哨；等待新 enter |
| LIMIT_EXEMPT | 封板附近失真 | 封板豁免 | 不立哨；次日晚正常生成 |
| retest TRIGGERED | 反抽不过 | 确认破位 | 暂停，等待新 enter |
| retest EXPIRED | 到期从未入区 | 无反抽 | 暂停，等待新 enter |
| retest INVALIDATED | 放量站回 | 假破位＋纠错 | 次日晚恢复保护行 |
| EXECUTE_NOW 后反抽 | 回到原止损线附近 | after_execute_now 纠错 | 不生成观察哨或买点 |

## **4. 报告槽位投影表**

| 槽位 | 读取条件与展示要求 |
|---|---|
| 环境行 | T 日指数、有效 context_marks、事件日历；过期数据单独标记 |
| 候选区 | valid_for=R 的 ACTIVE entry；同票多策略全部列出 |
| 池内票区 | T 晚供 R 日使用的 stop/profit/t；缺失显示生命周期原因 |
| A0 触发回顾 | T 日真实 TRIGGERED 事件；主报告可汇总，审计版显示完整事件 |
| 破位区 | T 日触发 stop；展示原始定性、弱市覆盖和收盘结果 |
| 收盘处理 | T 日 stop 结算与观察哨结案；主报告默认汇总，审计版可列明细 |
| 观察区 | T 晚仍 ACTIVE 的 retest_confirm |
| 判决区 | closed_at 属于 T 日的 retest_confirm 终态与判决事实 |

约束：

1. 候选区和池内票区不折叠；
2. 同一 retest_confirm 不得同时进入观察区与判决区；
3. 判决区只显示当天结案；
4. 收盘处理不等于纠错；不设顶级“纠错区”；
5. 当日执行、生成观察哨、封板豁免不计纠错；
6. 破位区与收盘处理区不得在主报告中重复整表明细。

## **5. 依据等级**

- **A｜本版方案明确**
- **B｜语义推导**
- **C｜工程近似，可配置**

## **6. 各槽位模板**

### **6.1 报告头与环境行**

```text
【2026-10-28 交易计划】
数据基准日：2026-10-27｜计划有效日：2026-10-28
报告状态：已结算

环境：
  量能 6724.14亿｜量能档位：LOW
  上证 3648.30/MA5 3681.47（低于MA5 0.90%）
  深证成指 11245.70/MA5 11503.10（低于MA5 2.24%）
  科创50 1252.28/MA5 1312.49（低于MA5 4.59%）
  光模块：退潮｜半导体设备：轮动
  板块上下文：存储数据过期，不参与破位定性
  熔断日：是/否（读取市场日快照）
```

指数名称由固定映射生成，不得将 `sz399001` 显示为创业板。

### **6.2 A0 信号日触发回顾**

主报告汇总：

```text
A0 信号日（2026-10-27）触发回顾：
  共触发28条｜stop 19｜t 8｜retest_confirm 1
  完整触发时快照见审计附录
```

审计版逐条：

```text
触发：大普微 stop 310.959–314.1｜14:02
  signal_id=sig_...｜strategy_id=...｜parent=...
  zone_snapshot=zs_...｜source=live_watch

触发：创世纪 t_buy 10.22–11.48｜10:18
  signal_id=sig_...｜配对：t_sell sig_...／未配对
```

要求：展示触发时刻；T 必须区分 t_buy/t_sell（读结构化 side，不解析 signal_id 字符串），配对信息由现有配对逻辑推导；entry 能审计触发窗口；A0 不与 T 晚最终 ACTIVE 集硬对齐；replay/backfill 不进默认生产 A0。

### **6.3 候选区**

五种策略独立，同票多策略全部列出，不合并。

```text
候选（2只，不合并同票多策略）：

天孚通信
  entry  first_move_5d  208.00–209.50
    有效日：2026-10-28｜触发窗口：14:30–15:00
    前提：5日线上行＋光模块主线＋未进监管＋榜不烂
    基准日参考：收盘211.20，高于买入区间，待有效日评估
    失效：收盘低于207.80（失效阈值）
    依据：5日线买在明天的位置，破5日就是卖点

  entry  volume_breakout  207.50–208.50
    有效日：2026-10-28｜触发窗口：全天有效
    前提：放量站上207.00关键位＋收阳量能确认＋未进监管
    失效：收盘低于207.00

深科达
  entry  first_move_10d  65.20–65.80
    有效日：2026-10-28｜触发窗口：14:30–15:00
    前提：回调充分＋10日线方向未死＋未进监管
    失效：收盘低于65.20（区间下沿）
    均线冷却：5日线家族暂停，10日线不受影响
```

约束：失效阈值不得高于买入区间下沿；“明日 MA／区间下沿／MA×系数”按真实来源命名；有效日未结束时只显示“基准日参考”，不输出未触达/不可达结论；超上限标 truncated；候选与池内 stop 重合时提示“空仓看买点，池内保护看 stop”；LOW 时附“仓位提示：减半（量能LOW）”。

EV_NEG 只能绑定真实策略：

```text
[EV_NEG：strategy=first_move_5d，近50笔期望≤0，仅记录]
```

禁止“【EV_NEG记录】：池内”。EV_NEG 不过滤候选。

### **6.4 竞价快照**

```text
竞价：天孚通信 弱转强，量比3.2定格，高开0.8%
今日候选 208.00–209.50 正常生效

竞价：×× 强转弱，昨日涨停今低开-2.1%，量比0.6
今日全部BUY信号已作废（INVALIDATED, reason=auction_weak）

竞价：×× 一字首开（limit_up_break），量比5.8定格，开盘+1.2%
量与开盘涨幅需动态判断，系统不自动定性

竞价：天孚通信 中性，无操作含义
```

### **6.5 盘中买入提醒**

```text
触发：天孚通信 entry first_move_5d 208.00–209.50
14:47 价格在区间内，且处于有效日触发窗口，单点触碰即 TRIGGERED

忽略：天孚通信 entry first_move_5d 208.00–209.50
10:23 价格在区间内，但窗口外，不触发、不推送

撤销：×× entry first_move_5d（premise_broken）
该股今日进入监管名单，生成前提已破坏，未触发部分全部作废
```

### **6.6 池内票区**

同一只票可同时出现在候选区与池内票区，不折叠。

正常 UP：

```text
天孚通信（UP）
  量价：+5.3%，量比1.31，主动差+9.4%
  板块：光模块主线，池内排名1/3
  履历：自上次 entry 触达至今 +12.4%
  点位：stop 211.50–212.30｜profit 220.00–221.50
  做T（次日有效）：t_buy 208.00–209.50｜t_sell 220.00–221.50
  T配对：t_buy已生成，t_sell未触发
```

正常 DOWN：

```text
臻宝科技（DOWN）
  点位：stop 200.30–201.00｜profit 218.00–220.00
  提示：趋势DOWN，不生成T_BUY及其他BUY行
```

观察哨存续：

```text
深科达（观察哨存续）
  提示：观察哨68.00–68.50，剩2日
  普通stop/profit/t暂停，由观察哨承担观察语义
```

等待新 enter：

```text
深科达（DOWN）
  点位：无普通保护行
  保护状态：确认破位／EXECUTE_NOW后链终止，等待新enter
```

假破位后恢复：

```text
深科达（DOWN）
  点位：stop 64.80–65.30｜profit ...
  保护状态：假破位结案后按最新行情重建
  前序signal_id=sig_...｜当前signal_id=sig_...
```

T 未生成：

```text
大普微（UP）
  点位：stop ...｜profit ...
  T状态：未生成｜原因：<结构化reason>
```

没有 stop 时，不得统一写“支撑区间已转为卖出线”，必须显示真实生命周期原因。

### **6.7 破位区**

破位区展示逐条依据，收盘处理区负责汇总。

普通 EXECUTE_NOW：

```text
博杰股份 stop 84.2391–85.0900
  原始定性：EXECUTE_NOW
  收盘结算：收盘78.81仍低于84.2391，当日执行，不立观察哨
  保护状态：等待新enter
```

弱市覆盖：

```text
博杰股份 stop 84.2391–85.0900
  原始定性：DELAY_OBSERVE
  弱市处理：熔断日视同 EXECUTE_NOW
  收盘结算：当日执行，不立观察哨，不产生当日收复纠错
  保护状态：等待新enter
```

非熔断日仍破：

```text
深科达 stop 68.00–68.50
  原始定性：DELAY_OBSERVE
  收盘结算：收盘67.80低于68.00，生成观察哨
```

非熔断日收复：

```text
创世纪 stop 11.1058–11.2180
  原始定性：DELAY_OBSERVE
  收盘结算：收盘11.20收复止损下沿11.1058，当日假破位，不立观察哨
  纠错提醒：收复止损下沿，可按纪律纠错。
  [push_correction_notice, scene=same_day_recover]
  保护状态：次日晚恢复正常生成
```

LIMIT_EXEMPT：

```text
×× stop ...
  原始定性：LIMIT_EXEMPT
  收盘结算：封板附近失真，不立观察哨，次日晚正常生成保护行
```

数据不足：

```text
×× stop ...
  原始定性：待补数据
  数据状态：MISSING/STALE/ANOMALOUS
  收盘结算：暂未完成，不默认按DELAY_OBSERVE处理
```

EXECUTE_NOW 后反抽附项（不计入当日 stop 触发数）：

```text
纠错附项：天孚通信
  原stop于10.21判EXECUTE_NOW，原signal_id=sig_...
  今日价格反抽接近原止损线211.50，原破位定性不变
  仅作纠错参考，不生成观察哨或买点
  [push_correction_notice, scene=after_execute_now]
```

### **6.8 收盘处理区**

收盘处理是独立结算视图，包含当日执行、当日收复、生成观察哨、封板豁免及观察哨结案，**不等于纠错**。

主报告默认只显示汇总：

```text
收盘处理（2026-10-27）：
  stop结算：共19条
    当日执行 8｜当日收复 11｜生成观察哨 0｜封板豁免 0｜待补数据 0
  观察哨结案：共1条
    确认破位 1｜假破位 0｜无反抽 0
  纠错事件：共11条
    当日收复 11｜EXECUTE_NOW后反抽 0｜假破位站回 0
```

审计版列明细：

```text
signal_id=...｜raw=DELAY_OBSERVE｜effective=EXECUTE_NOW
circuit_day=true｜outcome=EXECUTE_NOW
close=78.81｜zone_snapshot=...｜source=live_settle

signal_id=...｜raw=DELAY_OBSERVE｜effective=DELAY_OBSERVE
circuit_day=false｜outcome=RECOVERED_CORRECTION
close=11.20｜correction_scene=same_day_recover
```

统计约束：收盘处理数＝全部已完成结算记录数；纠错事件数＝仅三种 correction_scene 的事件数；当日执行不得进入纠错事件数；重复生成报告不得增加纠错事件数；只有 notice_status=pushed 才能写“已推送”。

### **6.9 观察区**

```text
观察哨（1个）：
深科达：破位日10.21（DELAY_OBSERVE）
  观察区间68.00–68.50，尚未进入区间，剩2个交易日
  观察期内不出买点，普通保护行暂停
```

摸到但未收复：

```text
观察哨（1个）：
深科达：破位日10.21
  观察区间68.00–68.50
  今日最高68.30进入区间，收盘67.90未站上68.50
  剩1个交易日
```

### **6.10 判决区**

TRIGGERED 必须证明“曾入区但未收复”：

```text
判决：深科达 确认破位（TRIGGERED）
  观察期第2个交易日最高68.30，进入观察区间68.00–68.50
  收盘67.90，未站上区间上沿68.50
  观察哨关闭；普通保护行暂停，等待新enter
```

EXPIRED 必须证明“窗口到期且从未入区”：

```text
判决：深科达 无反抽（EXPIRED）
  观察窗口共3个交易日，期间最高价从未进入68.00–68.50
  窗口到期，观察哨关闭；普通保护行暂停，等待新enter
```

INVALIDATED 必须证明放量站回：

```text
判决：深科达 假破位（INVALIDATED）
  收盘68.90站上观察区间上沿68.50，量比1.40，满足收复条件
  观察哨关闭；下一evening恢复正常池内保护行
  纠错提醒：可按纪律纠错，不自动产生买点
  [push_correction_notice, scene=retest_invalidated]
```

“未形成有效反抽”不得单独作为终态依据。

### **6.11 判决后候选区**

```text
候选（1只）：
深科达
  entry  first_move_10d  65.20–65.80
    有效日：2026-10-28｜触发窗口：14:30–15:00
    前提：回调充分＋10日线方向未死＋未进监管
    失效：收盘低于65.20
    均线冷却：5日线家族暂停，10日线不受影响
```

闭环验收点：同一只票从 stop 终态进入观察、判决或等待新 enter；满足下一档均线前提时候选区出现新 entry；冷却说明解释为何不是原来的 5 日线策略；新旧信号通过 signal_id、root_id 和时间快照衔接。

## **7. 典型路径示例**

**路径一：破5→反抽不过→确认破位→10日线再买**

```text
10.21 stop TRIGGERED，原始DELAY_OBSERVE，非熔断日，收盘仍破
      → settle生成retest_confirm，普通保护行暂停
10.24 最高入区、收盘未站上 → retest TRIGGERED
      → 判决区展示入区与未收复事实，继续等待新enter
10.28 价格到10日线附近，三个前置检查通过（5日线家族冷却）
      → 生成first_move_10d，valid_for=次一交易日
有效日14:47窗口内触碰 → entry TRIGGERED → 保护行恢复
```

**路径二：破5→放量站回→假破位→恢复保护**

```text
10.21 stop TRIGGERED，生成retest_confirm
10.23 收盘站上上沿且量比达标 → retest INVALIDATED＋纠错事件
原stop状态不迁移，不自动发买点
下一evening恢复正常池内保护行；人工买回仍需新entry
```

**路径三：非熔断日当日收复**

```text
stop盘中TRIGGERED，原始DELAY_OBSERVE，收盘收复zone_low
→ RECOVERED_CORRECTION＋same_day_recover纠错，不生成观察哨
→ 下一evening恢复正常保护行
```

**路径四：熔断日统一执行**

```text
circuit_day=true，原始DELAY_OBSERVE/DIVERGENCE
→ effective=EXECUTE_NOW
无论收盘是否收复：当日执行、不立哨、不走普通收复纠错
→ 普通保护行暂停，等待新enter；原始qualifier保留供审计
```

**路径五：观察期价格横跳**

```text
观察哨ACTIVE期间：该票零买点、普通stop/profit/t暂停
价格横跳不自动产生纠错或新entry，等待三终态之一结案
```

## **8. 迁移审计与依据等级**

| 规则 | 实现落点 | 依据 | 验收锚点 |
|---|---|---|---|
| entry/T 晚生成、次日有效（valid_for 显式化） | generator/watch/settle | A | test_t_next_session_validity |
| entry 窗口约束 | watch | A | test_trigger_window |
| 原始 qualifier 落库不被覆盖 | watch/journal | A | qualifier与settlement分离 |
| 熔断日整分支视同 EXECUTE_NOW | market_snapshot/settle | A | test_circuit_day_uniform |
| 收盘四路结果 | settle | A | test_settle_branches |
| 保护行矩阵（EXECUTE_NOW 等待新 enter；INVALIDATED 恢复） | generator/evening | A（本版定稿） | test_pool_protection_matrix |
| 终态 stop 不原地更新；同票一条 ACTIVE stop | lifecycle/generator | A | test_stop_terminal_immutable |
| stop 触发后 T_BUY 连坐 | watch/lifecycle | A/B | test_zone_invariants |
| 三种纠错场景；收盘处理≠纠错 | correction_event/render/stats | A | test_correction_projection |
| A0 使用触发时快照 | report/journal | A | test_report_as_of_snapshot |
| 历史重放隔离 | replay/journal/stats | A | test_live_replay_isolation |
| EV_NEG 只绑定 strategy、不过滤 | stats/render/generator | A | test_ev_neg_subject |
| 过期 context 不参与定性；缺失不默认 DELAY | guard/volume | A | test_data_quality_gate |
| retest 三终态保存判决事实 | watch/settle | A | test_retest_verdict_facts |
| retest_volume_min=1.2 / window_days=3 / cooldown_days=2 | config | C | 可配置 |
| 主报告汇总、审计版明细 | render | B | 两视图事实一致 |

## **9. 架构边界**

- 盘中时效受纯 CLI 人工 watch 频率限制；
- retest_volume_min、retest_window_days、same_ma_cooldown_days 为 C 级工程近似；
- entry 可达性主口径为尾盘近似，日 K 全天可达率并列输出；有效日未结束只显示基准日参考；
- depth 与双周期确认继续废弃；
- 报告不重算业务分支；A0 不与最终 ACTIVE 集硬对齐；两日点位相同不能单独证明快照复用；
- 收盘处理可以独立成区，但不是纠错同义词；纠错不是 Signal，不形成新状态机；
- 过期 context 不参与破位定性；真实触发与 replay/backfill 分开报告、分开统计；
- pool 是关注范围，不是持仓清单；候选区与池内票区不折叠；
- 观察区与判决区对同一观察哨互斥；判决区只显示当天结案；
- EXECUTE_NOW 后反抽只纠错，不生成观察哨或买点；
- 主报告与审计报告只能从同一事实源生成。

## **10. 默认主报告与审计视图**

主报告顺序：

```text
报告头与环境 → 今日候选 → 池内变化与异常 → A0触发汇总
→ 破位区逐条依据 → 收盘处理汇总 → 观察区 → 判决区
```

审计视图增加：完整 A0 事件、完整池内点位、signal_id/parent_id/root_id、zone_snapshot_id、raw/effective qualifier 与 circuit_day、settlement_outcome/data_status、correction_scene/notice_status、event_source/occurred_at/recorded_at、code_version/input_snapshot_id、完整收盘处理明细、近期已结案。
