# 文档一：交易信号系统技术方案（v7.0）

> 本版相对 v6.1 的实质变化：**把“状态机与破位时序”拆成“三层建模”和“破位链因果规则”两节，纠正 v6.1 中状态机层次被 S0–S7 叙事压扁的问题。**其余章节保留 v6.1 口径，只补此前讨论中定死的硬约束（settle 四路分支、纠错统一、三个前置检查、config 三项、废弃项、验收锚点）。

## 0. 定位与铁律

一个**信号服务**：每晚收盘后计算点位、入库；盘中价格触达点位提醒；信号过期或前提作废撤销提醒。不下单、不连交易接口、不接 LLM、不建模真实仓位、不记账、不知道你的账户。纯后端 CLI 驱动，无定时任务自动推送，无按钮回传。

三条铁律：

1. **系统世界里没有持仓，只有建议。** pool 就是持仓清单本身——你在 pool 里，系统就当你拿着，每晚为你生成保护行；你把自己从 pool 里删掉，系统就当你不在了。不做来源区分，不做回执推算持仓。
2. **信号一生只有四个状态：ACTIVE → TRIGGERED / EXPIRED / INVALIDATED。** 人的动作（veto、回执）是对信号的标注，不是状态机的一环。
3. **策略就是 strategies.yaml 里的几段配置。** 每条策略必须写全 premise / y / z / w 四键，缺一加载即报错。加打法 = 加一段 YAML。

## 1. 行为建模

系统的全部行为按触发源分三类。时钟列为**建议时刻**，实际全部由人工 CLI 触发。

**时钟触发（routines）：**

| 时刻 | 行为 | 输出 |
|---|---|---|
| 15:30 | evening 收盘生成 | 信号入库＋汇总报告（含零预案判定） |
| 9:15–9:25 | auction_call 竞价判定 | 形态分类 → 作废当日 BUY 或快照供人确认 |
| 9:30–15:00 | watch 盘中盯梢 | 触发检查 → 确认 → 定性 → 提醒 |
| 14:30 | 尾盘快照 | 把此刻价格写入 tail_snapshots，供晚间 ma_tomorrow 使用 |
| 14:30 起 | restore_check T 复原倒计时 | 未配对 T 提醒 |
| 15:05 | settle 收盘清点 | EXPIRED / INVALIDATED ＋ retest_confirm 生成（按 §7.2 四路分支）＋ Z 滚动重算 |

**行情触发（watch 内部分支）：** 价格入区间 → 触发确认（BUY 双周期 / STOP 单点）；破位 → 两级定性（板块 → 指数）＋封板豁免＋分歧规则；STOP 当日收复 → 纠错提醒；前提破坏 → 作废＋撤销提醒；retest_confirm 盘中触达 → 按 §7.3 判定终态。

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
│   ├── models/                 # 领域模型层
│   │   ├── signal.py           # Signal + 四状态枚举 + zone校验(low<high)
│   │   ├── journal.py          # JournalEntry（含 confirmed / touched_only）
│   │   ├── receipt.py          # Receipt（executed/ignored/veto + size_filled）
│   │   └── pool.py             # PoolEntry（date/symbol/theme_label，即持仓清单）
│   ├── db/session.py           # 连接与持久化，不含模型定义
│   ├── data/
│   │   ├── akshare.py          # 行情/竞价/龙虎榜/指数/板块指数/两融/主力资金流，重试+缓存
│   │   ├── guard.py            # DataFreshnessGuard + 脏数据闸门 + 量比×换手口径交叉校验
│   │   └── mock.py             # 测试假数据源
│   ├── core/                   # 纯判断与纯生成：不 import data/db/notify（CI 强制）
│   │   ├── ma.py               # ma_tomorrow（预估收盘价语义）
│   │   ├── trend.py            # UP/DOWN（支撑压力角色互换）
│   │   ├── volume.py           # 量比档/换手分位/主动差/破位三问/封板豁免/分歧规则
│   │   ├── auction.py          # 竞价形态分类（含一字首开 limit_up_break）
│   │   ├── lhb.py              # 烂榜特征（砸盘席位+卖方集中度）
│   │   ├── sector.py           # 板块三态 + 池内排序 + theme_map 读取
│   │   ├── selector.py         # 策略前提检查（每策略一个纯函数，独立并行）
│   │   ├── generator.py        # 前提满足集 × pool → 信号行（ENTER/Z/W 同生、三个前置检查）
│   │   └── lifecycle.py        # 单信号四状态迁移唯一入口
│   ├── jobs/
│   │   ├── evening.py / auction_call.py / watch.py / restore_check.py / settle.py
│   ├── notify/
│   │   ├── push.py             # 人工触发单向通知（push-file），不做按钮回传
│   │   └── render.py           # 槽位报告（见 §8）
│   ├── stats.py                # 两层报表
│   └── cli.py                  # receipt / veto / t-size / stats / holdings / pool / report / push-file
├── legacy/                     # 冻结区
└── tests/
    ├── fixtures/               # 川环26.5 / 博创42.3 / 兆龙60.5
    ├── test_ma.py / test_trend.py / test_volume.py / test_lifecycle.py
    ├── test_ordering.py / test_replay.py / test_deps.py / test_selector.py
    ├── test_settle_branches.py # 四路定性 settle 分支（新增）
    └── test_reentry_checks.py  # 三个前置检查（新增）
```

## 3. 数据模型（五表）

**signals（核心表）**：id / symbol / strategy_id / side（BUY/SELL）/ tag（enter / stop / profit / t / retest_confirm / index_context / sector_context / time_trigger，仅 stats 分组用）/ zone_low、zone_high（强制区间）/ size_hint / premise（生成时前提快照，含 premise_facts 结构化字段）/ trend（UP/DOWN）/ activation / valid_policy（intraday / until_stopped）/ status / status_reason（hit / eod / premise_broken / human_veto / retest_pass / retest_fail / auction_weak / lhb_veto）/ parent_id（stop/profit/t/retest_confirm 必填，指向 enter；无 enter 履历的持仓票用合成值 `holding:<symbol>`，此类信号链不进统计）/ created_at / closed_at。

**journal**：每次迁移一行，含 confirmed（confirmed / touched_only）。归因全部从日志重建。**报告“当日结案”判定依赖 journal 的 closed_at 日期。**

**receipts**：verdict（executed / ignored / veto）、size_filled（可空，默认全额）。不驱动状态迁移。

**pool**：date / symbol / theme_label。**就是持仓清单，无角色区分、无来源标记**——你在里面就是持仓，不在就不是。指数与板块上下文不经过 pool，由 config 的 market_context 固定生成。

**tail_snapshots**：date / symbol / price，14:30 由 watch 顺手写入。ma_tomorrow 的 est_close 优先取它；无快照才 fallback 当日收盘价，且生成信号的日志必须标注口径（`est_close=tail_snapshot` 或 `fallback=close`）——**严禁无标注地用事后真实收盘价**。

## 4. 判断模块（core/）

**ma.py**（最高测试优先级）：`ma_tomorrow(closes, n, est_close)`。est_close 是尾盘预估收盘价，非事后真实收盘价。锚点：川环26.5 / 博创42.3 / 兆龙60.5，对懂哥当时输入误差 <0.5%。

**trend.py**：收盘>20日线且5日线斜率向上 → UP，其余 DOWN。同一价位区间，UP 挂 BUY（支撑=买点），DOWN 挂 SELL（跌破支撑=卖点不是买点）。

**volume.py**：竞价量比用 9:25 定格值，不外推；`break_qualifier` 四路输出：

```python
def break_qualifier(change_pct, drive, vgear, sector_triggered, index_triggered) -> str:
    if limit_exempt(change_pct, code, name): return "LIMIT_EXEMPT"   # 封板内外盘失真
    # 主动/被动两级查询：先板块行近30分钟有无TRIGGERED，再指数行
    if 放量 and 主动:        return "EXECUTE_NOW"     # 立即推“破位，撤”
    if 下跌 but 外盘占优:     return "DIVERGENCE"      # 有人在接，看不懂别装懂
    return "DELAY_OBSERVE"                            # 缩量被动，收盘定生死
```

**auction.py**：四类分类——weak_to_strong（BUY 正常生效）/ strong_to_weak（当日 BUY 全部 INVALIDATED＋提醒）/ **limit_up_break（昨日涨停今日竞价打开：带竞价量比＋开盘涨幅快照供人确认，“量+开盘涨幅需要动态分析”，不写死阈值）** / neutral。

**lhb.py**：命中砸盘席位或卖方集中度过阈 → 次日 BUY 不生成；持仓票 push 预警。

**sector.py**：主线/轮动/退潮三态判定；池内去弱择强排序；theme_map 读取。

**selector.py**：策略前提检查，每策略一个纯函数，**独立并行、无优先级排序**：

```python
def check_first_board_dip(facts) -> bool: ...      # 首板(人工标注,带日期) + 有支撑
def check_volume_breakout(facts) -> bool: ...      # 放量站上关键位 + 收阳
def check_first_move_5d(facts) -> bool: ...        # 5日线上行
def check_first_move_10d(facts) -> bool: ...       # 回调深但10日线方向未死
def check_first_move_drift(facts) -> bool: ...     # 5日线下移 + 尾盘突破明日5日线位置
```

**generator.py**：收集全部前提满足的策略 → **同票不去重，全部输出全部推送** → 过三个前置检查（见 §7.5）→ premise 校验（监管/龙虎榜/量能档位）→ ENTER 生成时 Z/W 行同生 → 持仓票按趋势生成保护行与 T 行 → 指数/板块上下文行 → time_trigger 行（周五收摊、财报T-1、节假日提前控仓提示）。

**持仓票每晚生成规则**：

| 趋势 | 生成 |
|---|---|
| UP | STOP 行（当前均线，每日滚动上移）＋ T_BUY（支撑）＋ T_SELL（压力） |
| DOWN | STOP 行（支撑位即风控线），不生成 BUY |
| UP→DOWN 翻转当晚 | 原 T_BUY 连坐 INVALIDATED，STOP 行 zone 更新为支撑位 |

**lifecycle.py**：唯一 `transition()` 入口，四状态终态不可逆。**lifecycle 只感知单信号，不感知链。** 链的推进由 settle/generator 在产生新信号时通过 parent_id 建立因果关系。

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
  applies_to: holding
  rows:
    - { side: BUY,  tag: t, zone: support_minus_1pct }
    - { side: SELL, tag: t, zone: resistance_minus_tick }

market_context:
  indices: [sh000001, sz399001, sh000688]
  sectors: pool 内票的 theme_map 归属，每板块一支撑一压力
```

**size_hint 修饰器（只两个）**：① 量能 LOW 档全部 BUY 减半；② T_BUY 档位通过 CLI `t-size` 落库（同等/加倍/减半）。**修饰器③（浮盈切10日线）彻底删除**——“跌破连反抽都没有，就看自己的利润或者纪律”是二选一的人为决策，系统不知道你的真实利润厚度，不做、不近似、不假装能做。

**size_hint 与报告的关系（写死）**：size_hint 不在报告主槽位单独占行；量能 LOW 减半时在候选行尾附可选字段 `仓位提示：减半（量能LOW）`；T_BUY 的 t-size 结果附在持仓区 T 行尾。这样既让读者知道仓位提示，又不让报告被 size_hint 撑散。

**config.yaml**：

```yaml
market_volume_tiers: ...      # 量能档位
regulator_list: ...           # 监管清单
lhb_dumpers: ...              # 砸盘席位
poll_intervals: ...           # 竞价1分钟/常规5-15分钟/尾盘1分钟
watchlist_max: 25             # 候选上限
holiday_calendar: ...         # 节假日日历：长假前最后一交易日输出“建议空仓过节”，停止生成新 BUY

retest_window_days: 3         # 观察哨有效期（交易日）
retest_volume_min: 1.2        # 放量站回量比下限（工程近似，C级）

reentry_rules:
  same_ma_cooldown_days: 2    # 同均线档位两次止损后冷却窗口
```

无杠杆字段。**已删除 `dedup_zone_overlap`**：生成层不合并，不需要重叠度阈值。

## 6. 建模：三层结构

**这是本版纠正 v6.1 的核心。** v6.1 第 6 节把“破位四幕”和状态机放在一起，容易被读成“链本身是一个状态机”。实际建模分三层，互不混用。

### 6.1 第一层：实体与四状态机（唯一的状态机层）

**状态机只有这一层，且只有四状态。**

| 实体 | 状态 | 说明 |
|---|---|---|
| Signal（tag=enter） | ACTIVE → TRIGGERED / EXPIRED / INVALIDATED | 候选与盘中买入是它的两个观察点 |
| Signal（tag=stop） | 同上 | 持仓期间每晚生成；破位是它 TRIGGERED |
| Signal（tag=retest_confirm） | 同上 | 破位后由 settle 生成；三种终态就是“判决” |
| Signal（tag=t / profit / index_context / sector_context / time_trigger） | 同上 | 各自独立生命周期 |
| PoolEntry | in / out | 成员关系，不是状态机 |
| Receipt | executed / ignored / veto | 标注，不驱动迁移 |

`lifecycle.py` 的 `TRANSITIONS` 只管这一层：

```python
TRANSITIONS = {
    ("ACTIVE", "hit"):            "TRIGGERED",
    ("ACTIVE", "eod"):            "EXPIRED",
    ("ACTIVE", "premise_broken"): "INVALIDATED",
    ("ACTIVE", "human_veto"):     "INVALIDATED",
}
```

**TRIGGERED 确认语义**：BUY 类连续两个轮询周期在区间内（或量比过阈）记 confirmed；单点触碰记 touched_only，不进假想期望统计。STOP 类单点触碰。

### 6.2 第二层：因果链（parent_id 接力，不是状态迁移）

“破位→反抽→再进场”不是一条信号在迁移，而是**多条信号通过 parent_id 接力**。链的推进是规则，不是状态迁移。

- **边由 parent_id 表示**：stop / profit / t / retest_confirm 必须指向原 enter；无 enter 履历的持仓票用合成值 `holding:<symbol>`。合成 parent 的信号链不进统计。
- **边的产生由 settle / generator 实现**：settle 在 stop 进终态时决定产不产生 retest_confirm；generator 在三个前置检查通过且价格到位时产生新 enter。
- **lifecycle.py 不感知链**：它只管单信号四状态。

完整因果规则表见 §7.6。

### 6.3 第三层：报告槽位投影（状态快照的叙事顺序）

某 date 的报告，是把**当时所有 Signal 的状态快照**按叙事顺序投影到槽位。投影规则见 §8。

**报告不产生状态，只读取状态。** `report --date <date>` 不改任何 Signal，不触发任何迁移。

### 6.4 为什么链不是状态机

用 S0–S7 这种“阶段命名”描述链是可以的，但**它是报告叙事的时间线章节，不是状态迁移图**。至少四个“阶段”不是任何信号的状态：

| 报告阶段 | 实际是什么 | 为什么不是状态 |
|---|---|---|
| 竞价确认 | auction_call 的一次事件 | 结果要么直接 INVALIDATED，要么信号仍 ACTIVE；没有“竞价中”这个状态 |
| 持仓 | pool 成员关系＋每晚新生成的 stop/t 行 | 没有一条叫“持仓”的 Signal；enter 早已终态 |
| 破位当天 | stop 信号自己的 ACTIVE→TRIGGERED | 不是 enter 的迁移；stop 是另一个信号 |
| 判决后选股 | 一个布尔判断（三个前置检查＋价格位置） | 为真则生成新 enter；为假什么都不发生 |

**结论**：状态机只有一层；链是因果关系不是状态迁移；报告是状态快照的叙事投影。§7 的“四幕”是因果链的实例，不是状态机。

## 7. 破位-反抽-再进场链的因果规则

### 7.1 破位当天定性（volume.py + watch.py）

STOP 单点触碰即 TRIGGERED，随后调用 `break_qualifier` 四路输出。定性只写 journal 的 status_reason 和推送文案，不改状态机。

| 定性 | 推送文案 | 当天动作建议 |
|---|---|---|
| EXECUTE_NOW | “破位，撤” | 当天走 |
| DELAY_OBSERVE | “缩量破位，疑似被动，收盘定生死” | 等收盘 |
| DIVERGENCE | “下跌但外盘占优，有承接，看不懂，收盘复核” | 等收盘 |
| LIMIT_EXEMPT | “封板附近失真，不定性” | 不判 |

### 7.2 收盘定生死（settle.py，四路分支写全）

```python
for stop in today_triggered_stops:
    q = stop.status_reason   # qualifier
    if q in ("DELAY_OBSERVE", "DIVERGENCE"):
        if close < stop.zone_low:
            create_retest_confirm(stop)      # 收盘仍破 → 生成 retest_confirm
        else:
            push_correction_notice(stop)     # 收盘收复 → 纠错提醒，不生成 retest_confirm
    elif q == "EXECUTE_NOW":
        create_retest_confirm(stop)          # 当天执行也生成：给纠错留门
    # LIMIT_EXEMPT → 不生成 retest_confirm，次日晚上正常生成新 STOP
```

**四条硬约束**：

1. `EXECUTE_NOW` **当天执行也生成 retest_confirm**。
2. `DELAY_OBSERVE` 与 `DIVERGENCE` **走同一段收盘判定**，不是两条独立路径。
3. `LIMIT_EXEMPT` **不生成 retest_confirm**，次日正常重算止损。
4. 生成 retest_confirm 时，zone 取原 STOP 的 zone（原支撑，现在它是压力）；parent_id 指向原 enter 或 `holding:<symbol>`；有效期 `config.retest_window_days`。

### 7.3 retest_confirm 的三终态

retest_confirm 是**一条信号**，它自己有 ACTIVE 段和终态段。三种终态各有自己的结案时刻，不是固定 T+3。

| 终态 | 判定依据 | 结案时刻 | 判定者 |
|---|---|---|---|
| TRIGGERED | 盘中价格摸进观察区间、收盘没站上区间上沿 | 盘中某次 watch 轮询（事件） | watch |
| INVALIDATED | 收盘站上区间上沿且量比 ≥ `retest_volume_min` | 盘中最后一次 watch 或收盘判定 | watch |
| EXPIRED | 窗口到期，价格从没摸进区间 | 到期日收盘后清点 | settle |

```python
# watch 对 retest_confirm 的判定
if price_in_zone and close < zone_high:
    transition(retest, "hit")                # TRIGGERED，reason=retest_fail
if close >= zone_high and volume_ratio >= cfg.retest_volume_min:
    transition(retest, "premise_broken")     # INVALIDATED，reason=retest_pass
    push_correction_notice(retest)
# 到期：settle 置 EXPIRED
```

**“当日结案”查法**：报告判决区取 `status in (TRIGGERED, INVALIDATED, EXPIRED)` 且 `closed_at` 日期 = 报告日期的 retest_confirm。**不是拿当前时间减立哨时间**，而是按 journal 的 closed_at 过滤。

### 7.4 纠错提醒统一

**两条路径、同一函数、文案一致**：

- 破位当日盘中收复（watch 发现）→ `push_correction_notice(stop)`；
- retest_confirm 放量站回（watch 判 INVALIDATED）→ `push_correction_notice(retest)`。

**统一语义**：都不迁移原 STOP 状态、都不发新买点、都不生成新信号。买回是人的动作，回执后系统才知道你还在。

### 7.5 判决后重新选股：三个前置检查（替代 depth）

generator 对每只 pool 票生成买点前，按序过三个前置检查。**这三个检查替代了旧的 depth 参数，depth 已废弃。**

```python
# 检查一：这只票还在反抽观察期里吗？
if has_active_retest_confirm(symbol):
    skip_buy()

# 检查二：这根均线最近是不是刚连续止损过？
# 暂停的是“均线档位”不是“票”——5日线暂停不影响10日线前提满足时出信号
if same_ma_stop_count(symbol, ma_n=5, days=cfg.reentry_rules.same_ma_cooldown_days) >= 2:
    exclude(["first_move_5d", "first_move_drift"])   # 5日线家族暂停

# 检查三：观察期结案了吗？价格在哪里？
# 观察期结案（TRIGGERED/EXPIRED/INVALIDATED 任一终态）→ 选股恢复
# 价格在哪根均线附近，哪个策略前提自然满足就出哪个
```

**“破5卖→10日再买”不是编排出来的**，是检查三在价格走到10日线时的自然结果：你在5日线止损出来，价格就处于“回调深”状态，check_first_move_10d 自然为真。

三条典型路径确认检查行为：

| 路径 | 检查一 | 检查二 | 结果 |
|---|---|---|---|
| 破5 → 反抽摸不过 → 确认破位 → 跌到10日线 | 结案后放行 | 5日线档暂停中 | **10日线先手前提满足 → 新买点** ✓ |
| 破5 → 放量站回 → 人纠错买回 | 结案后放行 | 无两次止损记录 | 管道开放，但价格远离均线，前提不满足 → **不出信号，等** ✓ |
| 破5 → 观察期里价格横跳 | 存续中挡住 | — | **零买点输出** ✓ |

### 7.6 因果规则表（parent_id 接力）

| 触发 | 条件 | 产生 | parent_id | 实现 |
|---|---|---|---|---|
| enter TRIGGERED | 回执入场 | 持仓上下文 | — | evening |
| 持仓中，每晚 evening | 趋势 UP/DOWN | 新 stop、t、profit 行 | → 原 enter | generator |
| stop TRIGGERED | EXECUTE_NOW | retest_confirm | → 原 enter 或 holding:<symbol> | settle |
| stop TRIGGERED | DELAY/DIVERGENCE 且收盘<zone_low | retest_confirm | 同上 | settle |
| stop TRIGGERED | DELAY/DIVERGENCE 且收盘收复 | 纠错提醒（无新信号） | — | settle |
| stop TRIGGERED | LIMIT_EXEMPT | 不产生 retest_confirm | — | settle |
| retest_confirm TRIGGERED | 反抽不过 | 链终止，等新 enter | — | watch |
| retest_confirm INVALIDATED | 放量站回 | 纠错提醒；人买回需新 enter | — | watch |
| retest_confirm EXPIRED | 到期无反抽 | 链终止，等新 enter | — | settle |
| 任意时刻 | 三个前置检查通过＋价格到位 | 新 enter | 无（新链起点） | generator |

**“破5卖→10日再买”不在这张表任何一格被“编排”**——它是最后一行规则在价格走到10日线时的自然结果。

## 8. 报告形态与槽位投影

报告由 CLI `report --date <date>` 生成，`push-file` 人工单向推送。槽位规范与措辞库按 moban.md 执行。以下是 v7.0 的槽位投影规则。

| 槽位 | 投影规则（读取条件） |
|---|---|
| A0 今日执行回顾 | 当日进入 TRIGGERED 的全部 Signal ＋对应 receipts 回执＋机械执行 vs 实际操作差异 |
| 环境行 | ACTIVE 的 index_context / sector_context / time_trigger |
| 候选区 | ACTIVE 的 enter（BUY），经三个前置检查过滤；同票多策略全部列出不合并；超 watchlist 上限标 truncated |
| 竞价快照 | auction_call 当日输出（弱转强/中性/强转弱/一字首开） |
| 盘中提醒 | watch 当日推送记录（confirmed / touched_only / premise_broken） |
| 持仓区 | pool 成员的 ACTIVE stop/t/profit |
| 破位区 | 当日进入 TRIGGERED 的 stop |
| 反抽观察区 | ACTIVE 的 retest_confirm |
| 判决区 | 当日 `closed_at` 的 retest_confirm 终态（三种终态任一） |

**两个关键约束**：

1. **观察区与判决区互斥**：同一条 retest_confirm 在同一天要么在观察区（ACTIVE），要么在判决区（当日刚结案），不会同时在两处。
2. **判决区只显示“当天刚结案”的**：前一日结案的，今日报告判决区不再出现；它已进入履历和执行记录。如需“近期已结案”回看，在判决区下加单独小节，按 `closed_at` 倒序取最近 N 条，与“当日判决”分开。

**零预案日两个变体**：

- **市场熔断型**（LOW 档+指数 DOWN）：报告主体只有“明日零预案”＋环境行＋持仓保护行（STOP 收紧为破位即 SELL）；
- **节假日型**（长假前最后一交易日）：输出“建议空仓过节”，停止生成新 BUY。

## 9. 统计（两层）

**信号质量层（纯行情可算）**：触达率；分策略假想期望（触发价机械执行、只用 confirmed、满30笔起算、滚动50笔期望≤0 → EV_NEG 仅记录不推送）；卖飞率 vs 避损率（STOP 后追踪5日：5日内收盘收复 zone_high 计纠错命中，两数同出一张表）；纠错提醒命中率；池子质量报表。合成 parent（`holding:` 前缀）的信号链不进统计。

**行为层（回执驱动，覆盖率<50% 明示“数据不足”）**：纪律执行率；做T增值粗算；复原违规次数。

**明确删除**：账户盈亏、连续亏损、违规计数、杠杆提醒、浮盈计算。

## 10. 测试与验收

test_ma（懂哥当时输入三元组，严禁事后反推）；test_trend；test_volume（破位四路＋封板豁免＋分歧规则＋两级查询）；test_selector（每策略一条正向用例、事实不足输出 no_strategy、互不耦合）；test_lifecycle（四状态全生命周期、T 连坐、合成 parent 不进统计）；test_ordering；test_replay（无网络依赖）；test_deps（core 不得 import data/db/notify）。

**验收锚点—测试文件映射**：

| 验收锚点 | 测试文件 |
|---|---|
| 缩量/分歧收盘仍破 → 生成 retest_confirm；收盘收复 → 只发纠错 | test_settle_branches |
| EXECUTE_NOW 当天执行仍生成 retest_confirm | test_settle_branches |
| LIMIT_EXEMPT 不生成 retest_confirm，次日正常生成 STOP | test_settle_branches |
| 纠错两条路径文案一致、不迁移状态、不发买点 | test_lifecycle |
| TRIGGERED / INVALIDATED＋纠错 / EXPIRED 各一条 | test_lifecycle |
| 观察期存续时选股对该票零输出 | test_reentry_checks |
| 结案且价格到位后 first_move_10d 自动出现 | test_reentry_checks |
| 同均线两次止损后 5日线家族暂停、10日线不受影响 | test_reentry_checks |
| 同票多策略全部输出全部推送，无合并 | test_selector |
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
| 全市场板块扫描 | 只做池内板块上下文行 |
| 账户追踪/自报/熔断器/杠杆提醒 | 系统不知道账户。唯一约束是市场熔断 |
| 定时任务/按钮回传/自动推送 | 纯 CLI 人工触发 |
| LLM / 网格回测 / 基本面闸门 / 真实仓位建模 | 将来以新 tag 或新报表增量加入，不动状态机和表结构 |

## 12. 废弃项

| 废弃项 | 原因 |
|---|---|
| depth 参数 | 不再用于策略选择或再入场深度控制；选股由三个前置检查＋价格位置决定。相关 config、签名、调用点一并清理 |
| `dedup_zone_overlap` | 生成层不合并，同票多策略全部输出全部推送，不需要重叠度阈值 |
| “三道闸/闸门”叫法 | 统一为“前置检查/条件”，避免生硬术语 |
| 浮盈切10日线修饰器 | 系统不知道真实利润厚度，不做、不近似 |

## 13. 当前实施状态（沿用 v6.1 并更新）

已实现：市场输入与尾盘快照、selector 按事实独立选策（depth 不再决定 5d/10d）、volume_breakout 独立 assignment、同票多策略不去重并全部推送、A0 今日执行回顾、零预案日严格报告、EV_NEG 报告记录、节假日日历、一字首开分类、`stats --date` 接线 correction/restore 数据、破位四路定性、retest_confirm 生成/到期/放量收复判决、观察哨存续禁买、同均线家族冷却、`settle/watch` CLI 接线。

**本版新增待实现**：settle 四路分支写全（EXECUTE_NOW 也生成 retest_confirm；LIMIT_EXEMPT 不生成）；纠错提醒统一函数；三个前置检查在 generator 中并列实现；config 补 `retest_window_days` / `retest_volume_min` / `reentry_rules`；删除 `dedup_zone_overlap`；depth 残留调用清理；报告判决区按 `closed_at` 过滤。

---

