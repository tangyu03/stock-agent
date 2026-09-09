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

## 第三阶段：估值透镜（机构标签失真 / 估值维度缺失 / 亏损分型粗糙 / 技术基本面脱节）

**用户铁证（Pushplus 报告 8 大缺陷 → 3 个方向性误判）**：报告只罗列"净利同比"一个数字，
估值、盈利体量、订单证据、研报共识全部不在系统考量内——

| 误判案例（真实数据） | 原框架行为 | Phase3 行为 |
|---|---|---|
| 兆易创新 603986：PE(TTM) 33.9 倍 / 净利 68.57 亿 / +1091.5% / 研报一致买入 | 被 4 票资金流投票标"机构看空(-2票)" | 标签语义修正（"资金看X"）+ `value_growth` 正向证据 + `analyst_flow_conflict` 冲突双标签 |
| 长光华芯 688048：PE 1155.5 倍 / 净利 +238% 但仅 3034 万 | "+238%" 高增长无任何拦截 | `low_base_reversal` + `valuation_bubble`【veto】出厂拒绝 |
| 中科飞测 688361：亏损但合同负债 +66.3% / 存货 +26% | 与芯原同贴"机构偏空" | `strategic_loss`【warn】订单加速的战略性亏损，披露不否决 |
| 芯原股份 688521：亏 6.12 亿无订单证据 | 同上 | `model_loss`：追高型策略 veto / 低吸型 warn+0.5 乘数 |
| 中际旭创 300308：净利 136.5 亿 + PE 合理 + 订单排至 2027 | 防守模式"禁追强"静默拦截 | 闸门不放开（纪律优先）但 **⚠基本面冲突显式披露**，踏空风险可复盘 |

### A. 估值透镜（`src/analyzers/valuation_lens.py`，与 fundamental_gate 互补）

后者管"业绩在不在暴雷"；透镜管"**增长是真的吗 / 估值配得上吗 / 亏损是什么性质 /
资金票与研报共识是否反向**"：

1. **估值分档**：PE(TTM)≥300 且净利<2亿（低基数）→ veto；PE≥300 但体量大 → warn+0.5；
   PE 60~300 → 仅标注"估值偏高"（科技股常态，宽进严出防误伤）；
   PE<60 且同比≥50% 且净利≥5亿 → `value_growth`（正向证据只展示，不加分防吹票）。
2. **低基数反转**：同比≥100% 且净利<2亿 → 增速数字不构成成长证据，报告双口径。
3. **亏损分型**：合同负债较年初≥30% / 存货≥20% → `strategic_loss`（中科飞测）；
   无订单证据 → `model_loss`：追高型（确认追强/价量突破）veto，低吸型 warn。
4. **资金-分析师冲突**：资金票≤-1 而研报共识看多（买入+增持≥60% 或≥3家）→
   冲突标记双标签呈现（兆易案例）。**分析师共识不混入资金票计分**——资金是节奏、
   研报是方向，混票会把冲突平均掉，这正是"机构看空"误导的根源。

### B. 标签语义修正（`institutional_scorer.py`）

4 票源（主力/股东/两融/龙虎榜）全是短周期资金流/筹码数据，无一测度研报共识——
"机构看多/看空"是语义挪用。改为"资金看X" + `label_scope` 口径声明 +
`analyst_consensus` 旁路注入（渲染双标签，冲突显式标记）。

### C. 追强闸门冲突披露（`unified_engine._strategy_blockers`）

防守/撤退模式拦下追强但个股基本面强（value_growth / strategic_loss / 研报看多）→
闸门**不因个股放开**（防守纪律优先，否则模式闸门名存实亡），但 blocker 文案
追加 ⚠基本面冲突警示——报告看得见踏空风险，模式判断错了可被复盘。

### D. 数据源（全部优雅降级 + session 缓存）

- `ak.stock_value_em` PE-TTM/PB/总市值；兜底 `ak.stock_zh_a_spot_em`（全市场表
  会话级共享缓存，口径标注"动态,非TTM"）
- 业绩快报"净利润"列（复用 fundamental_gate 已缓存表，**零额外调用**）→ 净利绝对值
- `ak.stock_balance_sheet_by_report_em` 合同负债/存货较年初
- `ak.stock_research_report_em` 近 90 天研报评级计数

### E. 接线

- `signal_plan.build_execution_plan`：透镜按 entry_type 重新评估（追高型更严格），
  veto → execute=False + 留痕；warn → 风险乘数 + 置信度降档
- `timing_engine`：`_fetch_tech_data` 拉透镜入 `tech_data["valuation_lens"]`；
  EntrySignal 增 `valuation_note`；出厂拒绝条件扩为 假说∨业绩雷∨透镜
- `engine.py`：买卡/观察卡透传 `valuation_lens`；拒绝留痕落库存档
- 推送⑦基本面行：净利双口径（增速+绝对值）+ PE/PB + 估值分档/亏损分型/冲突标签

### 新增测试（34 用例，`tests/test_valuation_lens.py`）

五案例全锚定：兆易 value_growth+冲突 / 长光华芯 bubble veto / 中科飞测战略亏损 /
芯原模式亏损双策略分型 / 中际旭创闸门冲突披露；出厂拒绝流、风险乘数、
渲染双口径、单位归一化（元→亿）、优雅降级、阈值可配、标签语义修正、
共识标签规则、旁路不混票。


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
   `theme_map.yaml enabled: false` 可关闭主题归属修正（回退纯行业链路）；
   `valuation_lens.enabled: false` 可整体关闭估值透镜（回退 Phase2 行为）。
4. 旧行为测试已按新语义更新：`test_orchestrator_low_confidence`（低置信≠假说不完整，
   低置信仍可调度；`execute=False` 才被拦）；`test_signal_plan`/
   `test_data_source_fixes`/`test_institutional_fund_flow` 标签断言随
   "机构看X→资金看X"语义修正同步更新。

## 测试

```bash
python -m pytest tests/ -q          # 216 passed / 2 skipped / 2 failed(基线遗留，见注)
```

> 注：沙箱无 akshare。`test_institutional_fund_flow.py` 顶部硬依赖 akshare
> 需 `--ignore` 隔离（基线即如此）；`test_metrics_enrichment.py` 的 2 个失败
> 在 Phase2 原始代码上同样复现（已用打包件对照验证，属环境依赖遗留，与
> 本次改造无关）。

新增测试套件（共 130+ 用例）：

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
| `tests/test_valuation_lens.py` | 估值透镜：兆易冲突双标签 / 长光华芯泡沫 veto / 中科飞测战略亏损 / 芯原模式亏损分型 / 中际旭创闸门冲突披露 / 出厂拒绝流 / 单位归一化 / 优雅降级 |

---

# 第四阶段：决策记录落地（P0/P1 全量 + P2/P3 简表）

本阶段把《决策记录（12 条带审计链的改造建议）》逐条落进实现。每条建议
自带依据 / 反方 / 作废条件 / 验证四要素——**每个数字有出处，每条规则
有反方，每个建议有死亡条件**，作废条件的判定数据全部留档可查。

## P0（最高优先级）

### P0-1 量能外推口径（`src/analyzers/volume_projection.py`）

依据：蘅东光 9/7 的 1.02x 拦截——盘中累计量对比全天均量，午前结构性
小于 1 是数学必然（分子只走了半天），外推口径下同一数据为 2.0x。

- 外推全天量 = 累计量 ÷ U 型分位（默认表：午前 52%，可整体覆盖）；
- 反方对冲已内置：10:00 前禁用（开盘冲量高估）、封板禁用（按代码的
  涨跌停幅度，北交所 30%）、14:45 后退化累计量、回测/日频禁用；
- 触发口径：实际 >1.0 或 外推 ≥1.2（外推阈值更严，防假放量）任一成立；
- 误差记录表 `volume_projection_log`：盘中记录外推快照，收盘回填实际
  全天量（`daily_review` 自动执行），`projection_error_report()` 判定
  作废条件（10:30 后误差持续 >15% → 换标的池分位数表，框架保留参数重校）；
- CLI：`python scripts/trade_feedback.py --projection-errors`。

### P0-2 Z 线统一（`hypothesis.py` + `signal_plan.py`）

依据：博杰 9/7 止损线 85.54 与当日跌停价 85.53 吻合到分——用制度边界
充当结构破位判定，污染 RRR/置信度/仓位三层输出。

- `z_line_mode: bare_structure`（默认）：执行止损 = 假说结构位本体
  （博杰重算：Z=99.39，RRR 0.59→2.99≈3.0，置信度 3/6→4/6）；
- `z_line_mode: atr_buffer`：回退族（结构位−1.5×ATR，Phase1 行为保留）；
- Z 宽度检查仅在 atr_buffer 模式生效（裸结构位的宽度由市场结构决定）；
- RRR 质量票：RRR≥2.5 → 置信度 +1（出处：隐含胜率 28.6%，非惯例）；
- 落库假说携带 `z_line_mode`，`z_mode_comparison()` 按模式分层对照
  期望值（作废条件的判定材料，30 笔起步）；
- CLI：`python scripts/trade_feedback.py --zmode`。

### P0-3 卖出条件分级 OR（`timing_engine.check_exit_signals`）

依据：两天 30+ 次卖出检查零触发，汇成真空 -6% 无响应——AND 门在
数学上近乎永不开启。

- 止损类（价格破 Z 线、技术走弱 medium≥2）任一即出，不容商量；
- 止盈类（冲高止盈 strong≥2、MA5 压制三条件 AND）保持原有计票——
  防止 OR 化从"永不触发"摆到"频繁误杀"的另一极；
- `exit.graded_or.enabled: false` 可回退旧计票；
- `graded_exit_sensitivity()` 统计周触发（作废条件：>5 次且过半 3 日内
  回本 → 回调结构位距离参数，而非回退 AND）；
- CLI：`python scripts/trade_feedback.py --sensitivity`。

## P1（高优先级）

### P1-1 防守模式确认追强降仓可用（`timing_engine._defensive_chase_gates`）

依据：蘅东光 9/7 +14.81%/量比 2.01/ADX 48 却零提示——防守的定义是
压缩敞口而非禁用策略。

- 三重门全过才放行：门一四确认+时机（创新高/量比/量能外推/ADX/
  外盘主动/RSI 未过热），门二基本面（净利同比≥30% 且非业绩雷/
  低基数/模式亏损/筹码分散），门三板块联动（主线）；
- 试探仓比例 = 单笔风险预算 1% ÷ 止损距离（`compute_risk_budget_position`）：
  中波动 ≈29%≈1/3，蘅东光类高波动（止损距离~10%）≈10%，封顶 1/3，
  不足 1 手按 1 手地板（比例语义保留在 `risk_budget_note`）；
- 9/3 形态（RSI 过热+户数分散）被门一/门二拦截；
- 踏空成本台账：防守模式未放行的创新高+放量标的入 `chase_missed`
  推送留档（回退禁用后继续记录——这份数据是防守模式是否值得存在的证据）；
- 统计分层键 `确认追强@防守`（30 笔滚动胜率<30% 且期望<0 → 回退禁用）。

### P1-2 再入场循环（`signal_lifecycle.py`）

依据：蘅东光 9/4 止损 -5%、9/7 +14.8%，缺的只是第二次入场——止损
是尝试的结束，不是死刑判决。

- 待命判定 `reentry_status()`：新事件（创新高 / 收复 Z 线）→ 待命
  队列头部（观察卡置顶 + 独立推送）；显式声明"不携带沉没成本逻辑，
  待完整触发条件重新确认"；
- 次数上限 `max_reentries: 2`：用尽后转入长期观察，新事件不再重播
  （拒绝留痕"再入场上限"）；expired（未入场形态）不计次数；
- 作废条件数据：再入场样本与首入场样本分层对照（strategy_stats）。

### P1-3 昨日事件追踪表（`src/feedback/event_tracker.py`）

依据：9/3 六条信号 9/4 全灭，系统没有任何回头看的行为——没有记忆的
系统无法校准任何参数。

- 收盘版（`daily_review`）顶部：近 2 日信号事件 + 闭合交易表
  （状态：待回踩/已入场/失效撤单/过期作废/了结；了结带浮盈浮亏%与
  入场→离场链路）；
- 验收标准内置：每月一次按表校准参数（拒绝无数据的调参）；
- 本条是其他条目的基础设施，无作废条件。

## P2 / P3（简表，同样带审计链）

| 任务 | 实现 | 作废条件的数据基础设施 |
|---|---|---|
| P2 板块版本化 | `theme_map.yaml` 版本戳 + 假说携带 `sector_version` + `sector_version_breakdown()` | `--sector-versions` |
| P2 资金拆层 | 快源（两融/龙虎榜）投票，慢源（主力/股东）拆层单独计票（`fund_layering` + 渲染独立行） | 拆层开关 `set_fund_layering` |
| P2 组合预算 | 同板块并发敞口上限 3（`portfolio_budget.py`，超额降级观察并留痕） | `portfolio_budget.enabled` |
| P3 趋势延续 | 第五策略：突破后 1~10 日延续段回踩不破+再放量（`_check_trend_continuation`，Z=MA10） | `trend_continuation.enabled` |
| P3 第八问驱动源 | 业绩/板块/资金/价格四分类（`driver_attribution.py`，买卖卡 ⑧行 + 假说留档） | `driver_attribution` 阈值 |
| P3 参数附录 | 收盘版底部参数出处+验证状态（`param_appendix.py`：RRR 隐含胜率/Z 线模式/外推误差/分级OR敏感度/风险预算） | 附录机制永不作废 |

## 配置与回退

全部新增行为集中在 `config/timing.yaml`，每块都有 `enabled` 开关：

```yaml
hypothesis_gate.z_line_mode: bare_structure   # P0-2（atr_buffer 回退族）
volume_projection.enabled: true               # P0-1
exit.graded_or.enabled: true                  # P0-3
defensive_chase.enabled: true                 # P1-1（false = 回退禁追强，踏空留档）
reentry.enabled: true                         # P1-2
trend_continuation.enabled: true              # P3-1
portfolio_budget.enabled: true                # P2-3
confidence.rrr_quality_threshold: 2.5         # P0-2 RRR 质量票
```

回测链路（loop/）不受影响：回测模式下量能外推禁用（日频口径即全天量），
其余闸门走同一份配置。

## 测试（Phase4 新增 7 套件 90 用例）

```bash
python -m pytest tests/ -q --ignore=tests/test_institutional_fund_flow.py
# 306 passed / 2 skipped（akshare 环境依赖隔离同前）
```

| 文件 | 锚定的验证项 |
|---|---|
| `tests/test_volume_projection.py` | 蘅东光 1.02x@11:27→2.0x / 沃尔德 0.92x→1.8x 触发 / 10:00前·封板·14:45后禁用 / 误差记录表与作废条件 / 回测禁用 |
| `tests/test_z_line_unification.py` | 博杰 Z=99.39 / RRR 0.59→2.99 / 置信度 3/6→4/6 / 旧模式复现 85.54 病灶 / 双模式对照统计 |
| `tests/test_graded_exit_or.py` | 汇成真空 -6% 破位止损 / 技术走弱 medium2 分级OR / 冲高止盈 strong1/2 继续持有 / MA5压制三条件保留 / 周触发过敏感统计 |
| `tests/test_defensive_chase.py` | 蘅东光 9/7 三重门全过+降仓 / 9/3 RSI过热+户数分散拦截 / 风险预算 29%·10%·封顶1/3 / 调度器地板100股 / 拦截披露文案 |
| `tests/test_reentry_cycle.py` | 创新高待命头部 / 收复Z线 / 次数上限2 / expired不计 / 沉没成本显式免责 |
| `tests/test_event_tracker.py` | 9/3→9/4 六条信号闭合记录(了结,浮亏-3~-8%) / 收盘版顶部表格 / 生命周期行去重 |
| `tests/test_phase4_p2p3.py` | 主题版本戳 / 拆层快慢计票 / 半导体设备8只预算拦截 / 沃尔德趋势延续触发 / 博杰业绩+板块双标签 / 参数附录全覆盖 |

旧断言更新（行为变更随决策记录语义）：`test_hypothesis_gate`
（裸结构位模式宽度检查仅 atr_buffer 生效）、`test_signal_plan`
（RRR 质量票 5→6）、`test_observation_reasons`/`test_valuation_lens`
（防守追强三重门文案）、`test_data_source_fixes`（拆层后慢源不再投票）。

---

# 第五阶段：验收回炉（9/8 盘前报告四个新问题）

依据：2026-09-08 08:55 盘前报告验收（蘅东光/博杰/精智达三案例）——
12 项改动落地 8 项、方向全部正确，但修复引入了四个新问题，全部在本阶段闭环。

## 问题 1：RRR 天文数字以"合法形态"回归（Z 线统一漏了缓冲层）

精智达裸结构位 476.75 距买点 480.77 仅 0.84%，日振幅 8.16% 的十分之一
即可扫损；RRR=12.30 还给置信度 +1（坏数据给好评）。决策记录 P0-2 原文
本写"结构位**或**结构位加 1~2 倍 ATR 缓冲"，Phase4 只做了前半句。

修复（`hypothesis.py`）：
- 新默认 `z_line_mode: buffered_structure`——Z = 结构位 − clamp(k×ATR)：
  - k 自适应：ATR/结构位 ≥5%（高波动）用 1.5，<5%（低波动）用 2.0；
  - 下限：max(0.5%, 毫厘噪声)（防 0.84% 类毫厘止损）；
  - 上限：结构位 ×8%（防 1.5×ATR 拖到跌停价——博杰 9/7 旧病：
    13.85 缓冲恰好落在 85.54=当日跌停价；现在被压到 7.95，Z=91.44）；
  - 宽度检查与缓冲上限自洽（min(1.5×ATR, 8%×Y))，高波动标的不再被
    自己的 cap 与宽度检查夹击拒绝。
- `bare_structure` 降为对照族，保留防噪下限（止损距离<0.8% 或 <0.3×ATR
  拒绝出厂）；`atr_buffer` 为 Phase1 回退族，三族同台由
  `z_mode_comparison` 分层统计（作废条件的数据基础设施）。
- 验证重算：精智达 Z 476.75→438.61，止损距离 0.84%→8.8%，
  RRR 12.30→1.17（诚实区间）；博杰 Z 99.39→91.44（不再=跌停价 85.54）。

配套（`signal_plan.py`）：RRR 质量票钳制 `[2.5, 5]`——RRR>5 不加分，
details 显式标注"止损距离过窄，神话数字嫌疑"（验收：坏数据不配给好评）。

## 问题 2：蘅东光倒在"创新高"上（门一口径 bug）

蘅东光 9/7 盘中 555.10 创历史新高、收盘 549.50，"创新高"判定却未过——
根因：`recent_high` 含当日高点，判定 `current ≥ recent_high×0.99` 实际
测度的是"收盘接近日内最高"（549.50 < 555.10×0.99=549.55，差 0.05 元）。

修复（`timing_engine.py`）：
- 新增 `prior_high`（近 N 日高，剔除当日，`_compute_prior_high`）；
- 三处判定换锚：门一"创新高"、海龟突破（Donchian）、趋势延续
  bars_since_high；`reentry_status`/踏空台账同步（`unified_engine.py`）；
- `prev_high` 语义修正为真昨日高点（旧实现=当日高点，名不副实）；
- RSI 文案带口径（"RSI14未过热(<67)"）——验收质疑"蘅东光 RSI6=73
  未拦、罗博特科 RSI6=79.5 拦"实为口径不透明（判定用 RSI14 一致）。
- 验证：蘅东光 549.50 ≥ 前高 462.30×0.99 → 门一创新高通过、三重门
  全过、确认追强降仓放行（不再是零提示）；光力科技式（未破前高）仍拦。

## 问题 3：推导栏与闸门栏新旧并存

- 推导栏 defend 文案由"禁用:确认追强"改为
  "可用:恐慌抄底/套利低吸/价量突破/趋势延续 | 追强降仓需三重门"
  （与环境闸门栏同口径——两处表述相反时，执行以谁为准的审计问题）；
- 推导栏置信度优先取执行计划的评分口径（"中(2/6)"），
  消除"置信度:高" vs "2/6 中"的矛盾（点名四轮的老问题）。

## 问题 3b：存量事件与新规则双轨未标注

博杰事件仍带旧 Z=85.54（9/7 生成，规则冻结合理），但报告未注明——
审计者会误以为 Z 线统一没做。修复（`signal_lifecycle.py`）：
- `SignalEvent.rule_version` 字段（DB 幂等补列），`register_event` 记录
  生成时的 `z_line_mode`；
- 事件状态行渲染版本：旧模式事件标"Z按bare_structure(旧版)"，
  旧库空版本标"Z按旧版规则(版本未记录)"，当前版显示规则名。

## 问题 4：风险系数随机出现（规则化）

验收实证：精智达振幅 8.16%、风险系数 1.00；博杰反而 0.60——
风险乘数与波动率无关等于随机数。修复（`signal_plan.py`）：
- `_volatility_tier_multiplier`：max(当日振幅, 近5日平均振幅) 分档，
  ≥8% → 0.6、≥5% → 0.8、<5% 不降档（换手已有 turnover_hot 单独惩罚）；
- 配置 `risk.volatility_tier`（enabled/high_amp/mid_amp/window 可调）；
- 作废条件：strategy_stats 分层统计显示 vol_tier 档期望不低于全样本
  → 移除分档（高波动不是劣势来源时，规则退场）。

## 测试（Phase5 新增 `tests/test_phase5_rework.py` 28 用例）

```bash
python -m pytest tests/ -q --ignore=tests/test_institutional_fund_flow.py
# 339 passed / 2 skipped（2 个 metrics_enrichment 基线遗留与改造无关）
```

锚定验证项：精智达 buffered 重算（Z/RRR/止损距离/风险乘数四联动）、
RRR 12.3 不加分且显式标注、[2.5,5] 区间照常加分、博杰防宽（Z≠跌停价）、
蘅东光门一过/旧口径复现（差 0.05 元）/光力式仍拦/海龟突破/再入场头部、
prior_high 构建口径、RSI 文案、推导栏同步、执行计划置信度、
事件版本三类标注、波动分档六分支、试探仓预算反推。

旧断言按新语义更新：`test_z_line_unification`（默认改 buffered，
bare 转显式对照族）、`test_hypothesis_gate`（毫厘止损三模式全拒、
裸位防噪下限）、`test_defensive_chase`（RSI14 文案）、
`test_phase4_p2p3`（趋势延续 buffered Z）。

## Phase5 配置速查

```yaml
hypothesis_gate.z_line_mode: buffered_structure   # 默认（bare/atr_buffer 为对照/回退族）
hypothesis_gate.z_buffer_max_pct: 0.08            # 缓冲上限（防拖到跌停价）
hypothesis_gate.bare_noise_min_pct: 0.008         # 裸位防噪下限
confidence.rrr_quality_cap: 5.0                   # RRR 质量票上限钳制
risk.volatility_tier.enabled: true                # 波动率分档风险乘数
```

# 第六阶段：层间接口缺失修复（9/8 验收）

三处层间接口缺口（策略→分档、评分→决策、仓位→账户）+ 两项老问题复烧：

## 问题 1：策略层 → 分档层（追强的语言，低吸的价格）

追强类策略（确认追强/价量突破/趋势延续）的 Y 一律挂在 MA10，距现价
10%~13%（蘅东光 Y=498.26 距 555.25 有 10.3%、罗博特科 Y=577.99 距
656.10 有 13.5%），RRR 被压到 0.96。修复：`timing_engine.entry_main_tier_price()`
按策略族分化——追强 Y=触发位（浅回踩档=触发位×0.98），低吸类
（恐慌抄底/套利低吸）维持 MA10 档。分档层档位文案随策略标注
（追强档/浅回踩档 vs MA10档/MA5档）。

## 问题 2：评分层 → 决策层（0/6 照样放行，评分不连闸门）

0/6 低置信信号不提供胜率证据，不允许假设胜率超过盈亏平衡线。修复：
`signal_plan._evaluate_ev_gate()`——评分 <1（0/6）直接拒绝；策略统计
足 30 笔用真实胜率算 EV=W×R−(1−W)，EV<0 拒绝；样本不足用 0.5 保守
占位。拒绝理由显式写“负期望不出场，空仓是合法输出”。`live_scheduler`
新增 `buy_ev_gate` 独立拒收桶，不进入 `buy` 列表。

## 问题 3：仓位层 → 账户层（公式对了，预算绝对值黑箱）

仓位公式从未披露预算基数，300 股×88.56=26,568 元敞口隐含 265 万
账户。修复：`config/timing.yaml position_budget` 显式定义预算基数
（250,000 元/笔）与账户口径（1,000,000 元），调度 note 与
`param_appendix` 披露“建议 N 股×价=元(预算基数) | 单笔风险敞口 |
账户 100 万(1%预算=10,000 元，敞口占 X%) | 按 1% 风险预算反推隐含
账户”。

## 老问题 A：推导栏与置信度栏矛盾（第五轮）

执行计划建立后统一刷新 `sig.trigger_reason` 的“置信度:”口径为
“低(0/6)”——与置信度栏同源，不再出现“高/中” vs “0/6 低”并存。

## 老问题 B：派发日仍是 4/25 未刷新（第五轮）

`_count_distribution_days` 返回 `last_date/stale/stale_days`；末根距
参考日 >7 天判定过期，过期时指数 K 线换源重取，模式判定降级 defend
并披露“数据截至……已滞后 N 天，过期数据不参与模式判定”。

## 测试（新增 `tests/test_interface_gaps.py` 16 用例）

锚定：追强 Y=触发位 / 低吸 Y=MA10、EV 闸门拒绝与真实胜率路径、
预算披露字符串、置信度口径同源、派发日 stale 降级与披露。

```bash
python -m pytest tests/ -q -p no:cacheprovider --basetemp=.\pytest-basetemp
# 366 passed / 2 skipped
```

## Phase6 配置速查

```yaml
hypothesis_gate.ev_gate:
  enabled: true
  min_confidence_score: 1        # 0/6 不提供胜率证据 → 拒绝
  min_trades_for_win_rate: 30    # 足样本才采信真实胜率
  default_win_rate: 0.5          # 样本不足保守占位
tiering.chase_probe_pct: 0.02    # 追强浅回踩档 = 触发位 × 0.98
position_budget:
  budget_per_stock: 250000       # 单笔预算基数（元）
  account_value: 1000000         # 账户口径（元，1% 风险预算=10,000 元）
```
