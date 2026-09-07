# 假说化改造说明（Falsifiability Refactor）

> 一笔交易在执行前应该能写成这样的句子：
> **"因为 X，所以在 Y 买入；如果 Z 出现，说明我错了，离场；如果 W 出现，兑现离场。"**
> X、Y、Z、W 四个位置缺一个，这笔交易就没有逻辑，只有冲动。

本文档说明六条改造规则在代码中的落点。改造只动**核心实盘链路**
（`analyzers / decision / orchestrator / feedback / db / push`），回测引擎 `loop/` 未改动
（共享的 `TimingEngine` 行为变化见第六节"兼容性说明"）。

---

## 一、从一句话假说开始：每笔交易必须是可证伪的

**落点**：`src/analyzers/hypothesis.py`（新）、`src/analyzers/signal_plan.py`

| 组件 | 说明 |
|---|---|
| `TradeHypothesis` | X/Y/Z/W 四要素 dataclass，`sentence()` 输出假说整句 |
| `validate_hypothesis()` | 出厂检查：X 非空 / Y>0 / Z>0 且 Z<Y / Z 宽度≥1.5×ATR / W 高点>Y |
| `build_execution_plan()` | **所有入场信号的唯一出口**：假说不完整 → `execute=False` 拒绝 |
| `signal_rejections` 表 | 拒绝留痕（不进调度不推送，但可审计） |

**沃尔德案例的拦截**（`tests/test_hypothesis_gate.py`）：

```
因为放量突破MA25，所以在93.88买入……
```

这句话写不下去：止损 93.94 高于买点 93.88，Z 位置填不出来。原系统对这种倒挂
仅降置信度照发（DB 里 9/4 那条 `RRR0.00`、`stop_loss=0` 的记录即铁证）；
新系统在 `build_execution_plan` 直接拒绝，原因是
`止损倒挂(Z>=Y): 认错价93.94高于买点93.88，假说自相矛盾`。

**倒挂的根源修复**：原实现止损锚定**信号时刻现价**（MA5×0.97），买点却锚定 **MA10 回踩档**，
两个锚点错位。新实现由 `calculate_paired_stop()` 按"X 的直接否定"重算 Z（见第四节）。

配置：`config/timing.yaml → hypothesis_gate`（`enabled` 总开关，关闭可回退旧行为对照）。

---

## 二、数据层：先有单一事实来源，再谈逻辑

**落点**：`src/analyzers/signal_plan.py → build_volume_snapshot()`

口径统一（全链路成交量只用一个单位"股"，接口层换算）已有
`_volume_share_factor` 反推机制；本次新增**两条一致性校验**作为硬拦截：

| 规则 | 判定 | 实战效果 |
|---|---|---|
| 规则1（原有，保留） | 量比<1 而 量能倍数>10 | 拦下 9/3 全部 4 条科创板误触发 |
| 规则2（新增） | 换手<10% 而成交量>10亿股 | 拦下 9/4 沃尔德（换手4.8%/量口径放大后>10亿股） |

命中任一规则 → `VolumeSnapshot.dirty=True` → `check_entry_signals` 在**生成阶段**返回空，
`entry_blocked_reason` 写入诊断，不进调度不推送。

配置：`config/timing.yaml → data_guard`（`turnover_threshold_pct` / `max_volume_shares`）。

---

## 三、信号层：把"状态"改造成"事件"

**落点**：`src/analyzers/signal_lifecycle.py`（新）、`timing_engine._check_volume_breakout`、
`unified_engine`、`db.py → signal_events 表`

"站上 MA25"是状态，今天为真、明天也为真 → 沃尔德连发两天买入信号。事件化后：

| 生命周期 | 实现 |
|---|---|
| **诞生** | 事件边界：昨收 ≤ 昨日MA25 且 今价站上今日MA25（`ma25_prev` 原本已算好但被弃用）；且当日收阳 + 量能确认（两条腿缺一不可） |
| **有效期** | `signal_events.expire_date = born + valid_days(默认5)`；N 日内回踩买点 Y 仍有效（观察卡显示事件状态） |
| **失效** | 收盘跌回突破位 / 板块状态机转退潮 → `evaluate_signal_events()` 置 invalidated 并推送**"信号作废——立即撤单"** |
| **过期** | 超期 → expired，推送"信号过期，作废不再重播" |
| **触发** | 买入回执 executed → 事件转 triggered，转入持仓配对出场跟踪 |
| **受众** | 买入事件只对空仓者成立；持仓者输出 **持有/加仓/减仓/止损** 四选一（`live_scheduler.schedule_live_signals(holdings=...)` 路由） |

**去重**：`check_entry_signals` 开头检查活跃事件（status='valid'），存在则跳过生成——
同日重复扫描、次日状态延续都不会重播。

配置：`volume_breakout.require_event_boundary / require_bullish_close`、
`signal_lifecycle.valid_days`。

---

## 四、买卖配对：每个策略在定义买入的那一刻就定义卖出

**落点**：`hypothesis.py → STRATEGY_EXIT_SPECS / calculate_paired_stop()`、
`timing_engine.check_exit_signals()`

| 策略 | 买入理由 X | 认错离场 Z（直接否定） | 兑现离场 W |
|---|---|---|---|
| 价量突破 | 放量站上关键位 | 收盘跌回突破位（MA25） | 下一阻力位分批，或 trailing 跟随（近5日低点×1.02） |
| 恐慌抄底 | 超跌+恐慌量能衰竭 | 反弹失败再创新低（恐慌低点下方） | 反弹至密集套牢区（近20日高）减仓 |
| 套利低吸 | 周线趋势中的低位结构 | 低吸结构破位（近5日低/MA10 下方） | 回到趋势通道上沿 |
| 确认追强 | 强势股趋势确认 | 跌破趋势线/MA20 | 动能耗尽（顶背驰+缩量）或目标位 |

配对三原则的实现：

1. **Z 必须是 X 的直接否定**：`Z = 结构位 - 缓冲`（价量突破的结构位=突破位 MA25，
   不再挪到 90.20 那种让 8.4% 破位被赦免的位置）。
2. **Z 的宽度由波动率决定**：`缓冲 = max(1.5×ATR, 结构位×0.5%)`——沃尔德那种
   0.3% 乃至负缓冲的档位在出厂检查即被拒。
3. **买卖敏感度对称**：W 是价位触发（触及 W 低沿→减半+trailing；触及 W 高沿→清仓），
   不再等 8~10% 外加四重投票；确认追强的"动能耗尽"走衰竭信号（顶背驰+缩量）。

**出场引擎接线**（`check_exit_signals`）：
- 持仓的入场假说来自**回执闭环**（`trade_logger.get_open_position()` 读 executed 买入行
  的 `paired_z / paired_w_low / paired_w_high`）；
- **C1 破位止损锚定到配对 Z**（`跌破配对止损Z=83.50（买入理由的直接否定）`）；
- **Block 6 策略兑现**：价格触 W → `策略兑现`（`source='paired'`，价位触发）；
- **配对优先**：已知持仓策略时，旧 MA5压制/冲高止盈/技术走弱/C3 全部降级
  `[辅助观察·非策略配对出场]`（urgency→观察）；破位止损作为系统安全网保持硬触发；
  持仓策略未知（无回执）时退回旧系统兜底逻辑，行为不变。

---

## 六、用记录闭环：让逻辑自己证明自己或杀死自己

**落点**：`db.py`（迁移）、`trade_logger.py`、`strategy_stats.py`（新）、
`engine.py`、`scripts/trade_feedback.py`

**四行日志**（`trade_logs` 新增 16 列）：

| 行 | 字段 | 写入时机 |
|---|---|---|
| 1. 假说原文 | `hypothesis_x/y/z/w/sentence, paired_z, paired_w_low/high, z_reference, event_id` | 推送时（修复：原实现 stop_loss/target_price 从未写入，47 行全为 0） |
| 2. 实际出入场 | `actual_price` + `exit_price, exit_date` | 买入/卖出回执 |
| 3. Z/W 是否触发 | `zw_triggered`（Z/W/系统） | 卖出回执自动按 exit_type 归类 |
| 4. 事后归因 | `review_outcome`（logic_right/luck/logic_wrong）+ `review_note` | 用户 `--outcome` 回执 |

**回执联动**（`trade_logger.update_action`）：
- 买入 executed 且带 event_id → 信号事件转 triggered；
- 卖出 executed → 自动回填开仓行 `exit_price / exit_date / pnl_pct / zw_triggered`
  （先锁定持仓再更新卖行，避免时序竞争）。

**分层统计与自动下线**（`strategy_stats.py`）：
- 30 笔起分层统计：胜率 / 均盈均亏 / 盈亏比 / 期望 / 归因分布；
- **作废条件**：滚动 50 笔期望值 ≤ 0（且胜率跌破盈亏平衡线 `1/(1+payoff)`）
  → 写 `strategy_status` 表置 offline → **调度器自动屏蔽该策略新买入** + 推送告警
  （`策略下线告警`）。已持仓的出场不受影响。样本不足 50 笔只报告不下线。

**回执 CLI 扩展**：

```bash
python scripts/trade_feedback.py --outcome <id> logic_right --note "突破有效回踩不破"
python scripts/trade_feedback.py --stats         # 分层统计（胜率/盈亏比/期望）
python scripts/trade_feedback.py --strategies    # 策略在线状态（含下线原因）
```

---

## 第二阶段：地基修正（零基本面 / 板块归属错乱 / 数据源缺陷）

> 用户实测三条批评：框架看所有股票都是一堆 K 线和资金流；板块归属歪了，
> 上层策略的精确性要打折扣；管道并不完备。

### A. 基本面闸门（`src/analyzers/fundamental_gate.py`）

六问（①方向②时机③量能④资金⑤拦截⑥风控）全部是技术面/资金流，零基本面
维度——发现不了汇成真空的业绩雷，也识别不出澜起“扣非+21%但净利+72%靠
投资收益”的盈利质量差异。现升级为**七问**（⑦基本面），且闸门参与出厂校验：

| 判定 | 触发条件（timing.yaml `fundamental_gate`） | 动作 |
|---|---|---|
| 业绩雷（veto） | 预告类型∈预亏/首亏/续亏/增亏/预减；或扣非/净利同比 < -30% | `execute=False` 出厂拒绝，留痕 signal_rejections |
| 盈利质量低（warn） | 净利同比 − 扣非同比 ≥ 30pp（非经驱动） | 置信度降一档 + 风险乘数 0.6 |
| 财报窗口（warn） | 距法定披露日 ≤ 7 天 | 降级标注（防披露跳空，不硬否决） |
| 数据缺失 | 业绩表/预告/扣非全部拉不到 | 放行（不产生假基本面结论） |

数据源：业绩快报/报表/预告全市场表（session 缓存）+ 扣非同口径同比；
报告期级数据强制带口径展示（“报告期2026-06-30”），防止把过去报告期的
变化当当下变化误读。买入理由 X 必须能与基本面共存：业绩在暴雷，
“放量突破”不构成买入理由。

### B. 主题归属修正（`src/analyzers/theme_attribution.py` + `config/theme_map.yaml`）

行业数据库自动映射把澜起/海光/大普微/中科飞测/芯碁微装/胜蓝/兆易创新
统统归入“电子化学品”，骄成超声归“电池”，创世纪归“自动化设备”——而
“主线/退潮/轮动”直接决定“禁追强/低吸可用”闸门输出。修正规则：

```
① config/theme_map.yaml overrides（人工映射，覆盖已知错配）
② 无映射 → 原行业链路兜底（行为不变）
状态：主题代理板块（THS 行业）取最严格 retreating > main_trend > rotational；
      代理不可得 → 沿用原状态。本层只修正归属，不发明状态。
```

自选池 23 只已按实盘主题归属全部入表（存储/半导体设备/算力硬件/先进封装/
光模块CPO/3C设备/消费电子/量子科技），修正记录透出 batch.theme_remaps
可审计。澜起不再被“电子化学品”的退潮状态误杀，改跟存储主线。

### C. 数据源技术性缺陷（`institutional_scorer.py`）

| 缺陷（用户铁证） | 修复 |
|---|---|
| 金海通 603061（沪市两融标的）被报“无融资余额数据（非两融标的或深市接口失败）” | 按日记录 SSE/SZSE 接口拉取成败；东财 datacenter RPTA_WEB_RZRQ_GGMX 兜底；官方表成功而无此券→「非两融标的」，接口失败→「两融接口失败（不代表非标的）」 |
| 股东户数“增加110%”是报告期级滞后数据 | 统计截止日距今 >90 天 → 不参与投票；展示强制带“报告期 YYYYMMDD”口径 |
| 主力净流出基于大单拆单算法，机构拆单即可规避 | 主力/股东票权降 0.5（两融/龙虎榜交易所披露口径保持 1.0），加权总分向零截断：单噪音源不再能翻动机构结论；推送④资金行带（权重0.5）标注 |

### 新增测试（40 用例）

| 文件 | 覆盖 |
|---|---|
| `tests/test_fundamental_gate.py` | 澜起盈利质量 warn / 汇成真空预亏 veto / 扣非阈值 / 财报窗口 / 快照多源合并与缓存 / 出厂拒绝流 / 置信度降档 |
| `tests/test_theme_attribution.py` | 七只错配股归属修正 / 创世纪·骄成超声改判 / 代理最严格状态 / 澜起脱误杀 / 批量重映射 / 引擎闸门接线 |
| `tests/test_data_source_fixes.py` | 金海通东财兜底 / 非标的 vs 接口失败 / 股东户数滞后不投票 / 噪音降权（单噪音源翻不动总分）/ 推送权重标注 / 七问基本面行 |

---

## 新增数据表

| 表 | 用途 |
|---|---|
| `signal_events` | 信号事件生命周期（born/expire/invalidated/triggered + 假说快照） |
| `signal_rejections` | 假说出厂拒绝留痕（缺 X/Y/Z/W、倒挂、缓冲不足） |
| `strategy_status` | 策略在线/下线状态（kill-switch 判定结果） |

迁移幂等：`python -m src.main init` 自动补列补表，旧数据全部保留
（已对带 47 行历史记录的真实 DB 验证）。

---

## 每日运行方式（不变）

```bash
python -m src.main run --phase intraday   # 盘中统一检查（推送 + 落库 + 生命周期）
python -m src.main run --phase post_market
python scripts/trade_feedback.py --list   # 回执
```

推送卡片新增"**可证伪假说**"块（X/Y/Z/W 四行 + 事件有效期），调度摘要新增
假说拒绝/策略下线统计与持仓者四选一建议区。

---

## 兼容性说明

1. **回测链路 `loop/` 未改动**。`TimingEngine` 为回测/实盘共享：假说门对回测中
   流经 `check_entry_signals` 的信号同样生效（只拒绝逻辑不成立的信号，属严格改进）；
   回测模式用内存事件存储、不读 DB、配对出场不启用（回测引擎自管出场）。
2. **v3 信号源**（`aggregator.py → stockagent_tuned_v3_signals`）不在主 intraday 链路上，
   本次未收编；后续如接入，需同样过 `build_execution_plan` 假说门。
3. `hypothesis_gate.enabled: false` 可整体关闭出厂检查回退旧行为（仅供对照排查）；
   `fundamental_gate.enabled: false` 同理可关闭基本面闸门；
   `theme_map.yaml enabled: false` 可关闭主题归属修正（回退纯行业链路）。
4. 旧行为测试已按新语义更新：`test_orchestrator_low_confidence`（低置信≠假说不完整，
   低置信仍可调度；`execute=False` 才被拦）。

## 测试

```bash
python -m pytest tests/ -q          # 182 passed / 2 skipped / 2 env-failed(原有，沙箱无 akshare)
```

新增测试套件（共 95+ 用例）：

| 文件 | 覆盖 |
|---|---|
| `tests/test_hypothesis_gate.py` | 沃尔德倒挂拦截 / 缺要素拒绝 / ATR 宽度 / 配对 Z 锚定 |
| `tests/test_data_consistency.py` | 9/3 量比冲突 / 9/4 换手-量级冲突 / 阈值覆盖 / 生成阶段拦截 |
| `tests/test_signal_lifecycle.py` | 事件边界 / 次日不重播 / 同日去重 / 失效撤单 / 过期 / 受众分流 |
| `tests/test_paired_exits.py` | 配对 Z 硬触发 / W 兑现分档 / 追强动能耗尽 / 旧门降观察 |
| `tests/test_strategy_kill_switch.py` | 分层统计 / 下线判定 / 调度过滤 / 四行日志 / 回执联动 / 拒绝留痕 |
| `tests/test_refactor_story.py` | 9/3→9/4 两日端到端闭环故事（诞生→不重播→回执→兑现→统计） |
| `tests/test_fundamental_gate.py` | 基本面闸门：澜起盈利质量 / 汇成真空业绩雷 / 财报窗口 / 出厂拒绝 |
| `tests/test_theme_attribution.py` | 主题归属修正：错配股改判 / 代理状态最严格 / 引擎闸门接线 |
| `tests/test_data_source_fixes.py` | 两融兜底与口径区分 / 股东户数滞后 / 主力噪音降权 / 七问渲染 |
