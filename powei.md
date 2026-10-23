

# 文档二：破位-反抽-再进场链 · 技术细节（v3.1）

> 本文是文档一 §7 的展开。状态机层次以文档一 §6 为准。涉及文件：volume.py、watch.py、settle.py、selector.py、generator.py、config.yaml。

## 0. 背景与目标

**止损触发（卖）→ 反抽观察（判）→ 观察期结案（撤或纠错）→ 价格走到下一档均线 → 三个前置检查通过（买）**。

| 环节 | 实现文件 | 职责 |
|---|---|---|
| 破位定性 | volume.py + watch.py | 四路输出**写入 signals.break_qualifier**；两级查询查 context_marks |
| 收盘定生死 | settle.py | 读 break_qualifier，四路分支决定生不生成 retest_confirm |
| 反抽观察判定 | watch.py + settle.py | retest_confirm 三终态 |
| 判决后选股 | generator.py + selector.py | 三个前置检查；同 symbol 同时刻一条 ACTIVE stop |

## 1. 破位当天定性

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

watch 对 STOP 单点触碰即 TRIGGERED，随后调用 break_qualifier，**写入 signals.break_qualifier，并记 journal record_type=qualifier**。

**熔断日特殊处理**：DELAY_OBSERVE / DIVERGENCE 当日直接按 EXECUTE_NOW 处理。

## 2. 收盘定生死（settle.py）

```python
for stop in today_triggered_stops:
    q = stop.break_qualifier          # 读 break_qualifier，不读 status_reason
    if q in ("DELAY_OBSERVE", "DIVERGENCE"):
        if close < stop.zone_low:
            create_retest_confirm(stop)
        else:
            push_correction_notice(stop)
    elif q == "EXECUTE_NOW":
        create_retest_confirm(stop)
    # LIMIT_EXEMPT → 不生成，次日晚上正常生成新 STOP
```

**四条硬约束**：EXECUTE_NOW 也生成；DELAY/DIVERGENCE 同一段双向；LIMIT_EXEMPT 不生成；生成时 zone 取原 STOP 的 zone，parent 指向原 enter 或 `pool:<symbol>`，valid_policy=fixed_window，window_days 从 config 快照。

## 3. retest_confirm 三终态

| 终态 | 判定 | 结案时刻 | 判定者 |
|---|---|---|---|
| TRIGGERED | 盘中摸进区间、收盘未站上区间上沿 | 盘中某次 watch | watch |
| INVALIDATED | 收盘站上区间上沿且量比 ≥ `retest_volume_min` | 盘中最后一次 watch 或收盘判定 | watch |
| EXPIRED | 窗口到期，价格从没摸进区间 | 到期日 15:05 | settle |

**观察期存续期间**：generator 检查一挡掉该票，候选区物理为空。**“当日结案”查 signals.closed_at，不查 journal。**

## 4. 纠错提醒统一

`push_correction_notice()`：破位当日盘中收复（watch）和 retest_confirm 放量站回（watch 判 INVALIDATED）共用。文案一致、不迁移原 STOP 状态、不发新买点。

## 5. 策略选择解耦（selector.py）

五策略独立纯函数，不 import data/db/notify：

```python
candidates = [sid for sid, check in CHECKS.items() if check(facts)]
```

生成层不做任何合并：同票多策略全部输出全部推送。`dedup_zone_overlap` 已删除。

## 6. 判决后重新选股：三个前置检查

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

## 7. entry 时间窗与触发（新增）

**ma_tomorrow 系 entry 的触发窗口 = 14:30–15:00**（tail_snapshot 时刻到收盘）：

```python
def watch_entry(entry, price, now):
    # 窗口检查
    if entry.trigger_window_start is not None:
        if not (entry.trigger_window_start <= now.time() <= entry.trigger_window_end):
            return  # 窗口外忽略，不记 touched_only，不推送
    # 窗口内单点触碰即触发
    if entry.zone_low <= price <= entry.zone_high:
        transition(entry, "hit", reason="hit")
```

**first_board_dip / volume_breakout 的 entry 无 trigger_window，全天有效。**

## 8. generator 的三条额外规则（A4 / A5）

**A4 同 symbol 同时刻只有一条 ACTIVE stop**：

```python
def ensure_stop_for_enter(enter):
    existing = find_active_stop(enter.symbol)
    if existing:
        # 不新建，把 enter 记为该 stop 的关联锚点
        existing.parent_id = enter.id
        # 不记 zone_update；zone 由池内票每晚规则维护
    else:
        create_stop_with_zone_from_Z(enter)   # Z/W 同生
```

**A5 观察哨存续期暂停 stop/profit/t 生成**：

```python
def generate_holding_rows(symbol):
    if has_active_retest_confirm(symbol):
        return []       # 观察期存续，保护语义由 retest_confirm 承担
    if in_post_judgment_gap(symbol):   # 判决后至新 enter 前
        return []       # 价格未到下一档均线，没有保护对象
    return build_stop_t_profit(symbol)
```

## 9. context_marks 与两级查询

**表结构**：date / context_type / code / level_low / level_high / label / triggered_at / created_at。

**triggered_at 更新规则**：**每次进入区间都更新为最近时间戳**，不保留首次。两级查询查“近 30 分钟有无 triggered_at 且时间戳在 30 分钟内”。

**生命周期**：evening 每天生成 ACTIVE；watch 盘中更新 triggered_at；次日 evening 覆盖或置 INACTIVE。**不接 lifecycle.py，不产生 journal 迁移记录，不进 stats。**

## 10. config

```yaml
retest_window_days: 3
retest_volume_min: 1.2
reentry_rules:
  same_ma_cooldown_days: 2
stats:
  entry_reachability_mode: tail_approx
  tail_approx_tolerance: 0.003
guard:
  two_source_deviation_max: 0.005
```

**已删除**：`dedup_zone_overlap`、`min_confirm_duration`。

## 11. 废弃项

| 废弃项 | 说明 |
|---|---|
| depth 参数 | 选股由三个前置检查＋价格位置决定 |
| `dedup_zone_overlap` | 生成层不合并 |
| “闸门”叫法 | 统一为“前置检查” |
| 双周期确认 | 单点触碰即触发；防毛刺由 guard 承担 |
| `journal.confirmed` | touched_only 概念消失 |
| **`holding:<symbol>` 前缀** | **改 `pool:<symbol>`** |
| **“熔断日 STOP 收紧为破位即 SELL”** | **改写为“熔断日破位定性取消延迟判决”** |
| **activation 对 entry“仅文案”** | **对 entry 升级为真实触发约束（trigger_window）** |

## 12. 验收

**破位链**：缩量收盘仍破→生成；缩量收盘收复→只发纠错；分歧收盘仍破→生成；分歧收盘收复→纠错不生成；EXECUTE_NOW→仍生成；LIMIT_EXEMPT→不生成；**熔断日 DELAY/DIVERGENCE 视同 EXECUTE_NOW**。

**break_qualifier**：写入 signals + journal record_type=qualifier；settle 能读到。

**retest_confirm 三终态**：各一条，验证结案日按 signals.closed_at 查。

**纠错统一**：两条路径文案一致。

**策略解耦**：五策略各一条正向；no_strategy；同票多策略全部输出全部推送。

**三个前置检查**：观察期存续零输出；结案且价格到位 first_move_10d 出现；同均线两次止损后 5日线家族暂停、10日线不受影响。

**entry 时间窗**：ma_tomorrow 系 14:30 前触碰被忽略、不记 touched_only；first_board_dip / volume_breakout 全天触发不受限。

**A4**：同 symbol 同时刻只有一条 ACTIVE stop；Z/W 同生时若有 ACTIVE stop 不新建、只关联。

**A5**：观察哨存续期不生成 stop/profit/t；判决后至新 enter 前也暂停。

**双周期删除**：同一条信号跑多次 watch 只推送一次；脏报价被 guard 拦截。

**回归**：无状态机非法迁移。

---

