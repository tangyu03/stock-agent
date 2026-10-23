# 交易信号系统技术方案（v6.1 定稿）


---

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
| 15:05 | settle 收盘清点 | EXPIRED / INVALIDATED ＋ retest_confirm 生成 ＋ Z 滚动重算 |

**行情触发（watch 内部分支）：** 价格入区间 → 触发确认（BUY 双周期 / STOP 单点）；破位 → 两级定性（板块 → 指数）＋封板豁免＋分歧规则；STOP 当日收复 → 纠错提醒；前提破坏 → 作废＋撤销提醒。

**人触发（全部可选，不驱动状态机）：** veto；receipt（executed / ignored，可带 size_filled）；t-size；pool 编辑（唯一天然会被给的人工输入——不更新池子系统今晚没活干）；config 编辑；push-file 人工单向推送。

**对"人"的唯一约束**：市场熔断——LOW 档叠加指数 DOWN → 输出"明日零预案"，且**持仓票的 STOP 行全部收紧为破位即 SELL**（逆大势时反弹是卖点不是买点）。纯行情可判定，不依赖用户承认任何事。

## 2. 项目结构

```
pointtable/
├── README.md
├── ARCHITECTURE.md
├── CHANGELOG.md
├── requirements.txt            # akshare / pyyaml / pydantic
├── config/
│   ├── strategies.yaml         # 全部策略（四键校验）
│   ├── config.yaml             # webhook、量能档位、监管清单、砸盘席位、阈值、节假日日历
│   └── theme_map.yaml          # 股票→主板块 人工 overrides
├── pointtable/
│   ├── models/                 # 领域模型层：这个世界里有什么
│   │   ├── signal.py           # Signal + 四状态枚举 + zone校验(low<high)
│   │   ├── journal.py          # JournalEntry（含 confirmed / touched_only）
│   │   ├── receipt.py          # Receipt（executed/ignored/veto + size_filled）
│   │   └── pool.py             # PoolEntry（date/symbol/theme_label，即持仓清单）
│   ├── db/
│   │   └── session.py          # 连接与持久化，不含模型定义
│   ├── data/                   # 把数据拿进来并保证可信
│   │   ├── akshare.py          # 行情/竞价/龙虎榜/指数/板块指数/两融/主力资金流，重试+缓存
│   │   ├── guard.py            # DataFreshnessGuard + 脏数据闸门 + 量比×换手口径交叉校验
│   │   └── mock.py             # 测试假数据源
│   ├── core/                   # 纯判断与纯生成：不 import data/db/notify（CI 强制）
│   │   ├── ma.py               # ma_tomorrow（预估收盘价语义）
│   │   ├── trend.py            # UP/DOWN（支撑压力角色互换）
│   │   ├── volume.py           # 量比档/换手分位/主动差/破位三问/封板豁免/分歧规则
│   │   ├── auction.py          # 竞价形态分类（含一字首开）
│   │   ├── lhb.py              # 烂榜特征（砸盘席位+卖方集中度）
│   │   ├── sector.py           # 板块三态(主线/轮动/退潮) + 池内排序(去弱择强) + theme_map读取
│   │   ├── selector.py         # 策略前提检查（每策略一个纯函数，独立并行）
│   │   ├── generator.py        # 前提满足集 × pool → 信号行（ENTER/Z/W同生、冷却闸门）
│   │   └── lifecycle.py        # 四状态迁移唯一入口
│   ├── jobs/
│   │   ├── evening.py          # 15:30
│   │   ├── auction_call.py     # 9:15–9:25
│   │   ├── watch.py            # 盘中
│   │   ├── restore_check.py    # 14:30 起
│   │   └── settle.py           # 15:05
│   ├── notify/
│   │   ├── push.py             # 人工触发单向通知（push-file），不做按钮回传
│   │   └── render.py           # 槽位报告（见 §7）
│   ├── stats.py                # 两层报表
│   └── cli.py                  # receipt / veto / t-size / stats / holdings / pool / report / push-file
├── legacy/                     # 冻结区：回测引擎、旧测试批次、被删模块源码
└── tests/
    ├── fixtures/               # 川环26.5 / 博创42.3 / 兆龙60.5（懂哥当时输入三元组）
    ├── test_ma.py / test_trend.py / test_volume.py / test_lifecycle.py
    ├── test_ordering.py / test_replay.py / test_deps.py / test_selector.py
```

## 3. 数据模型（五表）

**signals（核心表）**：id / symbol / strategy_id / side（BUY/SELL）/ tag（enter / stop / profit / t / retest_confirm / index_context / sector_context / time_trigger，仅 stats 分组用）/ zone_low、zone_high（强制区间）/ size_hint / premise（生成时前提快照，含 premise_facts 结构化字段）/ trend（UP/DOWN）/ activation（仅文案提示）/ valid_policy（intraday / until_stopped）/ status / status_reason（hit / eod / premise_broken / human_veto / retest_pass / retest_fail / auction_weak / lhb_veto）/ parent_id（stop/profit/t/retest_confirm 必填，指向 enter；无 enter 履历的持仓票用合成值 `holding:<symbol>`，此类信号链不进统计）/ created_at / closed_at。

**journal**：每次迁移一行，含 confirmed（confirmed / touched_only）。归因全部从日志重建。

**receipts**：verdict（executed / ignored / veto）、size_filled（可空，默认全额）。不驱动状态迁移。

**pool**：date / symbol / theme_label。**就是持仓清单，无角色区分、无来源标记**——你在里面就是持仓，不在就不是。指数与板块上下文不经过 pool，由 config 的 market_context 固定生成。

**tail_snapshots（新增）**：date / symbol / price，14:30 由 watch 顺手写入。ma_tomorrow 的 est_close 优先取它；无快照才 fallback 当日收盘价，且生成信号的日志必须标注口径（`est_close=tail_snapshot` 或 `fallback=close`）——**严禁无标注地用事后真实收盘价**。

## 4. 判断模块（core/）

**ma.py**（最高测试优先级）：`ma_tomorrow(closes, n, est_close)`。est_close 是尾盘预估收盘价（来自 tail_snapshots），非事后真实收盘价。锚点：川环26.5 / 博创42.3 / 兆龙60.5，对懂哥当时输入误差 <0.5%。

**trend.py**：收盘>20日线且5日线斜率向上 → UP，其余 DOWN。同一价位区间，UP 挂 BUY（支撑=买点），DOWN 挂 SELL（跌破支撑=卖点不是买点）。

**volume.py**：竞价量比用 9:25 定格值，不外推；`break_qualifier` 破位三问＋前置豁免＋第三路：

```python
def break_qualifier(change_pct, drive, vgear, sector_triggered, index_triggered) -> str:
    if limit_exempt(change_pct, code, name): return "LIMIT_EXEMPT"   # 封板内外盘失真
    # 主动/被动两级查询：先板块行近30分钟有无TRIGGERED，再指数行
    if 放量 and 主动:        return "EXECUTE_NOW"     # 立即推"破位，撤"
    if 下跌 but 外盘占优:     return "DIVERGENCE"      # 有人在接，看不懂别装懂
    return "DELAY_OBSERVE"                            # 缩量被动，收盘定生死
```

**auction.py**：四类分类——weak_to_strong（BUY 正常生效）/ strong_to_weak（当日 BUY 全部 INVALIDATED＋提醒）/ **limit_up_break（昨日涨停今日竞价打开：带竞价量比＋开盘涨幅快照供人确认，"量+开盘涨幅需要动态分析"，不写死阈值）** / neutral。

**lhb.py**：命中砸盘席位或卖方集中度过阈 → 次日 BUY 不生成；持仓票 push 预警。

**sector.py**：主线/轮动/退潮三态判定；池内去弱择强排序（破位/未破位、上游/下游、一线/二线）；theme_map 读取。

**selector.py（新增）**：策略前提检查，每策略一个纯函数，**独立并行、无优先级排序**：

```python
def check_first_board_dip(facts) -> bool: ...      # 首板(人工标注,带日期) + 有支撑
def check_volume_breakout(facts) -> bool: ...      # 放量站上关键位 + 收阳
def check_first_move_5d(facts) -> bool: ...        # 5日线上行
def check_first_move_10d(facts) -> bool: ...       # 回调深但10日线方向未死
def check_first_move_drift(facts) -> bool: ...     # 5日线下移 + 尾盘突破明日5日线位置
```

**generator.py**：收集全部前提满足的策略 → **同票不用去重**→ **再入场冷却闸门**（见下）→ premise 校验（监管/龙虎榜/量能档位）→ ENTER 生成时 Z/W 行同生 → 持仓票按趋势生成保护行与 T 行 → 指数/板块上下文行 → time_trigger 行（周五收摊、财报T-1、**节假日提前控仓提示**）。

**再入场链的正确语义**：STOP 触发后该票离开 pool（或你手动移出），策略管道对它关闭；价格继续跌到下一档均线位置时，first_move_10d 的前提**自然满足**（"回调深"是破位止损的必然结果），新 BUY 行自动生成——链条靠价格位置涌现，不靠 depth 参数编排。**冷却闸门只防一件事**：同 symbol 同均线 n 日内出现两次 STOP TRIGGERED 后，同档位策略进入冷却，防止"同一根线上反复打脸"。冷却规则进 config 的 reentry_rules，不进策略选择。

**持仓票每晚生成规则**：

| 趋势 | 生成 |
|---|---|
| UP | STOP 行（当前均线，每日滚动上移）＋ T_BUY（支撑）＋ T_SELL（压力） |
| DOWN | STOP 行（支撑位即风控线），不生成 BUY |
| UP→DOWN 翻转当晚 | 原 T_BUY 连坐 INVALIDATED，STOP 行 zone 更新为支撑位 |

**lifecycle.py**：唯一 `transition()` 入口，四状态终态不可逆。

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
  applies_to: holding                       # pool 内的票
  rows:
    - { side: BUY,  tag: t, zone: support_minus_1pct }
    - { side: SELL, tag: t, zone: resistance_minus_tick }

market_context:
  indices: [sh000001, sz399001, sh000688]
  sectors: pool 内票的 theme_map 归属，每板块一支撑一压力
```

**size_hint 修饰器（最终版，只两个）**：① 量能 LOW 档全部 BUY 减半；② T_BUY 档位通过 CLI `t-size` 落库（同等/加倍/减半）。**修饰器③（浮盈切10日线）彻底删除**——"跌破连反抽都没有，就看自己的利润或者纪律"是二选一的人为决策，系统不知道你的真实利润厚度，不做、不近似、不假装能做。

**config.yaml**：market_volume_tiers；regulator_list、lhb_dumpers；轮询频率建议（竞价1分钟/常规5–15分钟/尾盘1分钟）；watchlist 上限25只；**节假日日历**（长假前最后一交易日：输出"建议空仓过节"，停止生成新 BUY）。无杠杆字段。

## 6. 状态机与破位完整时序

```python
TRANSITIONS = {
    ("ACTIVE", "hit"):            "TRIGGERED",
    ("ACTIVE", "eod"):            "EXPIRED",
    ("ACTIVE", "premise_broken"): "INVALIDATED",
    ("ACTIVE", "human_veto"):     "INVALIDATED",
}
```

**TRIGGERED 确认语义**：BUY 类连续两个轮询周期在区间内（或量比过阈）记 confirmed；单点触碰记 touched_only，不进假想期望统计。STOP 类单点触碰。

**破位四幕时序（懂哥操作链的完整映射）**：

| 幕 | 发生什么 | 系统行为 | 原文依据 |
|---|---|---|---|
| 一、破位当下 | 盘中击穿止损线 | break_qualifier 定性：放量主动→EXECUTE_NOW 当天就走；缩量被动→DELAY_OBSERVE 延迟判决 | "主动放量算跌破，受板块拖累可以多忍一忍" |
| 二、等反抽 | 破位后1–3日 | settle 生成 retest_confirm 行（区间＝原支撑位，现在它是压力）；此期间策略管道对该票关闭——**反抽不是新买点** | "等跌破，看是否有反抽50" |
| 三、三种结局 | 反抽摸不过 | retest_confirm TRIGGERED（"确认破位，撤"），忍住没走的必须走 | "反抽不过去是有效跌破" |
| | 放量站回 | retest_confirm INVALIDATED（"假破位"）＋纠错提醒，**追不追是人** | "纠错呗" |
| | 连反抽都没有 | 观察哨到期 EXPIRED；利润厚不厚、走不走，是你的决策，系统不代答 | "就看自己的利润或者纪律" |
| 四、判决之后 | 价格跌到下一档均线 | first_move_10d 前提自然满足，生成新买点；上限由冷却闸门控制 | "到10日再买，再破再卖" |

**纠错提醒**：STOP 触发后当日收复破位线 → 只提醒，不迁移状态。**多信号同刻触发**：按 tag 排序，stop > retest_confirm > profit > t > enter，先卖后买，test_ordering 覆盖。

## 7. 报告形态（render.py）

报告由 CLI `report --date <date>` 生成，`push-file` 人工单向推送。槽位规范与措辞库按 moban.md 执行，补充两处：

**A0 今日执行回顾（新增，置于明日计划之前）**：列出当日全部 TRIGGERED 信号、对应 receipts 回执、机械执行 vs 实际操作的差异——journal 与 receipts 现成可查，对应"回测自己亏损最大的交易"的日频版。

**零预案日**：报告主体只有一行"明日零预案"+环境行+持仓保护行。

## 8. 统计（两层）

**信号质量层（纯行情可算）**：触达率；分策略假想期望（触发价机械执行、只用 confirmed、满30笔起算、滚动50笔期望≤0 → EV_NEG 仅记录不推送）；卖飞率 vs 避损率（STOP 后追踪5日：5日内收盘收复 zone_high 计纠错命中，两数同出一张表）；纠错提醒命中率；池子质量报表（pool 票五日后表现、趋势方向分布——审计选股）。合成 parent（`holding:` 前缀）的信号链不进统计。

**行为层（回执驱动，覆盖率<50% 明示"数据不足"）**：纪律执行率；做T增值粗算；复原违规次数。

**明确删除**：账户盈亏、连续亏损、违规计数、杠杆提醒、浮盈计算。

## 9. 测试

test_ma（懂哥当时输入三元组，严禁事后反推）；test_trend；test_volume（破位三问＋封板豁免＋分歧规则＋两级查询）；test_selector（每策略一条正向用例、事实不足输出 no_strategy、互不耦合）；test_lifecycle（含 retest_confirm 全生命周期、T 连坐、合成 parent 不进统计）；test_ordering；test_replay（无网络依赖）；test_deps（core 不得 import data/db/notify）。

## 10. 系统边界

| 不做 | 原因 |
|---|---|
| 选股/题材判断 | "没啥指标，就一眼感觉"。pool 落表并审计质量，不判断 |
| 支撑压力精密算法 | 机械代理＋premise 标注，人可 veto |
| 日内纠错决策 | 只发收复提醒，追不追是人 |
| 破位后"利润或纪律"的二选一 | 系统不知道你的利润厚度，不做、不近似 |
| 个股与大盘压力冲突优先级 | "人是活的" |
| 全市场板块扫描 | 只做池内板块上下文行 |
| 账户追踪/自报/熔断器/杠杆提醒 | 系统不知道账户。唯一约束是市场熔断（零预案日＋持仓 STOP 收紧） |
| 定时任务/按钮回传/自动推送 | 纯 CLI 人工触发，系统不假装在你不在时替你工作 |
| LLM / 网格回测 / 基本面闸门 / 真实仓位建模 | 将来以新 tag 或新报表增量加入，不动状态机和表结构 |

---

### **一句话总览**

整张 signals 表就是这个系统的全部世界：**每晚它替你算好明天的均线、查完监管和龙虎榜，把买点卖点摆在你面前；盘中你想看的时候它替你看住每一个价位，破位时替你看清是主动砸还是被板块带崩，然后立好反抽的观察哨；收盘它替你清点尸体；长假前它劝你休息，市场差到构架不起行情时它什么都不推。选哪只票、信不信这个点位、破位了追不追回来、利润厚不厚、账户里还剩多少钱——这五件事永远是你的，系统不猜、不问、不假装知道。**

## 11. 当前实施状态（2026-10-22）

已实现：市场输入与尾盘快照、selector 按事实独立选策（depth 不再决定 5d/10d）、volume_breakout 独立 assignment、同票多策略不去重并全部推送、A0 今日执行回顾、零预案日严格报告、EV_NEG 报告记录、节假日日历、一字首开分类（次日滚动识别节假日）、`stats --date` 接线 correction/restore 数据、破位四路定性、retest_confirm 生成/到期/放量收复判决、观察哨存续禁买、同均线家族冷却、`settle/watch` CLI 接线。

补丁说明：用户最终裁决同票不去重；即使多个策略 zone 重叠，也全部保留并推送。观察哨存续会禁止 BUY；同均线家族两次 STOP 冷却只冷掉该家族，不冷掉整票。
