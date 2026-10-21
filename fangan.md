# 交易信号系统技术方案（v5.0 终版）



## 0. 定位与铁律

一个**信号服务**：每晚收盘后计算点位、入库、push 汇总；盘中价格触达点位 push 提醒；信号过期或前提作废 push 撤销提醒。不下单、不连交易接口、不建模真实仓位、不记账。

三条铁律：

1. **系统世界里没有持仓，只有建议。** 仓位是从回执推算的布尔值，仅用于决定今晚生成哪几类点位。
2. **信号一生只有四个状态：ACTIVE → TRIGGERED / EXPIRED / INVALIDATED。** 人的动作（veto、回执）是对信号的标注，不是状态机的一环。
3. **策略就是 strategies.yaml 里的几段配置。** 每条策略必须写全 premise / y / z / w 四键，缺一加载即报错。

## 1. 行为建模（先于此后的一切结构）

系统的全部行为按触发源分三类，一个不多一个不少。

**时钟触发的例行行为（routines）：**

| 时刻 | 行为 | 输出 |
|---|---|---|
| 15:30 | 收盘生成 | 信号行入库＋汇总 push（含零预案判定） |
| 9:15–9:25 | 竞价判定 | 形态分类 → 作废当日 BUY 或快照供人确认 |
| 9:30–15:00 | 盘中盯梢 | 触发检查 → 确认 → 定性 → push |
| 14:30–15:00 | T 复原倒计时 | 未配对 T 提醒 |
| 15:05 | 收盘清点 | EXPIRED / INVALIDATED ＋ retest_confirm 生成 ＋ Z 滚动重算 |

**行情触发的事件（盯梢内部分支）：** 价格入区间 → 触发确认（BUY 双周期 / STOP 单点）；破位 → 两级定性（板块 → 指数）；STOP 当日收复 → 纠错提醒；前提破坏 → 作废＋撤销提醒。

**人触发的行为（全部可选，不驱动状态机）：** veto（信号作废）；receipt（executed / ignored，可带 size_filled）；pool 编辑（每晚给票池，这是唯一"天然会被给"的人工输入——不给池子系统今晚就没活干）；config 编辑（策略 YAML、阈值、清单、杠杆声明）。

## 2. 模块拆分与命名

按"谁拥有这类行为"切分，不按技术类型切。命名规则：**目录名是动词或职责名词，一句话能说清"什么时候被谁调用"；。

```
pointtable/
├── README.md
├── requirements.txt
├── config.yaml
├── strategies.yaml
├── pointtable/
│   ├── data/                   # "把数据拿进来并保证可信"
│   │   ├── akshare.py          # 行情/竞价/龙虎榜/指数/板块指数，重试+缓存
│   │   ├── guard.py            # 新鲜度断言 + 口径矛盾拦截
│   │   └── mock.py             # 测试假数据源
│   ├── core/                   # "纯判断与纯生成：不碰 IO 不碰库"
│   │   ├── ma.py
│   │   ├── trend.py
│   │   ├── volume.py           # 量比、承接、破位三问
│   │   ├── auction.py          # 竞价形态分类
│   │   ├── lhb.py              # 烂榜特征
│   │   ├── generator.py        # strategies.yaml × pool → 信号行（ENTER/Z/W 同生）
│   │   └── lifecycle.py        # 四状态迁移唯一入口
│   ├── jobs/                   # "编排时刻：什么时候干什么"
│   │   ├── evening.py          # 15:30 收盘生成
│   │   ├── auction_call.py     # 9:15–9:25 竞价判定
│   │   ├── watch.py            # 盘中盯梢
│   │   ├── restore_check.py    # 14:30 起 T 复原倒计时
│   │   └── settle.py           # 15:05 收盘清点
│   ├── notify/
│   │   ├── push.py             # 通道 + 按钮
│   │   └── render.py           # 一行一信号渲染
│   ├── db/
│   │   ├── session.py
│   │   └── models.py           # signals / journal / receipts / pool 四表
│   ├── stats.py                # 两层报表
│   └── cli.py
└── tests/
```

**拆分检验标准**：每个目录回答一个句式——feeds 只负责把数据拿进来并保证可信；judge 只负责纯判断；spawn 只负责生成；life 只负责状态迁移；routines 只负责编排时刻；tell 只负责让人知道；keep 只负责记住；stats 只负责算账。任何文件放不进这八个句式，说明它是新行为，先讨论归属再写代码。

**两个易混边界钉死**：guard 属于 feeds 不属于 judge（它管"数据能不能信"，是取数最后一道，不是判断）；破位三问属于 judge 不属于 watch（watch 只决定什么时刻调它，定性逻辑是纯函数）。

## 3. 数据模型（四表）

**signals（核心表）：**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | str PK | uuid |
| symbol | str | 证券代码；指数与板块指数同表 |
| strategy_id | str | strategies.yaml 条目；上下文行为固定值 |
| side | enum | BUY / SELL |
| tag | str | enter / stop / profit / t / retest_confirm / index_context / sector_context / time_trigger；仅 stats 分组用 |
| zone_low / zone_high | float | 强制区间，low<high，禁止单点 |
| size_hint | float | 三修饰器作用后的建议份额（见 §5.3） |
| premise | str | 生成时前提快照（题材+均线方向+未进监管+榜不烂） |
| trend | enum | UP / DOWN，决定支撑行 side（UP=BUY 买点，DOWN=SELL 风控线） |
| activation | str 可空 | **仅作文案提示**，watch 不做自动检查 |
| valid_policy | enum | intraday（日抛）/ until_stopped（跨日），settle 按此清点 |
| status | enum | ACTIVE / TRIGGERED / EXPIRED / INVALIDATED |
| status_reason | str | hit / eod / premise_broken / human_veto / retest_pass / retest_fail / auction_weak / lhb_veto |
| parent_id | str FK 可空 | stop / profit / t / retest_confirm 必填，指向 enter |
| created_at / closed_at | datetime | |

**journal**：每次迁移一行：signal_id、from、to、reason、价格、**confirmed**（confirmed / touched_only）、created_at。归因全部从日志重建。

**receipts**：signal_id、verdict（executed / ignored / veto）、size_filled（可空，成交几成，默认全额）、created_at。不驱动任何状态迁移。

**pool**：date / symbol / theme_label（人工标注题材）。选股不判断，但必须落表留痕——否则"池子质量"永远归不到因。

## 4. 判断模块（judge/）

**ma.py**（最高测试优先级）：`ma_tomorrow(closes, n, est_close)`。est_close 是尾盘两三点预估收盘价，非事后真实收盘价——评论区实测事后反推差 2%+。锚点：川环 26.5 / 博创 42.3 / 兆龙 60.5，对懂哥当时输入误差 <0.5%。

**trend.py**：收盘 >20 日线且 5 日线斜率向上 → UP，其余 DOWN。同一价位区间，UP 挂 BUY（上升期支撑=买点），DOWN 挂 SELL（下降期跌破支撑=卖点不是买点）。

**volume.py**：竞价量比用 9:25 定格值（不外推，懂哥"我一般计算好点位就记住了，不会再额外看分钟的分时"）；承接效率；`break_qualifier` 破位三问——放量主动 → EXECUTE_NOW 立即推；缩量被动 → DELAY_OBSERVE 收盘定生死。active_or_passive 用**两级查询**：先查该票板块行近 30 分钟有无 TRIGGERED，再查指数行，都平稳而个股独跌 → active。

**auction.py**：竞价轨迹分类。深水拉回零轴上方 → weak_to_strong（BUY 正常生效）；红盘持续走低 → strong_to_weak（该票当日 BUY 全部 INVALIDATED + push）；其余 neutral。

**lhb.py**：命中砸盘席位名单或卖方前五集中度过阈 → 次日 BUY premise 校验不通过不生成；持仓票 push 预警（"昨天的榜太烂了……竞价给红开就撤了"）。

## 5. 生成与策略（spawn/ + strategies.yaml）

generator.py 职责链：加载期校验四键齐全（缺一报错）→ premise 校验（监管清单、龙虎榜、量能档位）→ ENTER 生成时 **Z/W 行同生**（parent_id 关联——单边预案结构上不可能存在，这是"配对出场"的实现方式，不需要校验器）→ 持仓票追加 T 行 → 生成指数/板块上下文行 → 事件日历驱动 time_trigger 行（周五 14:30 收摊、财报 T-1 警报）。

```yaml
strategies:
  - id: first_move_5d
    side: BUY
    premise: 热门题材 + 5日线上行 + 未进监管 + 榜不烂
    y: { type: ma_tomorrow, n: 5, when: tail_30min }
    z: { type: ma_break, n: 5 }
    w: { type: pressure_zone, action: sell_part }
    on_stop: { reenter_at_next_ma: [10, 20], max_reentries: 2 }   # 破5卖→10日再买→再破→20日

  - id: volume_breakout
    side: BUY
    premise: 放量站上关键位 + 收阳量能确认 + 未进监管
    y: { type: pullback, ratio: 0.98 }
    z: { type: close_below, level: breakout_price }
    w: { type: next_resistance, trailing: true }

  - id: first_board_dip
    side: BUY
    premise: 首板 + 分时回踩 + 未进监管
    y: { type: intraday_support }
    z: { type: ma_break, n: 5 }

t_overlay:                        # 做T = 持仓票每晚多两行点位
  applies_to: holding
  rows:
    - { side: BUY,  tag: t, zone: support_minus_1pct }
    - { side: SELL, tag: t, zone: resistance_minus_tick }
  # 趋势翻转时 side 自动翻转；妖股（波动>阈值）不生成T行

market_context:                   # 指数+板块上下文行：可触发，TRIGGERED 仅作环境事实
  indices: [sh000001, sz399001, sh000688]
  sectors: 来自 pool 内票的 theme_map 归属，每板块一支撑一压力两行
  # 用途：破位定性两级查询的输入
```

**size_hint 三修饰器**（按序作用）：① 量能 LOW 档全部 BUY 减半；② T_BUY 的 push 带"同等/加倍/减半"三档按钮，你点哪个次日生效哪个（正T三式落地，信心是人的判断，系统只负责落点位）；③ 回执推算浮盈 >15% 时该持仓 STOP 从 5 日线切 10 日线（"利润厚放宽到10日"），回执覆盖率不足时停用并在 stats 明示。

**config.yaml**：market_volume_tiers（<9000亿 LOW / 9000–13000亿 MID / >13000亿 HIGH）；regulator_list、lhb_dumpers（人工维护）；轮询频率三段（竞价 1 分钟 / 常规 5–15 分钟可配 / 尾盘 1 分钟）；watchlist 硬上限 25 只（超限按 持仓票 > 指数 > 板块 > 候选池 截断）；**leverage: true/false**（一次性静态声明，仅用于 LOW 档时汇总 push 头部加一行红字"杠杆开着＋量能不足"）。

## 6. 状态机（life/machine.py）

```python
TRANSITIONS = {
    ("ACTIVE", "hit"):            "TRIGGERED",
    ("ACTIVE", "eod"):            "EXPIRED",
    ("ACTIVE", "premise_broken"): "INVALIDATED",  # 监管/趋势翻转/竞价强转弱/榜烂
    ("ACTIVE", "human_veto"):     "INVALIDATED",
}
# 终态不可逆。每次迁移写 journal。
```

**TRIGGERED 确认语义**：BUY 类要求连续两个轮询周期在区间内（或触发时量比过阈）记 confirmed；单点触碰记 touched_only，**不进假想期望统计**。STOP 类保持单点触碰——破位要的是快。

**破位两步确认**：支撑行被击穿 → settle 生成 retest_confirm 行（区间＝原支撑位，现在它是压力）→ 反抽摸到过不去 → TRIGGERED（"确认破位，撤"）；直接站回 → INVALIDATED（"假破位，虚惊"）。

**纠错提醒**：STOP 触发后当日价格收复破位线 → 只 push"收复止损位，可按纪律纠错"，不迁移状态。追不追永远是人。

## 7. 例行的每一天（routines/）

| 时刻 | 动作 |
|---|---|
| 15:30 evening | 拉数据 → guard 校验 → pool 落表 → 量能定档 → 龙虎榜 → 趋势判定 → 生成信号（含 T 行、on_stop 链、上下文行、time_trigger 行）→ **LOW 档叠加指数 DOWN 时输出"明日零预案"**（系统对人的唯一约束）→ push 汇总（头部标量能档位，杠杆开着时加红字） |
| 盘后 | 人工扫一眼，不顺眼 veto；不点默认生效 |
| 9:15–9:25 auction_call | 竞价形态分类；strong_to_weak 作废当日 BUY；一字首开类带快照供人确认（不写死阈值） |
| 9:30–15:00 watch | 分频率轮询：入区间 → 确认语义 → STOP 实时破位定性 → TRIGGERED＋push（STOP 永远排最前）；activation 仅进文案；STOP 当日收复 → 纠错提醒 |
| 14:30 起 restore_check | 每 15 分钟按回执序列（含 size_filled）推算未配对 T，倒计时提醒（"做正T无论成败收盘前恢复原仓位"） |
| 15:05 settle | 按 valid_policy 清点：日抛未触发 → EXPIRED；前提破坏 → INVALIDATED＋push 撤销；破位 → 生成 retest_confirm；持仓推算消失 → T 行连坐；先手持仓 Z 用 ma_tomorrow 滚动重算（旧值进 journal） |

## 8. 统计（stats/report.py，两层）

**信号质量层（纯行情可算，策略下线依据）**：触达率；分策略假想期望（触发价机械执行、只用 confirmed、满 30 笔起算，滚动 50 笔期望 ≤0 → 该 strategy_id 新信号标注 EV_NEG 仅记录不推送）；卖飞率 vs 避损率（STOP 触发后追踪 5 日）；纠错提醒命中率；**池子质量报表**（pool 落表票五日后平均表现、趋势方向分布——审计的是你的选股，不是策略）。

**行为层（回执驱动，覆盖率 <50% 明示"数据不足"）**：纪律执行率；做 T 增值粗算（T 行配对差价）；复原违规次数（回执推算得出，不依赖自觉登记）。

**明确删除的统计项**：账户盈亏、连续亏损、违规计数——这三个都依赖系统物理上拿不到的数据，删掉比假装能算更诚实。

## 9. 测试

test_ma（三案例懂哥当时输入，严禁事后反推）；test_trend（side 翻转）；test_machine（合法迁移 / 非法拒绝 / 终态不可逆 / retest_confirm 生成 / T 连坐 / activation 不影响生死）；**test_ordering**（同 tick 同时命中支撑与压力，断言 STOP 在推送队列头部、先卖后买）；test_replay（mock 重放历史行情，断言信号序列与排序，无网络依赖）。

## 10. 系统边界（明确不做）

| 不做 | 原因 |
|---|---|
| 选股/题材判断 | 懂哥："没啥指标，就一眼感觉"。系统不判断，但 pool 落表并审计池子质量 |
| 支撑压力精密算法 | 他本人："没想好怎么用文字描述"。机械代理＋premise 标注"未经人眼确认"，人可 veto |
| 日内纠错决策 | 只发收复提醒，追不追是人 |
| 个股与大盘压力冲突优先级 | "没有那么一尘不变，人是活的" |
| 全市场板块扫描 | 只做池内板块上下文行 |
| 账户状态追踪 / 自报 / 熔断 | 系统不知道账户，自报等于不报。对"人"的唯一约束是市场环境熔断（零预案日），纯行情可判定 |
| LLM / 网格回测 / 基本面闸门 / 真实仓位建模 | 成本与误杀率不匹配；将来以新 tag 或新报表增量加入，不动状态机和表结构 |

---

### **一句话总览**

整张 signals 表就是这个系统的全部世界：**每晚它替你算好明天的均线、盯好量能档位、查完龙虎榜和监管名单，把点位摆在你面前；盘中它替你看住每一个价位，破了位替你看清是主动砸还是被板块带崩，到点替你做T倒计时；收盘它替你清点尸体、立好反抽的观察哨。市场差到构架不起行情时它敢于什么都不推。而选哪只票、信不信这个点位、破位了追不追回来、账户里还剩多少钱——这四件事永远是你的，系统不猜、不问、不假装知道。**

