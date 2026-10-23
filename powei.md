

# 文档二：破位-反抽-再进场链 · 技术细节（v3.0）

> 本文是文档一 §7 的展开。状态机层次以文档一 §6 为准：**链是 parent_id 接力，不是状态机；状态机只有单信号四状态。** 涉及文件：volume.py、watch.py、settle.py、selector.py、generator.py、config.yaml。

## 0. 背景与目标

覆盖一只票从破位、到反抽观察、到重新进场的完整循环。一句话：**止损触发（卖）→ 反抽观察（判）→ 观察期结案（撤或纠错）→ 价格走到下一档均线 → 三个前置检查通过（买）**。

| 环节 | 实现文件 | 职责 |
|---|---|---|
| 破位定性 | volume.py + watch.py | 四路输出，只写 journal reason 和推送文案；两级查询查 context_marks |
| 收盘定生死 | settle.py | 四路分支决定生不生成 retest_confirm |
| 反抽观察判定 | watch.py + settle.py | retest_confirm 三终态 |
| 判决后选股 | generator.py + selector.py | 三个前置检查，价格位置出策略 |

## 1. 破位当天定性（volume.py + watch.py）

```python
def break_qualifier(change_pct, drive, vgear, context_marks) -> str:
    if limit_exempt(change_pct, code, name):
        return "LIMIT_EXEMPT"
    # 两级查询：先板块 context_marks 近30分钟有无 triggered_at，再指数
    if 放量 and 主动:
        return "EXECUTE_NOW"
    if 下跌 and drive > 0:
        return "DIVERGENCE"
    return "DELAY_OBSERVE"
```

watch 对 STOP 单点触碰即 TRIGGERED，随后调用 break_qualifier。定性只写 journal 和推送文案，不改状态机。

## 2. 收盘定生死（settle.py）

```python
for stop in today_triggered_stops:
    q = stop.status_reason
    if q in ("DELAY_OBSERVE", "DIVERGENCE"):
        if close < stop.zone_low:
            create_retest_confirm(stop)
        else:
            push_correction_notice(stop)
    elif q == "EXECUTE_NOW":
        create_retest_confirm(stop)
    # LIMIT_EXEMPT → 不生成，次日晚上正常生成新 STOP
```

**四条硬约束**：EXECUTE_NOW 也生成；DELAY/DIVERGENCE 同一段逻辑双向；LIMIT_EXEMPT 不生成；生成时 zone 取原 STOP 的 zone，parent 指向原 enter 或 `holding:<symbol>`，valid_policy=fixed_window，window_days 从 config 快照进信号行。

## 3. retest_confirm 三终态

| 终态 | 判定 | 结案时刻 | 判定者 |
|---|---|---|---|
| TRIGGERED | 盘中摸进区间、收盘未站上区间上沿 | 盘中某次 watch（事件） | watch |
| INVALIDATED | 收盘站上区间上沿且量比 ≥ `retest_volume_min` | 盘中最后一次 watch 或收盘判定 | watch |
| EXPIRED | 窗口到期，价格从没摸进区间 | 到期日 15:05 | settle |

```python
# watch
if price_in_zone and close < zone_high:
    transition(retest, "hit", reason="retest_fail")
if close >= zone_high and volume_ratio >= cfg.retest_volume_min:
    transition(retest, "premise_broken", reason="retest_pass")
    push_correction_notice(retest)
# settle：到期置 EXPIRED，reason=eod
```

**观察期存续期间**：generator 检查一挡掉该票，候选区物理为空。**“当日结案”查 signals.closed_at，不查 journal。**

## 4. 纠错提醒统一

`push_correction_notice()`：破位当日盘中收复（watch）和 retest_confirm 放量站回（watch 判 INVALIDATED）共用。文案一致、不迁移原 STOP 状态、不发新买点、不生成新信号。

## 5. 策略选择解耦（selector.py）

五策略独立纯函数，不 import data/db/notify：

```python
candidates = [sid for sid, check in CHECKS.items() if check(facts)]
```

**生成层不做任何合并**：同票多策略全部输出全部推送，不按重叠度去重，不设优先级。`dedup_zone_overlap` 已删除。

## 6. 判决后重新选股：三个前置检查（替代 depth）

```python
# 检查一：观察期存续 → 不出买点
if has_active_retest_confirm(symbol):
    skip_buy()

# 检查二：同均线档位近 N 日两次 STOP → 该均线家族暂停
if same_ma_stop_count(symbol, ma_n=5, days=cfg.reentry_rules.same_ma_cooldown_days) >= 2:
    exclude(["first_move_5d", "first_move_drift"])

# 检查三：观察期结案 + 价格位置
# 结案 → 选股恢复；价格在哪根均线附近，哪个策略前提自然满足就出哪个
```

“破5卖→10日再买”是价格位置的必然结果，不是编排。

| 路径 | 检查一 | 检查二 | 结果 |
|---|---|---|---|
| 破5 → 反抽摸不过 → 确认破位 → 跌到10日线 | 结案后放行 | 5日线档暂停中 | **10日线先手前提满足 → 新买点** ✓ |
| 破5 → 放量站回 → 人纠错买回 | 结案后放行 | 无两次止损记录 | 价格远离均线 → **不出信号，等** ✓ |
| 破5 → 观察期里价格横跳 | 存续中挡住 | — | **零买点输出** ✓ |

## 7. context_marks 与两级查询

**表结构**：date / context_type（index/sector）/ code / level_low / level_high / label / triggered_at / created_at。

**生命周期**：evening 每天生成 ACTIVE；watch 盘中检测进入区间写 triggered_at；次日 evening 覆盖或置 INACTIVE。**不接 lifecycle.py，不产生 journal 迁移记录，不进 stats，不进报告买卖区。**

**两级查询**：break_qualifier 先查板块 context_marks 近 30 分钟有无 triggered_at，再查指数。这是“主动/被动”判定的数据源。

## 8. config

```yaml
retest_window_days: 3        # 观察哨有效期（交易日）
retest_volume_min: 1.2       # 放量站回量比下限（工程近似，C级）
reentry_rules:
  same_ma_cooldown_days: 2   # 同均线档位两次止损后冷却窗口
```

**已删除**：`dedup_zone_overlap`、`min_confirm_duration`。

## 9. 废弃项

| 废弃项 | 说明 |
|---|---|
| depth 参数 | 选股由三个前置检查＋价格位置决定 |
| `dedup_zone_overlap` | 生成层不合并 |
| “闸门”叫法 | 统一为“前置检查” |
| **双周期确认** | 单点触碰即触发；防毛刺由 guard 承担 |
| **`journal.confirmed`** | touched_only 概念消失 |

## 10. 验收

**破位链**：缩量收盘仍破→生成；缩量收盘收复→只发纠错；分歧收盘仍破→生成；分歧收盘收复→纠错不生成；EXECUTE_NOW→仍生成；LIMIT_EXEMPT→不生成、次日正常生成 STOP。

**retest_confirm 三终态**：TRIGGERED / INVALIDATED＋纠错 / EXPIRED 各一条，验证结案日按 signals.closed_at 查。

**纠错统一**：两条路径文案一致、不迁移状态、不发买点。

**策略解耦**：五策略各一条正向；no_strategy；同票多策略全部输出全部推送、无合并。

**三个前置检查**：观察期存续零输出；结案且价格到位 first_move_10d 出现；同均线两次止损后 5日线家族暂停、10日线不受影响；depth 删除后无残留调用。

**双周期删除**：同一条信号跑多次 watch 只推送一次；脏报价被 guard 拦截不触发；日 K 线不可达但盘中触发 → 标 unreachable。

**回归**：无状态机非法迁移，test_lifecycle 全绿。

---

