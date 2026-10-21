# 交易信号系统技术方案

> 定位、形态、规则均已与懂哥六篇原文（做T的心得、先手买法、人性与交易、经得起风浪、背离、久违的1.3万亿、4.24午复盘）逐条核对完毕。本文档替代此前所有版本。

## 0. 定位与三条铁律

一个**信号服务**：每晚收盘后计算点位、入库、push 汇总；盘中价格触达点位 push 提醒；信号过期或前提作废 push 撤销提醒。不下单、不连交易接口、不接 LLM、不建模真实仓位。

三条铁律，来自全部讨论的收敛：

1. **系统世界里没有持仓，只有建议。** 仓位是从回执推算的布尔值（持有/空仓），仅用于决定今晚为这只票生成哪几类点位。
2. **信号一生只有四个状态：ACTIVE → TRIGGERED / EXPIRED / INVALIDATED。** 人的动作（过目、执行、回执、否决）是对信号的标注，不是状态机的一环。
3. **策略就是 strategies.yaml 里的几段配置。** 没有模板库、注册表、家族、编译器。加打法 = 加一段 YAML。

## 1. 设计哲学：懂哥体系的分工映射

懂哥方法论的三个支柱在系统里各有归属：

- **量能回答"真的还是假的"** → 破位三问定性（触发时实时计算）、市场量能总闸（每晚定档）、竞价形态分类（9:25 判定）、龙虎榜否决（每晚校验）。
- **先手买法回答"在哪进场成本最低"** → `ma_tomorrow` 每晚推算明日5/10/20日均线区间，尾盘买在明天的均线上。
- **做T回答"持仓期间怎么增值"** → 持有中的票每晚多生成两行点位（支撑低吸、压力高抛），14:30 起倒计时校验仓位复原。

贯穿一切的地基：**题材必须热门（选股由人，系统不碰）、纪律必须压过情绪（系统负责盯梢和提醒，人负责执行）**。

## 2. 项目结构

```
pointtable/
├── README.md
├── requirements.txt            # akshare / pyyaml / pydantic / apscheduler
├── config.yaml                 # 数据源、push webhook、量能档位阈值、监管票清单、砸盘席位名单
├── strategies.yaml             # 全部策略定义（见 §5）
├── pointtable/
│   ├── db.py                   # SQLite + SQLAlchemy
│   ├── models.py               # signals / journal / receipts 三张表
│   ├── datafeed/
│   │   ├── akshare_client.py   # 行情/竞价/龙虎榜/指数，含重试与当日缓存
│   │   └── mock_feed.py        # 测试假数据源
│   ├── core/
│   │   ├── ma.py               # ma_tomorrow：明日均线（纯函数，注意预估收盘价语义）
│   │   ├── volume.py           # 竞价量比、承接效率、破位三问定性
│   │   ├── trend.py            # 趋势状态 UP/DOWN 判定
│   │   ├── auction_pattern.py  # 竞价形态：weak_to_strong / strong_to_weak
│   │   ├── lhb_summary.py      # 龙虎榜烂榜特征判定
│   │   ├── generator.py        # 读 strategies.yaml → 生成信号行
│   │   └── lifecycle.py        # 状态迁移唯一入口
│   ├── jobs/
│   │   ├── evening.py          # 15:30：数据→量能定档→龙虎榜校验→趋势判定→生成→push汇总
│   │   ├── auction.py          # 9:15-9:30：竞价形态分类，强转弱作废当日BUY
│   │   ├── monitor.py          # 9:30-15:00：每分钟轮询 → 触发时定性 → push
│   │   ├── t_restore.py        # 14:30起每15分钟：T仓位复原倒计时提醒
│   │   └── settle.py           # 15:05：EXPIRED/INVALIDATED清点、破位→反抽确认行
│   ├── notify.py               # push 通道（server酱/webhook，消息带"做了/没做/否决"按钮）
│   └── cli.py                  # receipt / veto / stats / holdings
└── tests/
    ├── fixtures/               # 川环/博创/兆龙三案例（存懂哥当时的输入，非事后收盘价）
    ├── test_ma.py
    ├── test_trend.py
    ├── test_lifecycle.py
    └── test_replay.py          # 历史行情重放 → 期望信号序列
```

## 3. 数据模型

**signals（唯一核心表）：**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | str PK | uuid |
| symbol | str | 证券代码（指数同表，tag=index_context） |
| strategy_id | str | 指向 strategies.yaml 条目 |
| side | enum | **BUY / SELL**（仅二值，决定 push 话术与 monitor 行为） |
| tag | str | 自由字符串：enter / stop / profit / t / retest_confirm / index_context，仅用于 stats 分组，系统逻辑不读 |
| zone_low / zone_high | float | 强制区间，low < high 校验，禁止单点 |
| size_hint | float | 建议份额，量能 LOW 档时全局减半 |
| premise | str | 生成时前提快照（"热门题材+5日线上行+未进监管+榜不烂"） |
| trend | enum | UP / DOWN，生成时判定，决定支撑行 side（UP=BUY 买点，DOWN=SELL 风控线） |
| activation | str 可空 | 激活条件表达式（如"明日指数成交额>今日×1.1"），不满足则当日休眠，不触发不死亡 |
| valid_date | date | 生效日，日抛信号收盘未触发即 EXPIRED |
| status | enum | **ACTIVE / TRIGGERED / EXPIRED / INVALIDATED** |
| status_reason | str | hit / eod / premise_broken / human_veto / retest_pass / retest_fail / auction_weak / lhb_veto |
| parent_id | str FK 可空 | STOP/PROFIT/T/retest_confirm 行必填，指向 ENTER；连坐作废与归因的数据基础 |
| created_at / closed_at | datetime | |

**journal：** 每次状态迁移一行：signal_id、from、to、reason、当时价格、created_at。归因全部从日志重建，signals 表只留当前状态。

**receipts（人工标注，可选）：** signal_id、verdict（executed / ignored / veto）、created_at。**不驱动任何状态迁移**，只喂养行为层统计和持仓推算。

## 4. 核心模块

### 4.1 core/ma.py（纯函数，最高测试优先级）

```python
def ma_tomorrow(closes: list[float], n: int, est_close: float) -> float:
    """明日N日均线 = (最近N-1日收盘和 + 预估明日收盘价) / N。
    closes 长度 == n-1，否则 ValueError。
    关键语义：est_close 是"尾盘两三点时的预估收盘价"，不是事后真实收盘价。
    懂哥评论区实测：用事后真实收盘价反推川环得27.11，与他标注的26.5差2%+
    ——fixture 必须存他当时的输入三元组（当日均线值、预估收盘价、标注答案）。"""
```

验收锚点：川环科技 26.5 / 博创科技 42.3 / 兆龙互连 60.5，对懂哥当时输入的误差 < 0.5%。

### 4.2 core/volume.py

```python
def auction_volume_ratio(today: float, yesterday: float) -> float: ...
def absorption_efficiency(price_change_pct: float, amount_yi: float) -> float: ...

def break_qualifier(volume_state: str, active_or_passive: str, has_rebound: bool) -> str:
    """破位三问，触发时刻实时计算，不在生成时预设。
    放量主动 → EXECUTE_NOW（立即push"破位，撤"）
    缩量被动 → DELAY_OBSERVE（不推交易动作，收盘定生死，照常生成反抽确认行）
    懂哥原话："主动放量算跌破，受板块指数拖累可以多忍一忍"。"""
```

`active_or_passive` 的判定依赖指数行：破位时刻往前30分钟内，若指数行有 TRIGGERED（指数也在破）→ passive；指数平稳而个股独跌 → active。**这是指数行必须可触发的原因。**

### 4.3 core/trend.py

```python
def judge_trend(closes: list[float]) -> str:
    """UP: 收盘>20日线 且 5日线斜率向上。其余 DOWN。
    支撑压力角色互换的载体：同一价位区间，UP 时 side=BUY（上升期支撑=买点），
    DOWN 时 side=SELL（下降期跌破支撑=卖点不是买点，懂哥原话）。"""
```

### 4.4 core/auction_pattern.py

```python
def classify_auction(auction_series: list[tuple[str, float]]) -> str:
    """9:15-9:25竞价价格轨迹分类。
    深水拉回零轴上方 → weak_to_strong（BUY信号正常生效，"还能多看一眼"）
    红盘持续走低     → strong_to_weak（该票当日BUY全部INVALIDATED，push"竞价强转弱"）
    其余             → neutral（不干预）"""
```

### 4.5 core/lhb_summary.py

每晚对持仓票与候选票的龙虎榜做极简结构化：是否命中 config 里的砸盘席位名单、卖方前五集中度是否超阈值。命中"烂榜"→ 该票次日 BUY 信号 premise 校验不通过不生成；持仓票 push 预警（懂哥4.24原文："昨天的榜太烂了……竞价给红开就撤了"）。

### 4.6 core/lifecycle.py

```python
TRANSITIONS = {
    ("ACTIVE", "hit"):            "TRIGGERED",
    ("ACTIVE", "eod"):            "EXPIRED",
    ("ACTIVE", "premise_broken"): "INVALIDATED",  # 含监管/趋势翻转/竞价强转弱/榜烂
    ("ACTIVE", "human_veto"):     "INVALIDATED",
}
# 终态不可逆。每次迁移写 journal。
# STOP触发后若当日价格收复破位线 → 不迁移状态，仅push"收复止损位，可按纪律纠错"
# （纠错决策永远是人，系统只做提醒）
```

## 5. 策略定义（strategies.yaml 全文）

```yaml
strategies:
  - id: first_move_5d            # 先手5日：尾盘买在明天的均线上
    side: BUY
    tag: enter
    premise: 热门题材 + 5日线上行 + 未进监管 + 榜不烂
    y: { type: ma_tomorrow, n: 5, when: tail_30min }
    z: { type: ma_break, n: 5 }             # 破位定性触发时实时算，不预设
    w: { type: pressure_zone, action: sell_part }
    on_stop: { reenter_at_next_ma: [10, 20], max_reentries: 2 }  # 破5卖→10日再买→再破再卖→20日再买

  - id: volume_breakout          # 价量突破（保留）
    side: BUY
    tag: enter
    premise: 放量站上关键位 + 收阳量能确认 + 未进监管
    y: { type: pullback, ratio: 0.98 }
    z: { type: close_below, level: breakout_price }
    w: { type: next_resistance, trailing: true }

  - id: first_board_dip          # 首板分时低吸
    side: BUY
    tag: enter
    premise: 首板 + 分时回踩 + 未进监管
    y: { type: intraday_support }
    z: { type: ma_break, n: 5 }

t_overlay:                       # 做T不是策略，是"持有中的票多两行点位"
  applies_to: holding            # 回执推算为持有的票
  rows:
    - { side: BUY,  tag: t, zone: support_minus_1pct,   size_hint: 0.5成 }
    - { side: SELL, tag: t, zone: resistance_minus_tick, size_hint: 0.5成 }
  # 趋势翻转时同价位行 side 自动翻转；妖股（波动>阈值）不生成T行
  # 懂哥原话："优刻得、并行、青云这类偶尔一两次可以，但尽量不要做T，波动太大"

index_context:                   # 指数点位：可触发，但TRIGGERED仅作环境事实
  symbols: [sh000001, sz399001, sh000688]
  rows_per_symbol: 2             # 一支撑一压力
  # 用途有二：破位定性时判主动/被动；activation条件的判定依据
```

config.yaml 中的全局开关：

```yaml
market_volume_tiers:             # 市场量能总闸（懂哥：9000亿构架不起行情；1.3万亿是宣言）
  low: 9000亿以下      # 全部BUY信号size_hint减半，汇总push头部标红"量能不足"
  mid: 9000-13000亿
  high: 13000亿以上    # 正常
watchlist:
  regulator_list: []   # 监管/异动票，人工每晚维护；命中→BUY不生成+持仓票最高优先级警报
  lhb_dumpers: []      # 砸盘席位名单，人工维护
```

## 6. 每日运行时刻表

| 时刻 | 任务 | 动作 |
|---|---|---|
| 15:30 | evening.py | 拉数据 → 市场量能定档 → 龙虎榜校验 → 趋势判定 → 生成信号（含持仓票的T行、STOP的下一档再入场链）→ push 当日预案汇总（一条消息，头部标注量能档位） |
| 盘后 | 人工 | 扫一眼汇总，看谁不顺眼 `cli.py veto <id>` 或点消息按钮；不点则默认生效。**默认生效，可一键否决，无签字锁版仪式** |
| 9:15-9:30 | auction.py | 竞价形态分类；strong_to_weak → 当日该票 BUY 全部 INVALIDATED + push；一字首开类信号带竞价量比+涨幅快照 push 人工确认（懂哥："量+开盘涨幅，需要动态分析"，不写死阈值） |
| 9:30-15:00 | monitor.py | 每分钟轮询 ACTIVE（含指数行）：价格入区间 → 查 activation（不满足则休眠）→ STOP 行实时 break_qualifier 定性 → TRIGGERED + push。STOP 永远排最前；STOP 触发后当日收复破位线 → push"纠错提醒" |
| 14:30 起 | t_restore.py | 每15分钟按回执序列推算未配对T，推送"T仓位未复原，距收盘X分钟"（懂哥铁律：做正T无论成败收盘前恢复原仓位） |
| 15:05 | settle.py | 日抛未触发→EXPIRED；前提破坏→INVALIDATED+push撤销提醒；破位票生成反抽确认行（tag=retest_confirm，区间=原支撑位，现在它是压力了）；持仓推算消失→其T行连坐INVALIDATED；先手类持仓的Z用ma_tomorrow滚动重算，旧值进journal |

**破位两步确认完整流程**：支撑行被击穿 → settle 生成 retest_confirm 行（区间=原支撑位）→ 次日反抽摸到但过不去 → TRIGGERED（push"确认破位，撤"）；直接站回去 → INVALIDATED（push"假破位，虚惊"）。（懂哥原文："等跌破，看是否有反抽50，反抽不过去那么50大概率是有效跌破；如果跌破50连反抽都没有，那就看自己的利润或者纪律。"）

## 7. 统计（stats 输出）

**信号质量层（纯行情可算，策略下线依据）：**
- 触达率：ACTIVE 中 TRIGGERED 占比。全 ACTIVE 从不触发 = 点位画太远。
- **卖飞率 vs 避损率**：STOP 触发后追踪5日行情，对比"按线止损"与"扛单"的事后差异——懂哥"回测自己亏损最大的交易，是卖飞多还是亏损变多"的内置报表。

## 8. 测试与验收

- test_ma：三案例 fixture 存懂哥当时输入（预估收盘价），误差 < 0.5%。**严禁用事后真实收盘价反推**。
- test_trend：UP/DOWN 判定与 side 翻转。
- test_lifecycle：全部合法迁移、非法迁移拒绝、终态不可逆、破位→retest_confirm 生成、持仓消失 T 行连坐、activation 休眠不死亡。
- test_replay：mock_feed 重放历史行情，断言期望信号序列。全部测试无网络依赖。

## 9. 系统边界（明确不做的，及原因）

| 不做 | 原因 |
|---|---|
| 选股/题材判断 | 懂哥本人："没啥指标，就一眼感觉""市场选题材，我负责在里面挑"。票池由人每晚给出 |
| 支撑压力位的精密算法 | 他本人："没想好怎么用文字描述"。系统用机械代理（前高前低/15分钟密集区/缺口），premise 标注"机械推算，未经人眼确认"，人可 veto 修正 |
| 日内纠错决策 | "纠错呗"依赖"盘面证明我错了"的不可结构化判断。系统只发收复提醒，追不追永远是人 |
| 个股与大盘压力冲突的优先级 | "没有那么一尘不变，人是活的" |
| LLM / 板块层 / 网格回测 / 基本面闸门 / 真实仓位建模 / 盘中自动校验 | 成本与误杀率不匹配；将来需要时以"新 tag 信号行"或"stats 新报表"增量加入，不动状态机和表结构 |

---

### **一句话总览**

整张 signals 表就是这个系统的全部世界：**每晚它替你算好明天的均线、盯好量能档位、查完龙虎榜和监管名单，把点位摆在你面前；盘中它替你看住每一个价位，破了位替你看清是主动砸还是被带崩，到点替你做T倒计时；收盘它替你清点尸体、立好反抽的观察哨。而选哪只票、信不信这个点位、破位了追不追回来——这三个决定永远是你的，因为它们恰恰是懂哥自己都拒绝写成公式的那部分。**
