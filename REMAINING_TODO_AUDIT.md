# 待办实施方案（已收口）

记录时间：2026-10-22 14:26 CST  
裁决更新：2026-10-22 14:56 CST

本文档只描述后续实施方案，不改变当前运行行为。整体边界固定为：

- 纯后端，所有动作由 CLI 手动触发。
- 没有定时任务自动推送。
- 没有按钮回传或交互回调。
- 推送只保留人工触发的单向通知：`push-file` / `shadow-run --push-test`。
- 人的动作通过 `receipt`、`veto`、`t-size`、`pool-add` 等 CLI 命令落库。
- 系统不建模账户、不知道真实仓位、不下单。

---

## 审计边界

所有任务输出 JSON 或报告文件，不自动调用推送。

当前人工回执入口：

```bash
python -m pointtable.cli --db pointtable/data/pointtable.db receipt <signal_id> --verdict executed --size-filled 100
python -m pointtable.cli --db pointtable/data/pointtable.db veto <signal_id>
python -m pointtable.cli --db pointtable/data/pointtable.db t-size <signal_id> --choice same
```

当前人工推送入口：

```bash
python -m pointtable.cli --db pointtable/data/pointtable.db push-file \
  --title "2026-10-22 复盘" \
  --file reports/2026-10-22.md \
  --channel test
```

---

## 已裁决：不做池子角色分工

2026-10-22 裁决：`holding` 和 `candidate` 不做区分。

执行口径：

- `market-input` 只按股票代码处理池子。
- 同一个池内代码不做 holding/candidate 分流。
- 对池内代码生成买卖信号；`pool.role` 不参与信号生成决策。
- 后续如果保留 `pool.role`，只作为存量字段或展示信息，不再控制生成路径。

因此原方案中的“candidate 出策略、holding 出持仓保护行”的拆分不再实施。

---

## 已裁决：首版不做策略自动择优

2026-10-22 裁决：首版不实现策略自动择优。

执行口径：

- 不建立策略优先级。
- 不在多条策略中做“二选一”。
- 如果未来某个策略命中，所有命中策略都报出，不区分先后。
- `volume_breakout` 的标准本身清晰，后续接入时也应作为独立规则并行评估：
  - 放量突破；
  - 收阳确认；
  - 未进监管；
  - 命中即生成对应 BUY/Z/W。

当前先保留已实现的 `first_move_5d → first_move_10d` 再入场链，不做更大的策略选择器。

---

## P1：盘中破位定性接入

2026-10-22 已完成。`watch --context-json` 接入板块/指数上下文，STOP 定性写入返回值和 journal reason；无 context 时输出 `NOT_CLASSIFIED`，状态迁移不变。

### 现状

`qualify_break()` 已存在，但 `run_watch()` 没有把板块/指数上下文接入。

`watch --broken-id` 目前是人工指定 premise 破坏。

### 改法

给 `watch` 增加可选 `--context-json`：

```json
{
  "300001": {
    "volume_ratio": 1.5,
    "active_delta": 1000000,
    "stock_triggered": true,
    "sector_triggered": false,
    "index_triggered": false,
    "divergence": false,
    "limit_up": false
  }
}
```

`run_watch()` 在 STOP 触发时调用 `qualify_break()`。

结果只写入本次命令返回值和 journal reason，不改状态机：

```json
{
  "touched": ["stop-id"],
  "break_decisions": [
    {
      "signal_id": "stop-id",
      "qualifier": "EXECUTE_NOW",
      "reason": "active_volume_break"
    }
  ]
}
```

如果没有 context JSON，STOP 仍按单点触发，定性输出 `NOT_CLASSIFIED`，不猜结论。

### 测试

- 放量主动破位 → `EXECUTE_NOW`。
- 缩量被动破位 → `DELAY_OBSERVE`。
- 板块/指数同跌 → `DELAY_OBSERVE`。
- 无 context → `NOT_CLASSIFIED`。
- context 不改变状态迁移，只影响定性。

### 验收

- watch 仍是单次 CLI 调用。
- 没有 context 不阻塞触点记录。

---

## 已裁决：不做 premise 破坏自动化

2026-10-22 裁决：premise 破坏自动化不做。

执行口径：

- 不新增盘中 premise 自动检测。
- 不新增 `premise-check`。
- 不按 premise 变化自动作废信号。
- 保留现有显式 CLI 参数 `watch --broken-id <signal_id>` 作为人工兜底。

---

## P2：T 复原倒计时

2026-10-22 已完成。`restore-check --date` 基于 T_BUY/T_SELL 信号和 executed 回执配对，并补充尾盘价、阻力上沿和距尾盘分钟数；只读提醒。

### 现状

`restore_reminders()` 目前只识别 STOP 触发后价格收复，还没有“未配对 T”的概念。

### 改法

T 配对规则基于信号和回执，不基于账户：

- 同一 `symbol`
- 同一 `parent_id`
- 同一交易日
- `T_BUY` 有 `executed` 回执
- `T_SELL` 无 `executed` 回执

新增函数：

```python
find_unpaired_t_buy(signals, receipts) -> list[UnpairedT]
```

新增 CLI：

```bash
python -m pointtable.cli restore-check --date 2026-10-22
```

该命令读取：

- 当日 T_BUY / T_SELL
- receipts
- `tail_snapshots`

输出示例：

```json
{
  "unpaired_t_buy": [
    {
      "signal_id": "2026-10-22-300001-t-buy",
      "symbol": "300001",
      "tail_price": 10.2,
      "resistance_zone_high": 10.5,
      "minutes_since_tail": 18
    }
  ]
}
```

只提醒，不改状态、不推送。

### 测试

- T_BUY executed、T_SELL 未 executed → 未配对。
- 两边 executed → 不提醒。
- 无 receipts → 无法判断，输出 `NO_RECEIPT`。
- STOP 收复逻辑保持原样。

### 验收

- `restore-check` 不写信号状态。
- 不需要账户数据。

---

## 已裁决：不做浮盈切 10 日线

2026-10-22 裁决：浮盈 >15% 后 STOP 切 10 日线不做。

原因：

- 系统拿不到可靠浮盈信息。
- 不应使用信号触发价伪装真实持仓成本。
- 不扩表、不要求用户上报成本。

执行口径：

- STOP 仍按当前已实现的 MA5_tomorrow 滚动。
- 再入场后使用对应策略自带的 STOP，不根据浮盈切换周期。
- `profit_switch_n`、`profit_threshold_pct`、`receipt_min_coverage` 暂不参与运行逻辑。

---

## P2：统计闭环

2026-10-22 已完成。`pool-performance --date --window` 汇总池子五日表现、纠错命中率和未配对 T；`run_evening` 会读取历史质量并在 `Signal.premise` 标注 `ev_negative=true`。

### 现状

| 项 | 现状 |
|---|---|
| `signal_quality()` | 已有 EV_NEG 判断，但生成侧不用它 |
| `pool_quality()` | 有函数但没有真实五日表现输入 |
| `correction_hit_rate()` | 依赖外部字段 `correction_hit`，但没人计算 |
| `behavior()` | 覆盖率不足已处理，但 `restore_violations` 缺真实数据源 |

### EV_NEG

1. `run_evening()` 前先调用 `signal_quality()`。
2. 对 `ev_negative=true` 的 strategy_id：
   - 仍然生成信号，方便观察；
   - 在 `Signal.premise["ev_negative"] = true` 标注；
   - 报告中输出，不推送。

验收：

- 新信号可追溯 `ev_negative` 标注。
- 不自动删除或作废。

### 池子五日质量

新增 CLI：

```bash
python -m pointtable.cli pool-performance --date 2026-10-15 --window 5
```

对指定日期 pool 的每只票拉取 K 线，计算：

```text
five_day_return = close[t+5] / close[t] - 1
```

输出示例：

```json
[
  {
    "symbol": "300001",
    "base_close": 10.0,
    "future_close": 10.5,
    "five_day_return": 0.05
  }
]
```

把结果传给 `pool_quality()`。

验收：

- 不足 5 个交易日时输出 `measured: 0`，不硬算。
- 不使用账户盈亏。

### 纠错命中率

定义建议：

- STOP 触发后，5 个交易日内收盘价重新站上 `zone_high`，算市场纠错命中。
- 只统计纯行情，不看用户是否买回。

实现：

1. 从 journal 找 STOP 的 `TRIGGERED price/date`。
2. 从 K 线找之后 5 个交易日收盘。
3. 计算 `correction_hit`。
4. 输入 `correction_hit_rate()`。

### restore_violations

定义建议：

- 当日 T_BUY 已 executed，但收盘前没有配对 T_SELL executed，计一次。
- 如果没有回执，不计入违规，只计 `NO_RECEIPT`。

---

## P3：文档收口

2026-10-22 已完成。`fangan.md` 已写明报告生成、人工单向推送、无定时自动推送和无按钮回传。

当前 `fangan.md` 报告形态仍写“待定”，但 `notify/render.py` 已实现。

需要收口为当前报告结构，并写明：

- 报告由 `report` 命令生成。
- 可用 `push-file` 人工单向推送。
- 无定时自动推送。
- 无按钮回传。
- 报告只显示系统可测事实和建议点位。

---

## 建议实施顺序

| 顺序 | 任务 | 原因 |
|---|---|---|
| 1 | 池子按代码统一处理 | 消除 holding/candidate 分流影响 |
| 2 | watch 两级定性 | 补齐盘中判断 |
| 3 | T 复原 | 补齐 14:30 语义 |
| 4 | 统计闭环 | 样本足够后才有价值 |
| 5 | 文档收口 | 最后同步 |

---

## 已裁决清单

| 事项 | 裁决 |
|---|---|
| holding / candidate 分工 | 不做区分，只按股票代码处理池子 |
| 策略自动择优 | 首版不做；未来多策略命中则全部报出，不排序 |
| `volume_breakout` | 标准清晰；后续作为独立规则并行评估，不做优先级选择 |
| premise 破坏自动化 | 不做 |
| 浮盈 >15% 切 10 日线 | 不做；系统拿不到可靠浮盈信息 |
