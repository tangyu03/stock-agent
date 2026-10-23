# 文档三：信号全生命周期报告模板集（v4.0）

> 本版按三层建模重写：**状态机只有一层（单信号四状态）；链是 parent_id 接力；报告是状态快照的叙事投影。** S0–S7 降级为“典型路径示例”。术语统一：前置检查、结案日、观察期、retest_confirm、limit_up_break、池内票区。双周期确认删除后，触发语义统一为单点触碰。同一只票可同时在候选区和池内票区出现，不折叠。

## 0. 三层建模总览

```
第一层：实体与四状态机        第二层：因果链               第三层：报告槽位
────────────────────         ──────────────              ──────────────
Signal: ACTIVE →             enter → stop →              report --date 读取
TRIGGERED/EXPIRED/           retest_confirm →            当时状态快照，按
INVALIDATED                  新 enter                    叙事顺序投影到槽位
（唯一状态机）                （parent_id 接力）           （不产生状态）
```

- **状态机只有第一层**，四状态，不可逆。
- **链不是状态机**，是 parent_id 连接的信号接力。
- **报告是投影**，`report --date` 只读不写。

## 1. 实体与四状态机

| 实体 | 状态 | 说明 |
|---|---|---|
| Signal（role=entry） | ACTIVE → TRIGGERED / EXPIRED / INVALIDATED | 候选买点，触达即终态 |
| Signal（role=stop） | 同上 | 池内票风控线 |
| Signal（role=profit） | 同上 | 池内票止盈参考 |
| Signal（role=t） | 同上 | 池内票做T参考 |
| Signal（role=retest_confirm） | 同上 | 观察哨，三终态即“判决” |
| PoolEntry | in / out | 关注关系 |
| Receipt | executed / ignored / veto | 标注，不驱动迁移 |
| ContextMark | ACTIVE / INACTIVE | 轻量环境标记，不走 lifecycle |

**报告模板里不出现“S5 状态”“S7 迁移”**。出现的是“观察期存续”“判决后重新开放选股”——它们是时间段/条件，不是状态。

## 2. role 定义对照表

| role | side | zone_policy | pair_required | parent | 触发方式 | 触发后行为 |
|---|---|---|---|---|---|---|
| entry | BUY | 当日有效 | 否 | 无 | 单点 | 产生持仓上下文 |
| stop | SELL | rolling（每日上移） | 否 | enter 或 holding | 单点 | 产生 retest_confirm |
| profit | SELL | fixed（目标位不跑） | 否 | enter 或 holding | 单点 | 无 |
| t | BUY/SELL | fixed（当日） | 是（当日配对） | enter 或 holding | 单点 | 配对提示 |
| retest_confirm | NEUTRAL | window(n) | 否 | enter 或 holding | 条件判定 | 链终止或纠错 |

**加新 role 的规则**：先填此表，原子组合与现有五个不同才配加。

## 3. 因果链规则表

| 触发 | 条件 | 产生 | parent_id |
|---|---|---|---|
| enter TRIGGERED | 回执入场 | 持仓上下文 | — |
| 池内票，每晚 evening | 趋势 UP/DOWN | 更新 stop、t、profit | → 最近 TRIGGERED enter |
| stop TRIGGERED | EXECUTE_NOW | retest_confirm | → 原 enter 或 holding |
| stop TRIGGERED | DELAY/DIVERGENCE 且收盘<zone_low | retest_confirm | 同上 |
| stop TRIGGERED | DELAY/DIVERGENCE 且收盘收复 | 纠错提醒（无新信号） | — |
| stop TRIGGERED | LIMIT_EXEMPT | 不产生 retest_confirm | — |
| retest_confirm TRIGGERED | 反抽不过 | 链终止，等新 enter | — |
| retest_confirm INVALIDATED | 放量站回 | 纠错提醒；人买回需新 enter | — |
| retest_confirm EXPIRED | 到期无反抽 | 链终止，等新 enter | — |
| 任意时刻 | 三个前置检查通过＋价格到位 | 新 enter | 无（新链起点） |

## 4. 报告槽位投影表

| 槽位 | 读取条件 |
|---|---|
| A0 今日执行回顾 | 当日进入 TRIGGERED 的全部 Signal＋receipts＋机械 vs 实际差异 |
| 环境行 | context_marks（ACTIVE）＋ event_calendar |
| 候选区 | ACTIVE 的 entry，经三个前置检查过滤；同票多策略全部列出不合并；超上限标 truncated |
| 池内票区 | 池内票的 ACTIVE stop/profit/t（原“持仓区”改名） |
| 破位区 | 当日进入 TRIGGERED 的 stop |
| 观察区 | ACTIVE 的 retest_confirm |
| 判决区 | 当日 signals.closed_at 的 retest_confirm 终态 |

**三个约束**：

1. **同一只票可同时在候选区和池内票区出现，不折叠。** 系统不知道你持没持，两条都给。
2. **观察区与判决区互斥**：同一条 retest_confirm 同日只在一处。
3. **判决区只显示当天刚结案的**；回看走“近期已结案”小节。

## 5. 依据等级

- **A｜原文明确**：懂哥原文或已定边界直接支持。
- **B｜语义推导**：原文没逐字写，但由操作链必然推出。
- **C｜工程近似，可配置**：系统替原文补的参数，改 config 不改模板。

## 6. 各槽位模板

### 6.1 A0 今日执行回顾

```
A0 今日执行回顾
触发：天孚通信 entry first_move_5d 208.0–209.5（14:47 单点触发）
回执：executed，size_filled=全
差异：无
触发：深科达 stop 68.0（13:42 TRIGGERED，DELAY_OBSERVE）
回执：未回执
差异：机械执行=应止损；实际操作=无回执，状态不明
```

### 6.2 环境行

```
环境：
  上证 3201.5（-0.4%）｜深证 10876.2（-0.7%）｜创业板 2154.8（-1.1%）
  光模块：主线，池内 1/3｜半导体设备：退潮
  时间：周五收摊｜财报T-1：XX
```

### 6.3 候选区

**五种策略各自独立，同票多策略全部列出，不合并。**

```
候选（2只）：

天孚通信
  entry  first_move_5d  208.0–209.5
    前提：5日线上行 + 光模块主线第6日 + 未进监管 + 榜不烂
    失效：收盘破207.8（明日MA5）
    依据：5日线买在明天的位置，破5日就是卖点
  entry  volume_breakout  207.5–208.5
    前提：放量站上207.0关键位 + 收阳量能确认 + 未进监管
    失效：收盘跌破207.0

深科达  entry  first_move_10d  65.2–65.8
  前提：回调深（自破位日-4.1%）+ 10日线方向未死 + 未进监管
  失效：收盘破65.2
  均线冷却：5日线档位冷却中（10.18、10.21两次止损），该档不出买点

候选超过 watchlist 上限时按截断规则输出，截断项标 truncated。
仓位提示：量能LOW 时在行尾附“仓位提示：减半（量能LOW）”。
```

**EV_NEG 行**：

```
XX  entry  first_move_5d  26.3–26.5  [EV_NEG：该策略近50笔期望≤0，仅记录]
```

**零预案日两个变体**：

```
# 市场熔断型（LOW档+指数DOWN）
明日零预案
环境：量能LOW，指数DOWN
池内票保护：STOP 行全部收紧为破位即 SELL

# 节假日型（长假前最后一交易日）
建议空仓过节
停止生成新 BUY
```

### 6.4 竞价快照

```
竞价：天孚通信 弱转强，量比3.2定格，高开0.8%
今日候选 208.0–209.5 正常生效

竞价：XX 强转弱，昨日涨停今低开-2.1%，量比0.6
今日全部BUY信号已作废（INVALIDATED, reason=auction_weak）

竞价：XX 一字首开（limit_up_break），量比5.8定格，开盘+1.2%
量+开盘涨幅需动态判断，系统不自动定性

竞价：天孚通信 中性，无操作含义
```

### 6.5 盘中买入提醒

**触发语义：单点触碰即触发，不再双周期确认。**

```
触发：天孚通信 entry first_move_5d 208.0–209.5
13:42 价格在区间内，单点触碰即 TRIGGERED，已推送

撤销：XX entry first_move_5d（premise_broken）
该股今日进入监管名单，生成前提已破坏，未触发部分全部作废
```

### 6.6 池内票区（原“持仓区”）

**池内票的 stop/profit/t 全生成。同一只票也可在候选区出现，不折叠。**

```
天孚通信（UP）
  量价：+5.3%，量比1.31，主动差+9.4%
  板块：光模块主线，池内排名1/3
  履历：该票自上次 entry 触达至今 +12.4%
  点位：stop 211.5–212.3（明日MA5=212.3，每日上移）｜t买 208.0–209.5，t卖 220.0–221.5
  提示：无｜T配对：t买 208.3 已回执，t卖未触发（14:30已提醒）
  同位置买点前提满足（entry first_move_5d 208.0–209.5），空仓可看候选区

天孚通信（刚触达次日）
  履历：昨日14:47 entry 触达 @208.6
  点位：stop 211.5–212.3（入场时同生，非后补）

臻宝科技（DOWN）
  量价：-3.2%，量比0.7，主动差-11.2%
  板块：退潮
  点位：stop 200.3–201.0
  提示：趋势DOWN，支撑区间已转为卖出线。无T行、无BUY行。

深科达（直接加 pool，无 enter 履历）
  点位：stop 65.2–65.8（parent=holding:深科达）
  提示：无 enter 履历，保护行按合成 parent 生成
```

profit 行按 moban.md 另行渲染；size_hint 的 T_BUY t-size 结果附在 T 行尾。

### 6.7 破位区（当日 TRIGGERED 的 stop）

```
今日破位（2只）：

天孚通信 stop 211.5（EXECUTE_NOW）
  板块行无触发，指数行无触发，个股独跌+量比1.8
  今日执行依据：主动放量算跌破。当天走，观察哨照立（给纠错留门）

深科达 stop 68.0（DELAY_OBSERVE）
  板块“半导体设备”13:42已触发（context_marks triggered_at），疑似被带崩
  收盘定生死：收盘破68.0→生成观察哨；收盘收复→当日假破位，纠错提醒

执行对账：触发2，执行1，未执行1（深科达止损无回执）
```

**当日收复纠错提醒（统一文案）**：

```
收复：深科达收回 68.0 上方
上午破位（DELAY_OBSERVE），14:10收复止损位
收复止损位，可按纪律纠错。追不追，是你。
[push_correction_notice]
```

### 6.8 观察区（ACTIVE 的 retest_confirm）

```
观察哨（1个）：
深科达：破位日10.21（DELAY_OBSERVE），反抽观察68.0–68.5，未回抽，剩2日。（观察期内不出买点）

观察哨（1个，摸到了没收复）：
深科达：破位日10.21，反抽观察68.0–68.5。今日最高68.3摸到区间，收67.9未站上。
明日收盘仍在68.5下方→确认破位。剩1日。（观察期内不出买点）
```

### 6.9 判决区（当日 signals.closed_at 的 retest_confirm 终态）

**三种终态各有结案时刻，不是固定 T+3。**

```
判决：深科达 确认破位（TRIGGERED）
三日反抽未收复68.5（最高68.3），原支撑确认转为压力
观察哨关闭。后续：价格到10日线附近再看买点。

判决：深科达 假破位（INVALIDATED）
收盘68.9收复观察区间上沿68.5，量比1.4（达标）
原止损逻辑失效，明日起恢复正常池内票保护行（止损线按最新均线重算）
提醒：收复止损位，可按纪律纠错。追不追，是你。[push_correction_notice]

判决：深科达 无反抽（EXPIRED）
3日窗口到期，价格未回抽68.0–68.5
观察哨关闭。后续：继续阴跌→10日线前提满足时候选区出点位；企稳回升→无买点，等趋势重建
利润或纪律，是你。
```

**判决区只显示当天刚结案的**；前一日结案的不再出现，回看走“近期已结案”小节。

### 6.10 判决后候选区

```
候选（1只）：
深科达  entry  first_move_10d  65.2–65.8
前提：回调深（自破位日-4.1%）+ 10日线方向未死 + 未进监管
失效：收盘破65.2
均线冷却：5日线档位冷却中（10.18、10.21两次止损），该档不出买点
```

**闭环验收点**：同一只票，破位区里从 stop 终态消失，候选区里在下一档均线重新出现，冷却说明证明系统记得它为什么离开过。

## 7. 典型路径示例（因果链的实例，不是状态机）

**路径一：破5→反抽摸不过→确认破位→10日线再买**

```
10.21 stop TRIGGERED（DELAY_OBSERVE），收盘仍破 → settle 生成 retest_confirm
10.22 观察期 ACTIVE，未回抽，剩2日
10.24 摸到68.3未站上 → watch 判 TRIGGERED，当日判决区“确认破位”
10.28 价格跌到10日线附近，检查一放行、检查二5日线暂停、检查三前提满足
      → generator 生成新 entry（first_move_10d）
```

**路径二：破5→放量站回→人纠错买回**

```
10.21 stop TRIGGERED，生成 retest_confirm
10.23 收盘站上68.5、量比达标 → watch 判 INVALIDATED＋纠错提醒
      原 STOP 状态不迁移、不发买点；人买回需新 entry
      价格在5日线上方远离均线，检查三前提不满足 → 不出信号，等
```

**路径三：破5→观察期里价格横跳**

```
10.21 stop TRIGGERED，生成 retest_confirm
10.22–10.24 观察期 ACTIVE，价格横跳
      检查一存续中挡住 → 该票零买点输出（反抽不是买点）
```

## 8. 迁移审计＋依据等级

### 审计表

| 因果规则 | 触发条件 | 实现落点 | 动作/边界 | 依据等级 | 验收锚点 | 未覆盖风险 |
|---|---|---|---|---|---|---|
| entry 生成 | 策略前提满足＋三个前置检查通过 | generator + selector | 同票多策略全部输出；Z/W同生 | A | 五策略正向；同票全部推送 | watchlist 截断 |
| 竞价作废 | strong_to_weak | auction_call | 当日 BUY 全 INVALIDATED | A | 强转弱一条；一字首开不定性 | 一字首开靠人 |
| 买入触发 | 单点触碰 | watch | 触达即 TRIGGERED | A | 同信号多次 watch 只推送一次 | 数据源假报价由 guard 拦 |
| premise_broken | 监管/烂榜/题材变化 | evening 重检 | 撤销提醒 | A | 撤销一条 | — |
| 入场同生 | ENTRY 生成 | generator | 止损与买点同生 | A | 同生一条 | 无回执状态不明 |
| UP/DOWN 池内票 | 趋势 | evening | UP 挂 T；DOWN 无 BUY | A | UP/DOWN 各一条 | — |
| stop 定性 | 触及止损 | volume + watch | 四路输出 | A | 四路各一条 | 封板数据失真 |
| EXECUTE_NOW→retest | 当天执行 | settle | 也生成 retest_confirm | B | 当天执行仍生成一条 | — |
| DELAY/DIVERGENCE→retest | 收盘<zone_low | settle | 与收复分支互斥 | A | 仍破/收复各一条 | 盘中“摸到未过”晚报才报 |
| DELAY/DIVERGENCE→纠错 | 收盘收复 | settle | 统一纠错函数 | A | 两条路径文案一致一条 | — |
| LIMIT_EXEMPT→次日 | 封板 | settle | 不生成 retest_confirm | B | 封板不生成一条 | — |
| retest 三终态 | 摸到未过/站回/到期 | watch + settle | 三终态互斥；按 closed_at 查 | A/C | 三终态各一条 | 量比1.2是C级 |
| 观察期存续禁买 | ACTIVE retest | generator | 该票零买点 | A | 存续零输出一条 | — |
| 同均线冷却 | 同均线两次 STOP | generator | 该家族暂停，不冷整票 | B | 5日线暂停不影响10日线 | 窗口2日是C级 |
| 判决后再买 | 结案＋价格到位 | generator | 10日线前提自然满足 | A/B | 结案后10日线出现一条 | — |

**表外三行单列**：

- **生成层不合并**（A）：同票多策略全部输出全部推送，无合并用例，删除 `dedup_zone_overlap`。
- **depth 废弃**（A）：选股由三个前置检查＋价格位置决定，删除 depth 相关 config、签名、调用点，验收含“无残留调用”。
- **双周期确认删除**（A）：BUY 与 STOP 统一单点触碰；防脏数据由 guard 承担；统计可达性改日 K 线校验；`journal.confirmed`、`first_in_zone_at`、`min_confirm_duration` 删除；验收含“同信号多次 watch 只推送一次”和“unreachable 标记”。

## 9. 架构边界

- **盘中“摸到压力位过不去”的时效**受纯 CLI 手动 watch 频率限制。系统可能在晚报才告诉你今天摸到了。
- **`retest_volume_min=1.2`、`retest_window_days=3`、`same_ma_cooldown_days=2`** 均为工程近似，C 级，改 config 不改模板。
- **depth 参数已废弃**。
- **双周期确认已删除**：采样粒度由人控制，防毛刺职能由 guard 承担，保留双周期会产生系统性漏报。
- **观察区与判决区互斥**。
- **判决区只显示当天刚结案的**。
- **pool 是关注范围，不是持仓清单**：同一只票可同时在候选区和池内票区出现，不折叠。

---