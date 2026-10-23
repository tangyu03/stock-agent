# stock-agent2 → v6.0 迁移计划（整合全部裁决·终版）

> 本文档整合至今全部讨论结论：最新一轮"无懂哥出处"复核的改判（sector_pool/theme_attribution/sector_ranker 瘦身保留、external_market 瘦身）、sector_scanner 改判、portfolio 拆分、volume_pattern 拆三件、models 层新增。与此前任何版本冲突处以本文档为准。

---

## 一、目标架构（v6.0 + models 层）

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
│   │   ├── push.py             # 人工单向推送；回执走 CLI
│   │   └── render.py           # 槽位报告（P1-P7/N1-N3/A1-A4/C1 + 措辞库 + 降级规则）
│   ├── stats.py                # 两层报表（信号质量层 + 行为层）
│   └── cli.py                  # receipt / veto / stats / holdings / pool
├── legacy/                     # 冻结区：回测引擎、旧测试批次、被删模块源码
└── tests/
    ├── fixtures/               # 川环26.5/博创42.3/兆龙60.5（懂哥当时输入三元组）
    ├── test_ma.py / test_trend.py / test_volume.py / test_lifecycle.py
    ├── test_ordering.py / test_replay.py / test_deps.py
```

**依赖方向硬约束**：core/ 不得 import data/、db/、notify/；models/ 只自校验不执行业务逻辑；jobs/ 是唯一编排层，持有全部调用。

## 二、行为建模（触发源三分）

**时钟触发**：evening(15:30) / auction_call(9:15-9:25) / watch(9:30-15:00) / restore_check(14:30起) / settle(15:05)。

**行情触发**：入区间→确认（BUY双周期/STOP单点）；破位→两级定性（板块→指数）；STOP当日收复→纠错提醒；前提破坏→作废+撤销。

**人触发（全可选，不驱动状态机）**：veto / receipt / pool编辑 / config编辑。对"人"的唯一约束=市场熔断（LOW+指数DOWN→零预案日）。

## 三、配置层迁移

| 文件 | 处置 | 落点 |
|---|---|---|
| timing.yaml | 瘦身 | 止损/生命周期/量能阈值→strategies.yaml |
| push.yaml | 保留原样 | notify/push.py |
| schedule.yaml | 并入 | config.yaml |
| theme_map.yaml | **保留原样** | config/theme_map.yaml |
| sector_map.yaml | 删 | 与 theme_map 命名冲突 |
| scoring_weights.yaml / market_scoring.yaml / position.yaml / risk.yaml | 删 | 无主链路 |
| portfolio.yaml | 拆分 | 持仓清单→pool表；规则删 |
| config.json（若有） | 删 | 并入 config.yaml |

## 四、源文件去留（整合最新复核）

### 保留平移（16 个）

| 文件 | 落点 |
|---|---|
| akshare_adapter.py / akshare_safe.py / stock_data.py | data/akshare.py |
| data_cache.py（瘦身） | db/session.py 附 |
| event_calendar.py | core/（time_trigger 驱动） |
| timing_stop_loss.py（瘦身） | core/ma.py + core/volume.py |
| lhb_scorer.py（瘦身） | core/lhb.py |
| stock_filter.py（瘦身） | generator premise 校验 |
| timing_models.py | models/ 领域对象 |
| timing_data_fetch.py / timing_utils.py | core/ 对应文件 |
| rules_version.py | 保留原样 |
| signal_states.py | models/signal.py |
| pushplus.py | notify/push.py |
| data_freshness.py | data/guard.py |
| utils/trading_calendar.py + structured_logger.py + session_cache.py + numbers.py | 平移保留 |
| strategy_stats.py | stats.py（30/50笔骨架） |
| trade_logger.py | journal 迁移脚本 |
| config_env.py / config_models.py | config/ |

### 瘦身保留（12 个，含最新改判）

| 文件 | 砍什么 | 留什么 |
|---|---|---|
| **sector_pool.py** | 全市场计数（38/40/12）、S调度乘数 | **主线/轮动/退潮三态判定** → core/sector.py，喂报告N3+板块行 |
| **sector_ranker.py** | 分数排名体系 | **池内定性排序**（破位/未破位、上游/下游、一线/二线）→ core/sector.py，喂P4去弱择强 |
| **theme_attribution.py** | 复杂归属计算 | **theme_map读取函数** → core/sector.py |
| **sector_scanner.py** | 全市场扫描部分（若存在） | **池内板块状态判定**（拥挤度+板块行生成）→ core/sector.py |
| volume_pattern.py | 分型矩阵/星级/冲突/委比/RSI/位置档/早盘窗口/待验清单 | 测量工具（量比档/换手分位/主动差/封板豁免/分歧规则）→ core/volume.py；口径交叉校验→data/guard.py |
| timing_entry_checkers.py | 恐慌抄底/趋势延续/确认追强 | 价量突破→volume_breakout、套利低吸→first_board_dip |
| timing_engine.py / timing_t0.py / timing_lifecycle.py / signal_lifecycle.py / signal_plan.py | 拆分合并 | 生成→generator.py；触发→watch.py；生命周期→lifecycle.py；闸门→guard.py |
| external_market.py | 影响评分逻辑 | **"外围±%展示"环境行** → 环境行 |
| main.py | 冗余命令 | CLI 入口 |

### 删除（48 个）

**data_layer**：iwencai_api.py、ths_cache.py、sw_industry.py
**cache/**（整目录5个）：sector_map_service / board_snapshot / snapshot_store / builders / ttl_cache
**analyzers**：market_scorer.py、market_env.py、market_resonance.py、gem_sci_tech_scorer.py、institutional_scorer.py、institutional_trapped.py、suction_index.py、driver_attribution.py、volume_projection.py
**decision/**（整目录7个）：aggregator / mode_rules / position_builder / position_analyzer / live_scheduler / holding_health / insight_miner
**plan/**（整目录4个）：point_table / point_decision / point_overlay / sector_playbook
**orchestrator**：unified_engine.py、engine.py（拆后删除）
**feedback**：completion_scoring / daily_review / daily_review_parts / weekly_report / signal_ledger / event_tracker / observation_tracker / distribution_watch / sector_changes / param_appendix
**loop/**（除 freshness）：stockagent_tuned_v3_signals / signal_evaluator / market_mode_adaptive / data_loader / metrics → 冻结 legacy/（非删除）
**未归类**：llm_client.py、block_trade.py、skill_wrapper.py + skills/（19文件）、data/ths_cache/ 乱码文件

### 测试迁移

旧 521 个审计批次测试（test_p220_audit_defects.py 等）整体冻结进 legacy/ 供回归参考。新建 7 个测试文件（test_ma/trend/volume/lifecycle/ordering/replay/deps），fixture 三案例先行。

## 五、数据表迁移（15 → 4，全部新建）

| 旧表 | 处置 | 新表 |
|---|---|---|
| trade_logs | ETL | journal（signal_id/from/to/reason/价格/confirmed/created_at） |
| signal_events | ETL | signals（四状态+status_reason+parent_id+valid_policy） |
| insights / point_entry / 板块快照 / 其余11张 | 冻结归档 | 无对应 |
| 新建 | — | receipts（verdict+size_filled）、pool（date/symbol/theme_label） |

**关键**：四张新表全部字段级 ETL，不做表名替换。迁移脚本幂等可重跑，三方对账（源表行数 vs journal 迁移行数 vs 抽样核对）。

## 六、核心实现要点（v6.0 已定，此处不重述）

- ma_tomorrow 预估收盘价语义 + 三案例锚点
- break_qualifier 三输入+前置豁免+DIVERGENCE 第三路
- 持仓票每晚生成规则（UP/DOWN 分支）
- TRIGGERED 确认语义（BUY 双周期/STOP 单点）
- 破位两步确认（retest_confirm）
- size_hint 两修饰器（LOW 减半、T_BUY CLI 落库）
- 账户熔断→市场熔断（零预案日）
- render 槽位规范 + 措辞库 + 降级规则（非两融标的、回执覆盖率闸）

## 七、分阶段实施（四周）

### 第 1 周：清场与地基
1. 打 tag `b2-final` 封存整个 stock-agent2。
2. 拆走六样资产：trade_logs 历史、signal_events 历史、theme_map overrides、监管清单、砸盘席位名单、持仓清单。
3. **人工核对三案例 fixture 输入值**（川环/博创/兆龙三元组，博客评论区+行情软件）——比任何代码先写。
4. models/ 四领域对象 + db/session.py。
5. 字段级 ETL 脚本（15→4）+ 三方对账。
6. 执行删除清单（§四），被删源码复制进 legacy/（不物理删除，防将来回查）。
7. 旧测试冻结进 legacy/。
- **验收**：对账对平；剩余（平移+瘦身）文件 import 无残留；`git grep` 确认 decision/plan/market_*/institutional_* 零引用。

### 第 2 周：core 重写（测试先行）
8. fixture 三案例落盘。
9. core/ 七个文件依序：ma.py → trend.py → volume.py（破位三问+分歧+封板豁免）→ auction.py → lhb.py → sector.py（三态+排序+theme_map读取）→ lifecycle.py。
10. generator.py（四键校验、ENTER/Z/W 同生、持仓票 UP/DOWN 规则、premise 校验链、上下文行、time_trigger 行、on_stop 链）。
11. data/guard.py（freshness+脏数据闸门+口径交叉校验）。
- **验收**：core 单测全覆盖（含三案例锚点<0.5%误差）；依赖方向测试 test_deps 入 CI。

### 第 3 周：jobs + 报告 + 统计
12. jobs/ 五文件挂上（evening 含零预案判定；watch 含确认语义/两级定性/STOP 排序/纠错提醒；settle 含 valid_policy 清点/retest_confirm/T 连坐/Z 滚动）。
13. notify/render.py 槽位实现（P1-P7/N1-N3/A1-A4/C1 + 措辞库 dict + 降级规则 + 空槽跳过）。
14. notify/push.py 通道照抄旧配置；只保留人工 push-file，无按钮回传。
15. stats.py 两层报表（信号质量层含 EV_NEG/池子质量报表；行为层含纪律执行率/做T增值/复原违规）。
- **验收**：test_ordering 通过（同 tick STOP 排头部）；报告样本人工过一遍措辞（对齐 moban.md 槽位表）。

### 第 4 周：影子验证与切流
16. 影子跑 5 个交易日：全量跑推测试通道，每日核对点位合理性（重点验证先手点位预估收盘价语义、板块行对破位定性的影响）。
17. 首周每晚确认 pool→板块归属清单（theme_map 人工兜底）。
18. 切流：webhook 切正式通道，旧信号全量 EXPIRED(reason=system_migration)。
19. `--legacy` 留一个月供回查。
20. README 首行写明"推送变少是设计不是故障"。
- **验收**：影子期无"该生成没生成"漏报；切流后一周无故障；两层 stats 正常产出；三案例锚点全绿。

## 八、遗留未决项（已收口）

| 项 | 紧急度 |
|---|---|
| llm_client.py / block_trade.py / skill_wrapper.py + skills/（19文件）的实际引用与用途 | 已裁决：保留 |
| ths_cache 乱码文件删除前引用核查 | 已裁决：`data/ths_cache/` 归档，不迁入新链路 |
| sector_pool / sector_ranker / theme_attribution 瘦身边界函数级确认（需贴码） | 已裁决：保留在 `legacy/`；新链路由 `pointtable/core/sector.py` 承接池内判定与去弱择强 |
| 旧表→新表字段映射明细（15张旧表映射到 5 张新表） | 已完成：见 `MIGRATION_MAPPING.md` |
| 测试文件逐模块映射表 | 已完成：见 `MIGRATION_MAPPING.md` |

### 切流状态

- 2026-10-22 已执行 `cutover --yes`。
- 推送模式已切换为 `formal`。
- 迁移前 83 个 `ACTIVE/TRIGGERED` 信号已置为 `EXPIRED(reason=system_migration)`，并在 `journal` 记录审计。
- `python -m src.main` 已删除，当前唯一入口是 `python -m pointtable.cli`。
- 用户已裁决跳过 7 个自然日稳定观察，直接进入后续 v6.1 项；`--legacy` 保留一个月用于回查。
- 每日执行 `migration-status` 检查推送模式、迁移审计、旧信号冻结和池子归属；2026-10-22 切流已完成，旧源码回查保留到 2026-11-22。

## 九、最终形态一句话

**88 个源文件 → 约 26 个（含 models 层），15 张旧表 → 5 张新表（signals/journal/receipts/pool/tail_snapshots），14 个 YAML → 3 个（strategies/config/theme_map）。删掉的全是"决策层官僚"（评分、组合、调度、分型帝国、全市场扫描），留下的全是"测量工具+点位+环境事实+懂哥的板块语言"（量比、换手分位、主动差、破位三问、四状态信号、主线/轮动/退潮、去弱择强）。**

