# stock-agent

A股短线信号 Agent。当前版本定位是**瘦身后的信号服务**：只保留数据质量、择时判断、信号生命周期、调度推送和交易回执闭环；不再运行基本面闸门、估值透镜、LLM 解释器或复杂组合预算。所有报告以原始价位、指标、事件和回执为主，避免无法还原来源的黑盒透视。

## 当前能力

### 数据层

- akshare 封装与安全调用：`src/data_layer/akshare_adapter.py`、`src/data_layer/akshare_safe.py`。
- 问财/技能数据封装：`src/data_layer/skill_wrapper.py`，需在 `.env` 或环境变量设置 `IWENCAI_API_KEY`。
- 数据新鲜度检查：`src/loop/data_freshness.py` 中的 `DataFreshnessGuard`。
- 脏数据闸门：`src/analyzers/signal_plan.py` 校验量比、换手率和成交量口径；口径矛盾时拦截入场。
- 主题归属：`config/theme_map.yaml` 提供人工确认的股票→主题映射。

### 信号链路

1. `TimingEngine` 生成入场候选，覆盖恐慌抄底、套利低吸、确认追强、价量突破和趋势延续。
2. `signal_plan` 重估基准价、止损、目标区间、风险比例、置信度与分档。
3. `signal_lifecycle` 把状态信号转成事件：突破当日诞生，默认 5 个自然日有效；跌回突破位、板块退潮或超期后作废。
4. `live_scheduler` 按受众分流：空仓者收买入建议，持仓者收加仓评估；卖出信号全量输出。
5. `templates` 渲染瘦身后的 PushPlus 报告；信号卡只保留规则、原始价位、止损目标、建议仓位和事件来源。

入场信号以事件为准，同日同策略不重复建档；再入场由新事件触发，不使用沉没成本驱动的固定次数闸门。

### PushPlus 报告结构

- 顶层只保留：①结论与今日行动、②现役事件、③相对上一推送 DIFF、④数据未取到。
- 买入/卖出/观察卡只展示规则、现价、触发/基准价、止损、目标、建议仓位、关键价位和必要持仓回执。
- 不再渲染全池口径、量能分诊索引、评分闸门口径、板块提示、撮合计数、独立点位决策、环境附录、七问长诊断和资金/基本面全文。

### 出场规则

- 破位止损是硬触发：现价跌破系统止损价即输出卖出信号。
- 止损类技术信号采用分级 OR；止盈类信号继续保留计票。
- MA5 压制、冲高止盈、技术走弱和板块退潮仍作为系统级出场检查。
- 已删除“买入假说对应的 Z/W 配对出场”，系统不再把买入文案映射成独立卖出承诺。

### 记录与复盘

- `trade_logs` 记录信号价位、事件 ID、驱动源、板块版本、用户回执、实际成交和盈亏。
- 卖出回执自动回填开仓行的 `exit_price`、`exit_date`、`exit_reason` 和 `pnl_pct`。
- 策略统计按 `entry_type` 分层；达到样本阈值后可标记业绩下线。
- 事件跟踪保留入场、离场、作废、过期和成交状态，方便回看原始判定。

## 运行

```bash
python -m src.main run --phase pre_market
python -m src.main run --phase intraday
python -m src.main run --phase post_market
python -m src.main run --phase weekly
```

交易回执：

```bash
python scripts/trade_feedback.py --list
python scripts/trade_feedback.py --execute <id> --price 127.5
python scripts/trade_feedback.py --ignore <id>
python scripts/trade_feedback.py --holdings
```

诊断命令：

```bash
python scripts/trade_feedback.py --stats
python scripts/trade_feedback.py --strategies
python scripts/trade_feedback.py --projection-errors
python scripts/trade_feedback.py --sensitivity
python scripts/trade_feedback.py --sector-versions
```

## 配置

主要配置在 `config/timing.yaml`：

| 配置 | 用途 |
|---|---|
| `timing.data_guard` | 量比、换手率、成交量一致性拦截 |
| `timing.signal_lifecycle.valid_days` | 事件有效期 |
| `timing.exit.graded_or.enabled` | 止损类技术信号是否启用分级 OR |
| `timing.strategy_stats` | 分层统计和自动下线样本窗口 |
| `timing.volume_projection` | 盘中量能外推窗口与阈值 |

推送 token 配置在 `config/push.yaml` 的 `pushplus.token`。Windows 控制台建议设置：

```bash
export PYTHONIOENCODING=utf-8
```

数据库初始化与迁移：

```bash
python -m src.main init
```

## 已退役模块

以下复杂链路已从当前主信号链路移除：

- XYZW 可证伪假说与假说出厂拒绝闸门。
- 买入假说和配对 Z/W 出场逻辑。
- 基本面闸门、估值透镜、标签语义模块。
- RRR 纪律、风险乘数规则化引擎、EV 闸门。
- 板块 S1–S3 调度层、LLM 调用、`position_budget` 和解锁日历事件。
- 独立回测引擎与回测驱动的主链路。

保留的业绩统计和回执闭环只用于审计与策略下线判断，不会重新生成这些已退役闸门。

## 测试

```bash
python -X utf8 -m pytest -q
```

最近一次全量结果：

```text
531 passed, 4 skipped
```

已知边界：部分外部数据源在无网或无密钥环境下会自动降级；分钟级数据能力和机构成交均价近似仍依赖上游数据质量。
