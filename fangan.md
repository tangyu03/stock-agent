# 交易信号系统技术方案（v6.0 定稿）

> 本版为全部讨论的最终收敛：行为建模、模块拆分、命名、数据模型、策略集、报告形态、统计分层均已定稿。与此前任何版本冲突处以本版为准。

---

## 0. 定位与铁律

一个**信号服务**：每晚收盘后计算点位、入库、push 汇总；盘中价格触达点位 push 提醒；信号过期或前提作废 push 撤销提醒。不下单、不连交易接口、不接 LLM、不建模真实仓位、不记账、不知道你的账户。

三条铁律：

1. **系统世界里没有持仓，只有建议。** 仓位是从回执推算的布尔值，仅用于决定今晚生成哪几类点位。系统不知道你的杠杆、担保比例、盈亏总额—— README 只写一句边界声明："若使用两融，所有 size_hint 对你偏乐观，请自行打折。"
2. **信号一生只有四个状态：ACTIVE → TRIGGERED / EXPIRED / INVALIDATED。** 人的动作（veto、回执）是对信号的标注，不是状态机的一环。
3. **策略就是 strategies.yaml 里的几段配置。** 每条策略必须写全 premise / y / z / w 四键，缺一加载即报错。加打法 = 加一段 YAML。

## 1. 行为建模

系统的全部行为按触发源分三类，一个不多一个不少。

**时钟触发（routines）：**

| 时刻 | 行为 | 输出 |
|---|---|---|
| 15:30 | 收盘生成 | 信号入库＋汇总 push（含零预案判定） |
| 9:15–9:25 | 竞价判定 | 形态分类 → 作废当日 BUY 或快照供人确认 |
| 9:30–15:00 | 盘中盯梢 | 触发检查 → 确认 → 定性 → push |
| 14:30–15:00 | T 复原倒计时 | 未配对 T 提醒 |
| 15:05 | 收盘清点 | EXPIRED / INVALIDATED ＋ retest_confirm 生成 ＋ Z 滚动重算 |

**行情触发（盯梢内部分支）：** 价格入区间 → 触发确认（BUY 双周期 / STOP 单点）；破位 → 两级定性（板块 → 指数）；STOP 当日收复 → 纠错提醒；前提破坏 → 作废＋撤销提醒。

**人触发（全部可选，不驱动状态机）：** veto；receipt（executed / ignored，可带 size_filled）；pool 编辑（每晚给票池——不给池子系统今晚没活干，天然会被给）；config 编辑。

**对"人"的唯一约束**：市场环境熔断——LOW 档叠加指数 DOWN → 输出"明日零预案"。纯行情可判定，不依赖用户承认任何事。

## 2. 项目结构（惯例命名，职责句式写进 README）

```
pointtable/
├── README.md
├── ARCHITECTURE.md
├── CHANGELOG.md
├── requirements.txt            # akshare / pyyaml / pydantic / apscheduler
├── config/
│   ├── strategies.yaml         # 全部策略（四键校验：premise/y/z/w）
│   ├── config.yaml             # webhook、量能档位、监管清单、砸盘席位、轮询频率、watchlist上限
│   └── theme_map.yaml          # 股票→主板块 人工 overrides（保留原样）
├── pointtable/
│   ├── models/                 # 领域模型层：这个世界里有什么
│   │   ├── signal.py           # Signal + 四状态枚举 + zone校验(low<high)
│   │   ├── journal.py          # JournalEntry（含 confirmed / touched_only）
│   │   ├── receipt.py          # Receipt（executed/ignored/veto + size_filled）
│   │   └── pool.py             # PoolEntry（date/symbol/theme_label）
│   ├── db/
│   │   └── session.py          # 连接与持久化，不含模型定义
│   ├── data/                   # 把数据拿进来并保证可信
│   │   ├── akshare.py          # 行情/竞价/龙虎榜/指数/板块指数/两融/主力资金流，重试+缓存
│   │   ├── guard.py            # DataFreshnessGuard + 脏数据闸门 + 量比×换手口径交叉校验
│   │   └── mock.py             # 测试假数据源
│   ├── core/                   # 纯判断与纯生成：不 import data/db/notify，只被 jobs 调用（CI 强制）
│   │   ├── ma.py               # ma_tomorrow（预估收盘价语义）
│   │   ├── trend.py            # UP/DOWN（支撑压力角色互换）
│   │   ├── volume.py           # 量比档/换手分位/主动差/承接效率/破位三问/封板豁免/分歧规则
│   │   ├── auction.py          # 竞价形态分类
│   │   ├── lhb.py              # 烂榜特征（砸盘席位+卖方集中度）
│   │   ├── sector.py           # 板块三态判定(主线/轮动/退潮) + 池内排序(去弱择强) + theme_map读取
│   │   ├── generator.py        # strategies.yaml × pool → 信号行（ENTER/Z/W同生、premise校验、T行、上下文行、time_trigger行）
│   │   └── lifecycle.py        # 四状态迁移唯一入口
│   ├── jobs/                   # 编排时刻：目录即作息表
│   │   ├── evening.py          # 15:30 收盘生成（pool落表→量能定档→龙虎榜→趋势→生成→零预案判定→push）
│   │   ├── auction_call.py     # 9:15–9:25 竞价判定
│   │   ├── watch.py            # 盘中（触发确认、两级定性、STOP排最前、纠错提醒）
│   │   ├── restore_check.py    # 14:30起 T复原倒计时
│   │   └── settle.py           # 15:05 清点（valid_policy、retest_confirm、T连坐、Z滚动）
│   ├── notify/
│   │   ├── push.py             # 人工触发单向通知；不做按钮回传
│   │   └── render.py           # 槽位报告（P1-P7/N1-N3/A1-A4/C1 + 措辞库 + 降级规则）
│   ├── stats.py                # 两层报表（信号质量层 + 行为层）
│   └── cli.py                  # receipt / veto / t-size / stats / holdings / pool / push-file
├── legacy/                     # 冻结区：回测引擎、旧测试批次、被删模块源码
└── tests/
    ├── fixtures/               # 川环26.5/博创42.3/兆龙60.5（懂哥当时输入三元组）
    ├── test_ma.py / test_trend.py / test_volume.py / test_lifecycle.py
    ├── test_ordering.py / test_replay.py / test_deps.py
```

**硬边界**：core/ 不 import data/、db/、notify/，只被 jobs/ 调用——用依赖方向测试在 CI 里强制，防止 core 腐化成垃圾抽屉。guard 属于 data（数据能不能信是取数最后一道）；破位三问属于 core（watch 只决定什么时刻调它）。

## 3. 数据模型（四表）

**signals（核心表）：**

| 字段 | 说明 |
|---|---|
| id / symbol / strategy_id | symbol 含指数与板块指数 |
| side | BUY / SELL（仅二值） |
| tag | enter / stop / profit / t / retest_confirm / index_context / sector_context / time_trigger，仅 stats 分组用 |
| zone_low / zone_high | 强制区间，low<high，禁止单点 |
| size_hint | 三修饰器作用后的建议份额（见 §5.3） |
| premise | 生成时前提快照（题材+均线方向+未进监管+榜不烂） |
| trend | UP / DOWN，决定支撑行 side |
| activation | **仅作文案提示**，watch 不做自动检查 |
| valid_policy | intraday（日抛）/ until_stopped（跨日），settle 按此清点 |
| status / status_reason | 四状态；hit / eod / premise_broken / human_veto / retest_pass / retest_fail / auction_weak / lhb_veto |
| parent_id | stop / profit / t / retest_confirm 必填，指向 enter；连坐作废与归因的基础 |
| created_at / closed_at | |

**journal**：每次迁移一行，含 **confirmed**（confirmed / touched_only）。归因全部从日志重建。

**receipts**：verdict（executed / ignored / veto）、size_filled（可空，默认全额）。不驱动状态迁移。

**pool**：date / symbol / theme_label。pool = 持仓清单＋手动自选，选股不判断但必须落表留痕。

## 4. 判断模块（core/）

**ma.py**（最高测试优先级）：`ma_tomorrow(closes, n, est_close)`，est_close 是尾盘预估收盘价，非事后真实收盘价。锚点：川环26.5 / 博创42.3 / 兆龙60.5，对懂哥当时输入误差 <0.5%，严禁事后反推。

**trend.py**：收盘>20日线且5日线斜率向上 → UP，其余 DOWN。**支撑压力角色互换的载体**：同一价位区间，UP 挂 BUY（支撑=买点），DOWN 挂 SELL（跌破支撑=卖点不是买点）。

**volume.py**：竞价量比用 9:25 定格值，不外推；`break_qualifier` 破位三问——放量主动→EXECUTE_NOW 立即推；缩量被动→DELAY_OBSERVE 收盘定生死。active_or_passive **两级查询**：先查该票板块行近30分钟有无 TRIGGERED，再查指数行，都平稳而个股独跌→active。

**auction.py**：weak_to_strong（BUY 正常生效）/ strong_to_weak（当日 BUY 全部 INVALIDATED＋push）/ neutral。

**lhb.py**：命中砸盘席位或卖方集中度过阈 → 次日 BUY 不生成；持仓票 push 预警。

**generator.py**：加载期校验四键；premise 校验（监管/龙虎榜/量能档位）；**ENTER 生成时 Z/W 行同生**（parent_id 关联——单边预案结构上不可能存在）；持仓票生成规则见下表；指数/板块上下文行；time_trigger 行（周五收摊、财报T-1）。

**持仓票每晚生成规则（两条铁律的合流）：**

| 趋势 | 生成 | 对应原话 |
|---|---|---|
| UP | STOP 行（5日或10日线，每日滚动上移）＋ T_BUY（支撑）＋ T_SELL（压力） | "破5日就是卖点""上升期支撑是买点" |
| DOWN | STOP 行（支撑位即风控线），不生成 BUY | "下降期支撑是风控，跌破是卖点不是买点" |
| UP→DOWN 翻转当晚 | 原 T_BUY 连坐 INVALIDATED，STOP 行 zone 更新为支撑位 | "跌破连反抽都没有，就看纪律" |

**"均线绝不做扛单理由"的结构实现**：STOP 行 valid_policy=until_stopped 且每晚用 ma_tomorrow 滚动重算——上升期止损线每日上移，价格停下来就撞线。系统不判断你扛不扛，它只保证那条线一直在追。

**lifecycle.py**：唯一 `transition()` 入口，四状态终态不可逆。

## 5. 策略与配置

```yaml
strategies:
  - id: first_move_5d           # 强势贴线：尾盘买在明天5日线上，破5日走
    side: BUY
    premise: 热门题材 + 5日线上行 + 未进监管 + 榜不烂
    y: { type: ma_tomorrow, n: 5, when: tail_30min }
    z: { type: ma_break, n: 5 }
    w: { type: pressure_zone, action: sell_part }

  - id: first_move_10d          # 回调深但10日线方向未死：等更稳的线
    side: BUY
    premise: 热门题材 + 回调深但10日线方向未死 + 未进监管
    y: { type: ma_tomorrow, n: 10, when: tail_30min }
    z: { type: ma_break, n: 10 }
    w: { type: pressure_zone, action: sell_part }

  - id: first_move_drift        # 5日线下移：等明天的线比今天低，尾盘突破即进
    side: BUY
    premise: 热门题材 + 5日线正在下移 + 尾盘突破明日5日线位置
    y: { type: ma_tomorrow, n: 5, direction: descending }
    z: { type: ma_break, n: 5 }
    w: { type: pressure_zone, action: sell_part }

  - id: first_board_dip
    side: BUY
    premise: 首板 + 分时回踩 + 未进监管
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
  # 趋势翻转 side 自动翻转；妖股（波动>阈值）不生成T行

market_context:                 # 可触发，TRIGGERED 仅作环境事实（破位定性两级查询的输入）
  indices: [sh000001, sz399001, sh000688]
  sectors: pool 内票的 theme_map 归属，每板块一支撑一压力
```

**再入场链不在策略配置里**：它是 generator 行为——STOP 触发后持仓推算消失，下一晚自动检查 first_move_10d（乃至20日档）的 premise 是否满足，满足即生成新 BUY 行。"破5卖→10日再买"是换策略，不是同策略降级；stats 里两种先手分开算账。上限2次由 generator 按 parent 链深度控制。

**size_hint 修饰器**：量能 LOW 档全部 BUY 减半；T_BUY 档位通过 CLI `t-size` 落库，不做按钮回传。浮盈>15% 后 STOP 切 10 日线不实施：系统不保存可靠持仓成本和浮盈。

**config.yaml**：market_volume_tiers（<9000亿 / 9000–13000亿 / >13000亿）；regulator_list、lhb_dumpers（人工维护）；轮询频率三段（竞价1分钟 / 常规5–15分钟 / 尾盘1分钟）；watchlist 上限25只（超限按 持仓>指数>板块>候选 截断）。**无杠杆字段——已删**。

## 6. 状态机与关键流程

```python
TRANSITIONS = {
    ("ACTIVE", "hit"):            "TRIGGERED",
    ("ACTIVE", "eod"):            "EXPIRED",
    ("ACTIVE", "premise_broken"): "INVALIDATED",
    ("ACTIVE", "human_veto"):     "INVALIDATED",
}
```

**TRIGGERED 确认语义**：BUY 类要求连续两个轮询周期在区间内（或触发时量比过阈）记 confirmed；单点触碰记 touched_only，**不进假想期望统计**。STOP 类保持单点触碰——破位要的是快。

**破位两步确认**：支撑行击穿 → settle 生成 retest_confirm 行（区间＝原支撑位，现在它是压力）→ 反抽摸到过不去 → TRIGGERED（"确认破位，撤"）；直接站回 → INVALIDATED（"假破位，虚惊"）。

**纠错提醒**：STOP 触发后当日收复破位线 → 只 push 提醒，不迁移状态。追不追永远是人。

**多信号同刻触发**：按 tag 排序推送，stop > retest_confirm > profit > t > enter——先卖后买，test_ordering 覆盖。

## 7. 报告形态（render.py）
报告由 CLI `report --date <date> [--market-json <file>] [--output <file>]` 生成。系统只渲染市场档位、指数/板块环境、池内建议点位、股票名称、当前价或预估收盘价等可测事实；报告中不伪造成本、账户盈亏和成交。

推送由 CLI `push-file` 人工触发，保持单向通知；没有定时任务自动推送，也没有按钮回传。执行/否决/T 档动作仍然通过 `receipt`、`veto`、`t-size` 等 CLI 命令落库。

## 8. 统计（两层）

**信号质量层（纯行情可算，策略下线依据）**：触达率；分策略假想期望（触发价机械执行、只用 confirmed、满30笔起算，滚动50笔期望≤0 → 该 strategy_id 新信号标注 EV_NEG 仅记录不推送）；卖飞率 vs 避损率（STOP 后追踪5日）；纠错提醒命中率；**池子质量报表**（pool 票五日后平均表现、趋势方向分布——审计选股，不审计策略）。

**行为层（回执驱动，覆盖率<50% 明示"数据不足"）**：纪律执行率；做T增值粗算（T 行配对差价）；复原违规次数。

**明确删除**：账户盈亏、连续亏损、违规计数、杠杆提醒——都依赖系统物理拿不到或永远为真的数据，删掉比假装能算诚实。

## 9. 测试

test_ma（懂哥当时输入，严禁事后反推）；test_trend（side 翻转）；test_lifecycle（合法迁移/非法拒绝/终态不可逆/retest_confirm/T连坐/activation不影响生死）；test_ordering（同 tick 支撑压力同时命中，STOP 排头部、先卖后买）；test_replay（mock 重放，断言信号序列与排序，无网络依赖）。依赖方向测试：core/ 不得 import data/、db/、notify/。

## 10. 系统边界

| 不做 | 原因 |
|---|---|
| 选股/题材判断 | "没啥指标，就一眼感觉"。pool 落表并审计质量，不判断 |
| 支撑压力精密算法 | "没想好怎么用文字描述"。机械代理＋premise 标注，人可 veto |
| 日内纠错决策 | 只发收复提醒 |
| 个股与大盘压力冲突优先级 | "人是活的" |
| 全市场板块扫描 | 只做池内板块上下文行 |
| 账户追踪/自报/熔断器/杠杆提醒 | 系统不知道账户，自报等于不报，恒真提醒等于噪音。唯一约束是市场熔断（零预案日） |
| LLM / 网格回测 / 基本面闸门 / 真实仓位建模 | 将来以新 tag 或新报表增量加入，不动状态机和表结构 |

---

### **一句话总览**

整张 signals 表就是这个系统的全部世界：**每晚它替你算好明天的均线、查完监管和龙虎榜，把买点卖点摆在你面前；盘中它替你看住每一个价位，破位时替你看清是主动砸还是被板块带崩；收盘它替你清点尸体、立好反抽的观察哨；市场差到构架不起行情时它敢于什么都不推。选哪只票、信不信这个点位、破位了追不追回来、账户里还剩多少钱——这四件事永远是你的，系统不猜、不问、不假装知道。**
