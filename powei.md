

# 破位-反抽-再进场逻辑 · 局部修正指导文档（完整版）

## 背景

这条链路覆盖一只票从"持有"到"破位"到"重新进场"的完整循环，对应懂哥操作链：破位定性 → 等反抽判决 → 判决后按价格位置重新选策略进场。涉及文件：**volume.py、watch.py、settle.py、selector.py、generator.py、config.yaml**。不动状态机、不动表结构。

整条链的状态流转一句话：**STOP 触发（卖）→ retest_confirm 观察（判）→ 判决落地（撤或纠错）→ 价格走到下一档均线 → selector 前提重新检查（买）**。四个环节各管一段，互不越界。

## 问题

1. 定性四路只接了两路（漏 LIMIT_EXEMPT 和 DIVERGENCE）。
2. DELAY_OBSERVE 的"收盘定生死"没有执行者。
3. 观察哨窗口和量条件没有配置项。
4. 纠错提醒两条触发路径没统一。
5. **策略选择仍是 if-elif-else 耦合链，再入场 depth 被误用为策略参数。**
6. **反抽期间与新买点生成之间的边界没写死：什么时候策略管道对一只票关闭、什么时候重新开放。**

## 建议改法

### 一、破位定性（volume.py + watch.py）

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
# EXECUTE_NOW   → 立即推"破位，撤"
# DELAY_OBSERVE → 推"缩量破位，疑似被动，收盘定生死"
# DIVERGENCE    → 推"下跌但外盘占优，有承接，看不懂，收盘复核"
# LIMIT_EXEMPT  → 不定性，推送注明"封板附近失真"
# 定性只写 journal reason 和推送文案，不改状态机
```

### 二、收盘定生死（settle.py 新增）

```python
for stop in today_triggered_stops:
    q = stop.qualifier
    if q in ("DELAY_OBSERVE", "DIVERGENCE"):
        if close < stop.zone_low:
            create_retest_confirm(stop)      # 收盘仍破 → 立观察哨
        else:
            push_correction_notice(stop)     # 收盘收复 → 当日假破位，纠错提醒，不立哨
    elif q == "EXECUTE_NOW":
        create_retest_confirm(stop)          # 当天走了也立哨：给纠错留门
    # LIMIT_EXEMPT → 不立哨，次日 evening 正常生成新 STOP
```

### 三、观察哨：retest_confirm（settle.py 生成 + watch.py 判定）

生成规格：区间 = 原 STOP 的 zone（原支撑，现在是压力）；parent_id 指向原 enter（或 `holding:<symbol>`）；有效期 `config.retest_window_days`（默认 3 个交易日）。

watch 对观察哨的三种结局判定：

```python
# 结局一：摸到过不去 → 确认破位
if price_in_zone and close < zone_high:
    transition(retest, "hit")                # reason=retest_fail，推"反抽过不去，确认破位，撤"

# 结局二：放量站回 → 假破位
if close >= zone_high and volume_ratio >= cfg.retest_volume_min:   # 默认1.2
    transition(retest, "premise_broken")     # reason=retest_pass
    push_correction_notice(retest)           # "收复止损位，可按纪律纠错。追不追，是你。"

# 结局三：到期没摸回来 → 连反抽都没有
# settle 到期置 EXPIRED；"利润或纪律"二选一留给用户，系统不代答
```

纠错提醒统一走 `push_correction_notice()`：破位当日盘中收复（watch 发现）和跨日放量站回（结局二）是同一件事在两个时刻的触发，文案一致，都不迁移原 STOP 状态、都不发新买点。

### 四、策略解耦：selector.py（新增，替换原 if-elif-else）

每个策略是一个独立的前提检查纯函数，输入只有事实字段（均线、斜率、量比、突破价、首板标注等），不 import data/db/notify：

```python
def check_first_move_5d(facts) -> bool:      # 5日线上行 + 未进监管 + 榜不烂
def check_first_move_10d(facts) -> bool:     # 回调深但10日线方向未死
def check_first_move_drift(facts) -> bool:   # 5日线下移 + 尾盘突破明日5日线位置
def check_volume_breakout(facts) -> bool:    # 放量站上关键位 + 收阳
def check_first_board_dip(facts) -> bool:    # 首板(人工标注,仅标注日次日有效) + 有支撑

# generator 收集全部满足的策略，无优先级排序
candidates = [sid for sid, check in CHECKS.items() if check(facts)]
```

**同票不用去重，都推送消息**

### 五、两条链的咬合点：策略管道何时对一只票关闭、何时开放

这是解耦版最需要写死的一段——它替代了原来 depth 参数的全部职能：

```python
# generator 对每只 pool 票生成 BUY 前，按序过三道闸：

# 闸一：观察哨存续检查
# 该 symbol 有 ACTIVE 的 retest_confirm → 不产出任何 BUY
# 语义：破位判决未落地，反抽不是买点
if has_active_retest_confirm(symbol):
    skip_buy()

# 闸二：同档位冷却（防"同一根线上反复打脸"）
# journal 中同 symbol 同均线档位，近 config.same_ma_cooldown_days（默认2日）内
# 已有两次 STOP TRIGGERED → 该档位策略冷却
# 注意：冷却的是"档位"不是"票"——5日线冷却不影响10日线前提满足时出信号
if same_ma_stop_count(symbol, ma_n=5, days=cfg.same_ma_cooldown_days) >= 2:
    exclude(["first_move_5d", "first_move_drift"])   # 5日线家族冷却

# 闸三：判决后重新开放
# 观察哨已关闭（TRIGGERED/EXPIRED/INVALIDATED 任一终态）→ 管道重新开放
# 价格此时在哪根均线附近，哪个策略的前提自然满足，就出哪个策略
# "破5卖→10日再买"不是编排出来的，是价格位置的必然结果：
# 你在5日线止损出来，价格就处于"回调深"状态，check_first_move_10d 自然为真
```

用一张表确认三种典型路径下系统行为正确：

| 路径 | 闸一 | 闸二 | 结果 |
|---|---|---|---|
| 破5 → 反抽摸不过 → 确认破位 → 跌到10日线 | 观察哨关闭后开放 | 5日线档冷却中 | **first_move_10d 前提满足 → 新买点** ✓ |
| 破5 → 放量站回 → 人纠错买回 | 观察哨 INVALIDATED 后开放 | 无两次止损记录 | 管道开放，但当时价格在5日线上方远离均线，无前提满足 → **不出信号，等** ✓ |
| 破5 → 观察哨存续中价格横跳 | 存续中 | — | **零 BUY 输出** ✓（反抽不是买点） |

### 六、config.yaml 新增项

```yaml
retest_window_days: 3        # 观察哨有效期（交易日）
retest_volume_min: 1.2       # "放量站回"的量比下限
same_ma_cooldown_days: 2     # 同档位止损冷却窗口
dedup_zone_overlap: 0.7      # 同票多策略去重的 zone 重叠度阈值
```

## 验收

破位链：缩量破位收盘收复 → 纠错提醒无观察哨；收盘仍破 → 观察哨生成；封板日破位 → LIMIT_EXEMPT 不定性不立哨。

观察哨三结局：过不去 TRIGGERED / 放量站回 INVALIDATED＋纠错 / 到期 EXPIRED，各一条用例。

解耦：五策略各一条正向用例；事实不足输出 no_strategy；同票双策略重叠>70% 只留先手并标 merged_from。

咬合：观察哨存续期 selector 对该票零输出；观察哨关闭且价格到位后 first_move_10d 自动出现；同均线两次止损后5日线家族冷却、10日线不受影响。

全程无状态机非法迁移（test_lifecycle 既有用例全绿）。

