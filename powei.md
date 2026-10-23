
# 文档二：破位-反抽-再进场链 · 技术细节（v2.0）

> 本文聚焦一条链的实现细节，是文档一 §7 的展开。状态机层次以文档一 §6 为准：**链是 parent_id 接力，不是状态机；状态机只有单信号四状态。** 涉及文件：volume.py、watch.py、settle.py、selector.py、generator.py、config.yaml。

## 0. 背景与目标

覆盖一只票从持有到破位、到反抽观察、到重新进场的完整循环。一句话：**止损触发（卖）→ 反抽观察（判）→ 观察期结案（撤或纠错）→ 价格走到下一档均线 → 三个前置检查通过（买）**。

四个环节各管一段，互不越界：

| 环节 | 实现文件 | 职责 |
|---|---|---|
| 破位定性 | volume.py + watch.py | 四路输出，只写 journal reason 和推送文案 |
| 收盘定生死 | settle.py | 四路分支决定生不生成 retest_confirm |
| 反抽观察判定 | watch.py + settle.py | retest_confirm 三终态 |
| 判决后选股 | generator.py + selector.py | 三个前置检查，价格位置出策略 |

## 1. 破位当天定性（volume.py + watch.py）

```python
# volume.py —— 纯函数，四路输出
def break_qualifier(change_pct, drive, vgear, sector_triggered, index_triggered) -> str:
    if limit_exempt(change_pct, code, name):
        return "LIMIT_EXEMPT"      # 封板内外盘失真，不定性
    if 放量 and 主动:               # 主动=板块行和指数行近30分钟均无TRIGGERED且个股独跌
        return "EXECUTE_NOW"
    if 下跌 and drive > 0:          # 跌但外盘占优，有人在接
        return "DIVERGENCE"
    return "DELAY_OBSERVE"

# watch.py —— STOP 单点触碰即 TRIGGERED（不变），随后调用 break_qualifier
# 定性只写 journal reason 和推送文案，不改状态机
```

## 2. 收盘定生死（settle.py）

```python
for stop in today_triggered_stops:
    q = stop.status_reason
    if q in ("DELAY_OBSERVE", "DIVERGENCE"):
        if close < stop.zone_low:
            create_retest_confirm(stop)      # 收盘仍破 → 生成 retest_confirm
        else:
            push_correction_notice(stop)     # 收盘收复 → 纠错提醒，不生成
    elif q == "EXECUTE_NOW":
        create_retest_confirm(stop)          # 当天执行也生成：给纠错留门
    # LIMIT_EXEMPT → 不生成，次日晚上正常生成新 STOP
```

**四条硬约束**：EXECUTE_NOW 也生成；DELAY/DIVERGENCE 同一段逻辑双向；LIMIT_EXEMPT 不生成；生成时 zone 取原 STOP 的 zone（原支撑，现为压力），parent_id 指向原 enter 或 `holding:<symbol>`，有效期取 `retest_window_days`。

## 3. retest_confirm：生成、判定、终态

**生成规格**：zone = 原 STOP 的 zone；parent_id = 原 enter 或 `holding:<symbol>`；valid_policy 按 `retest_window_days`（默认3交易日）；tag = retest_confirm。

**三种终态各有结案时刻，不是固定 T+3**：

| 终态 | 判定 | 结案时刻 | 判定者 |
|---|---|---|---|
| TRIGGERED | 盘中摸进区间、收盘未站上区间上沿 | 盘中某次 watch 轮询（事件） | watch |
| INVALIDATED | 收盘站上区间上沿且量比 ≥ `retest_volume_min` | 盘中最后一次 watch 或收盘判定 | watch |
| EXPIRED | 窗口到期，价格从没摸进区间 | 到期日收盘后清点 | settle |

```python
# watch
if price_in_zone and close < zone_high:
    transition(retest, "hit")                # TRIGGERED，reason=retest_fail
if close >= zone_high and volume_ratio >= cfg.retest_volume_min:
    transition(retest, "premise_broken")     # INVALIDATED，reason=retest_pass
    push_correction_notice(retest)
# settle：到期置 EXPIRED
```

**观察期存续期间**：generator 检查一挡掉该票，候选区物理为空。**“当日结案”查法**：`status in 终态 且 closed_at 日期 = 报告日期`。

## 4. 纠错提醒统一

两条路径共用一个函数 `push_correction_notice()`：

- 破位当日盘中收复（watch 发现）→ 调用；
- retest_confirm 放量站回（watch 判 INVALIDATED）→ 调用。

**统一语义**：文案一致、不迁移原 STOP 状态、不发新买点、不生成新信号。买回是人的动作。

## 5. 策略选择解耦（selector.py）

每个策略是一个独立的前提检查纯函数，输入只有事实字段，不 import data/db/notify：

```python
def check_first_move_5d(facts) -> bool: ...
def check_first_move_10d(facts) -> bool: ...
def check_first_move_drift(facts) -> bool: ...
def check_volume_breakout(facts) -> bool: ...
def check_first_board_dip(facts) -> bool: ...

candidates = [sid for sid, check in CHECKS.items() if check(facts)]
```

**生成层不做任何合并**：一只票同时满足几个策略，就输出几条候选，全部推送。不按重叠度去重，不设优先级，不标 merged_from。`dedup_zone_overlap` 配置已删除。

## 6. 判决后重新选股：三个前置检查（替代 depth）

```python
# 检查一：观察期存续 → 不出买点
if has_active_retest_confirm(symbol):
    skip_buy()

# 检查二：同均线档位近 N 日两次 STOP → 该均线家族暂停
# 暂停的是“均线档位”不是“票”；5日线暂停不影响10日线
if same_ma_stop_count(symbol, ma_n=5, days=cfg.reentry_rules.same_ma_cooldown_days) >= 2:
    exclude(["first_move_5d", "first_move_drift"])

# 检查三：观察期结案 + 价格位置
# 结案（TRIGGERED/EXPIRED/INVALIDATED 任一终态）→ 选股恢复
# 价格在哪根均线附近，哪个策略前提自然满足就出哪个
# “破5卖→10日再买”是价格位置的必然结果，不是编排
```

三条典型路径：

| 路径 | 检查一 | 检查二 | 结果 |
|---|---|---|---|
| 破5 → 反抽摸不过 → 确认破位 → 跌到10日线 | 结案后放行 | 5日线档暂停中 | **10日线先手前提满足 → 新买点** ✓ |
| 破5 → 放量站回 → 人纠错买回 | 结案后放行 | 无两次止损记录 | 管道开放，但价格远离均线 → **不出信号，等** ✓ |
| 破5 → 观察期里价格横跳 | 存续中挡住 | — | **零买点输出** ✓ |

## 7. config.yaml 新增项

```yaml
retest_window_days: 3        # 观察哨有效期（交易日）
retest_volume_min: 1.2       # 放量站回量比下限（工程近似，C级）
reentry_rules:
  same_ma_cooldown_days: 2   # 同均线档位两次止损后冷却窗口
```

**已删除**：`dedup_zone_overlap`。

## 8. 废弃项

| 废弃项 | 说明 |
|---|---|
| depth 参数 | 不再用于策略选择或再入场深度控制；相关 config、签名、调用点清理 |
| `dedup_zone_overlap` | 生成层不合并 |
| “闸门”叫法 | 统一为“前置检查/条件” |

## 9. 验收

**破位链**：缩量收盘仍破→生成 retest_confirm；缩量收盘收复→只发纠错不生成；分歧收盘仍破→生成；分歧收盘收复→纠错不生成；EXECUTE_NOW 当天执行→仍生成；LIMIT_EXEMPT→不生成、次日正常生成 STOP。

**retest_confirm 三终态**：TRIGGERED / INVALIDATED＋纠错 / EXPIRED 各一条，且验证结案日按 `closed_at` 查。

**纠错统一**：两条路径文案一致、不迁移状态、不发买点。

**策略解耦**：五策略各一条正向；事实不足 no_strategy；同票多策略全部输出全部推送、无合并。

**三个前置检查**：观察期存续零输出；结案且价格到位 first_move_10d 出现；同均线两次止损后 5日线家族暂停、10日线不受影响；depth 删除后无残留调用。

**回归**：无状态机非法迁移，test_lifecycle 全绿。

---