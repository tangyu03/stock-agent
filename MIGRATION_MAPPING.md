# stock-agent2 → pointtable 迁移映射

本文档按当前实现核对，不是设计稿。源库是 `data/stock_agent.db`，新库是 `pointtable/data/pointtable.db`。迁移入口是 `pointtable/db/migrate.py`。

## 1. 范围结论

- 新运行时持久化核心表为 `signals`、`journal`、`receipts`、`pool`；另有当日运行表 `tail_snapshots`。
- 做字段级 ETL 的旧表只有三张：`signal_events`、`signal_event_logs`、`trade_logs`。
- 其余旧表不做字段级迁移。原因分三类：旧决策层已删除、临时/缓存数据不需要搬迁、新链路改为从行情与配置实时计算。
- `legacy/` 只作回查归档，不参与运行时。`sector_pool.py`、`sector_ranker.py`、`theme_attribution.py`、`data/ths_cache/` 保留为归档，不迁入 `pointtable`。
- `theme_map`、`pointtable/data/akshare.py` 封装、`DataFreshnessGuard` 和脏数据闸门保留。

## 2. 旧表 → 新表总表

| 旧对象 | 处置 | 新目标 / 后继 |
|---|---|---|
| `signal_events` | 字段级 ETL | `signals` |
| `signal_event_logs` | 字段级 ETL | `journal` |
| `trade_logs` | 字段级 ETL + 原始 CSV 导出 | `receipts`、`pool` |
| `board_component` | 归档 | `pointtable/core/sector.py` 与 `market-input` 的板块事实，不落历史表 |
| `board_snapshot` | 归档 | 同上 |
| `data_cache` | 归档 | `pointtable/data/akshare.py` 的安全封装与重试策略 |
| `distribution_watch` | 归档，功能删除 | 无 |
| `distribution_watch_logs` | 归档，功能删除 | 无 |
| `distribution_watch_snapshot` | 归档，功能删除 | 无 |
| `insight_daily` | 归档，洞察链删除 | 无 |
| `insights` | 归档，洞察链删除 | 无 |
| `judgment_chains` | 归档，洞察链删除 | 无 |
| `judgment_targets` | 归档，洞察链删除 | 无 |
| `judgments` | 归档，洞察链删除 | 无 |
| `market_score_history` | 归档，评分层删除 | `market-input` 产出的市场事实 JSON |
| `observation_checkpoints` | 归档，观察层删除 | 无 |
| `observation_intercepts` | 归档，观察层删除 | 无 |
| `point_entry` | 归档，不做表替换 | `pointtable/core/generator.py` 重新生成信号 |
| `sector_scan_history` | 归档 | `pointtable/core/sector.py` 实时判定 |
| `sector_status_history` | 归档 | `pointtable/core/sector.py` 实时判定 |
| `signal_event_daily_snapshots` | 归档 | 当日 `tail_snapshots` + `market-input` 收盘序列 |
| `signal_rejections` | 归档 | `generator` 返回缺失/无效原因，不落独立拒绝表 |
| `signal_tracking_stop_logs` | 归档 | `signals.status/status_reason` + `journal` |
| `stock_sector_history` | 归档 | `theme_map` overrides + `market-input` 板块数据 |
| `strategy_status` | 归档，策略开关层删除 | `pointtable/config/strategies.yaml` |
| `volume_projection_log` | 归档，量能预测层删除 | `pointtable/core/volume.py` 实时判定 |
| `sqlite_sequence` | SQLite 内部对象 | 不迁移 |
| `signal_ledger`（视图） | 归档 | `signals` / `journal` / `receipts` 统计与报告查询 |

## 3. 字段级映射

### 3.1 `signal_events` → `signals`

| 旧字段 | 新字段 | 规则 |
|---|---|---|
| `event_id` | `id` | 原值作为主键；重复执行靠 `INSERT OR IGNORE` 幂等 |
| `stock_code` | `symbol` | 原值 |
| `stock_name` | 不迁移 | 报告运行时从 `config/portfolio.yaml` 反填 |
| `entry_type` | `strategy_id` | 空值写 `unknown` |
| `entry_price` / `breakout_level` | `zone_low`、`zone_high` 的入场端 | 优先 `entry_price`；为空则用 `breakout_level` |
| `target_low` | `zone_low`、`zone_high` 的目标端 | 与入场端比较后，较小值写 `zone_low`，较大值写 `zone_high` |
| `size_hint` | 无 | 新链路不做迁移期仓位建议；新表保留 `NULL` |
| `stop_loss` | `premise.stop_loss` | JSON 序列化 |
| `target_high` | `premise.target_high` | JSON 序列化 |
| `entry_snapshot` | `premise.entry_snapshot` | JSON 序列化 |
| `hypothesis_x/y/z/w`、`y_formula`、`y_inputs`、`rules_version`、`frozen_prev_status`、`maintenance_check`、`driver_json` | 不迁移 | XYZW、公式评分与旧维护链已删除 |
| `status` | `status` | `valid/frozen → ACTIVE`；`triggered/filled → TRIGGERED`；`expired/time_exit/sig_target → EXPIRED`；`invalidated/chase_abandon → INVALIDATED`；未知写 `ACTIVE` |
| `invalid_reason` | `status_reason` | 优先原值；为空写 `old_status:<旧状态>` |
| `born_date` | 仅兜底 `created_at` | `created_at` 为空时使用 |
| `expire_date` | `valid_policy` 推导 | 有值写 `until_stopped`；无值写 `intraday` |
| `created_at` | `created_at` | 规范为 `YYYY-MM-DD HH:MM:SS`；为空用 `born_date` |
| `updated_at` | `closed_at` | 仅迁移后状态是 `EXPIRED/INVALIDATED` 时写入 |
| `side` | `side` | 固定 `BUY` |
| `tag` | `tag` | 固定 `enter` |
| `trend` | `trend` | 固定 `UP` |
| `activation`、`parent_id` | `activation`、`parent_id` | 迁移期写 `NULL` |

迁移前若入场端或目标端不大于 0，或两者相等导致区间为空，该行跳过，不计入新表。

### 3.2 `signal_event_logs` → `journal`

| 旧字段 | 新字段 | 规则 |
|---|---|---|
| `id` | 不迁移 | `journal.id` 由新库自增 |
| `event_id` | `signal_id` | 原值 |
| `stock_code` | 不迁移 | 通过 `journal.signal_id` 关联 `signals.symbol` |
| `from_status` | `from_status` | 空值写 `none` |
| `to_status` | `to_status` | 空值写 `unknown` |
| `reason` | `reason` | 空值写空串 |
| `source` | 不迁移 | 迁移只保留状态事实 |
| `trigger_data.price` 或 `trigger_data.current_price` | `price` | 两个字段按顺序取第一个非空值 |
| `to_status` | `confirmed` | `triggered/filled/sig_target/time_exit → confirmed`；其他写 `touched_only` |
| `created_at` | `created_at` | 规范为 `YYYY-MM-DD HH:MM:SS` |
| `rule_entry`、`rules_version` | 不迁移 | 旧规则版本层删除 |

`journal` 的唯一索引由 `signal_id, created_at, from_status, to_status, reason, price, confirmed` 组成，重复迁移不会产生重复流水。

### 3.3 `trade_logs` → `receipts`

只迁移 `user_action` 为 `executed`、`ignored`、`veto` 的行。

| 旧字段 | 新字段 | 规则 |
|---|---|---|
| `id` | 不迁移 | `receipts.id` 由新库自增 |
| `event_id` | `signal_id` | 必须非空；空值跳过 |
| `user_action` | `verdict` | 只接受 `executed/ignored/veto` |
| `actual_position` | `size_filled` | 非空时转整数 |
| `created_at` | `created_at` | 规范为 `YYYY-MM-DD HH:MM:SS` |
| `actual_price`、`exit_price`、`pnl_pct` | 不迁移 | 成交回报层不保留价格复盘流水 |
| `note`、`review_note`、`review_outcome` | 不迁移 | 报告层改为实时生成 |
| `trigger_price`、`stop_loss`、`target_price` | 不迁移 | 已由 `signals.zone/premise` 承载 |
| `hypothesis_*`、`paired_*`、`z_reference`、`zw_triggered` | 不迁移 | XYZW 假说与配对 Z/W 出场已删除 |
| `driver_json`、`sector_status`、`market_score`、`sector_version` | 不迁移 | 旧评分/归因层删除 |

### 3.4 `trade_logs` + 配置 → `pool`

`pool` 不是旧表直接替换，而是新链路的股票池。

| 来源 | 新字段 | 规则 |
|---|---|---|
| `trade_logs.date` | `date` | 去重后写入 |
| `trade_logs.stock_code` | `symbol` | 去重后写入 |
| `config/portfolio.yaml.stocks[].code` | `date`、`symbol` | 追加到迁移结果，日期为迁移执行日 |
| `pointtable/config/theme_map.yaml.overrides` | `theme_label` | 按 `symbol` 反查；没有映射写 `unknown` |

`pool` 的唯一约束是 `date + symbol + theme_label`；主题映射更新后可重跑补齐标签。

### 3.5 新增运行表 `tail_snapshots`

`tail_snapshots` 不来自旧库，用于 14:30 尾盘价格快照。

| 字段 | 含义 |
|---|---|
| `trade_date` | 交易日 |
| `symbol` | 股票代码 |
| `captured_at` | 快照时间 |
| `price` | 尾盘价格 |
| `source` | 数据来源，当前为 `spot` |

主键是 `trade_date + symbol`；晚间 `market-input` 优先使用该快照作为 `est_close`。

## 4. 归档表为什么不做字段映射

归档表不进入新链路，逐字段改成空值或 `NULL` 会制造假兼容。它们的原数据保留在 `legacy/` 与源库中，可按需要回查。需要提取历史事实时，先用 `pointtable/db/migrate.py` 的 CSV 导出能力，再按临时需求分析，而不是把这些表塞进新 schema。

## 5. 测试文件逐模块映射

新测试不追求旧测试一比一复制。旧测试里有大量评分、调度、洞察、基本面闸门、估值透镜、标签语义、RRR、EV 闸门、回测和 XYZW 的断言；对应能力已删除，测试也应删除。仍有语义价值的部分映射如下。

| 旧测试 | 新测试 / 处置 |
|---|---|
| `test_signal_lifecycle.py` | `tests/test_lifecycle.py` |
| `test_signal_ledger.py` | `tests/test_stats.py`、`tests/test_replay.py` |
| `test_signal_plan.py` | `tests/test_generator.py`、`tests/test_render.py`；RRR、评分闸门、风险乘数与置信分层不迁移 |
| `test_theme_attribution.py` | `tests/test_core_context.py`；只保留 `theme_map` 与板块三态，复杂归因删除 |
| `test_sector_playbook.py` | `tests/test_core_context.py` |
| `test_timing_engine.py` | `tests/test_ma.py`、`tests/test_volume.py`、`tests/test_trend.py`；回测与旧 timing 引擎删除 |
| `test_timing_t0_and_ma20.py` | `tests/test_ma.py`、`tests/test_trend.py` |
| `test_volume_pattern.py` | `tests/test_volume.py` |
| `test_volume_pattern_conditions.py` | `tests/test_volume.py` |
| `test_volume_projection.py` | 归档，量能预测功能删除 |
| `test_point_decision.py` | `tests/test_generator.py`、`tests/test_lifecycle.py` |
| `test_point_overlay.py` | `tests/test_generator.py`、`tests/test_render.py` |
| `test_point_scheduler.py` | `tests/test_jobs.py` |
| `test_shadow_point_table.py` | `tests/test_shadow.py` |
| `test_market_mode_adaptive.py` | 归档，自适应市场评分删除；市场事实由 `tests/test_evening.py` 覆盖 |
| `test_market_mode_render.py` | `tests/test_render.py` |
| `test_p218_pushplus_chunking.py` | `tests/test_push.py` |
| `test_p3_feedback.py` | `tests/test_lifecycle.py`、`tests/test_stats.py` |
| `test_entry_maintenance.py` | `tests/test_generator.py`、`tests/test_lifecycle.py`、`tests/test_jobs.py` |
| `test_event_arbitration.py` | `tests/test_lifecycle.py` |
| `test_event_broadcast.py` | `tests/test_push.py`、`tests/test_render.py` |
| `test_event_calendar_auto.py` | `tests/test_jobs.py` |
| `test_event_tracker.py` | 归档，事件追踪层删除 |
| `test_reentry_cycle.py` | `tests/test_lifecycle.py`、`tests/test_generator.py` |
| `test_stock_agent.py` | `tests/test_cli.py`、`tests/test_v6_completion.py` |
| `test_stock_data.py` | `tests/test_data_akshare.py`、`tests/test_guard.py` |
| `test_data_source_fixes.py` | `tests/test_data_akshare.py`、`tests/test_evening.py` |
| `test_data_consistency.py` | `tests/test_guard.py` |
| `test_config_env.py` | `tests/test_cli.py`、`tests/test_deps.py` |
| `test_interface_gaps.py` | `tests/test_cli.py`、`tests/test_evening.py` |
| `test_metrics_enrichment.py` | `tests/test_stats.py` |
| `test_audit_reporting.py` | `tests/test_stats.py`、`tests/test_render.py` |
| `test_audit_followups.py` | 归档；仍有价值的数据一致性由 `tests/test_guard.py` 覆盖 |
| `test_f0_audit_fixes.py` | 归档；通用修复由 `tests/test_v6_completion.py` 收口 |
| `test_f1_audit_fixes.py` | 归档；通用修复由 `tests/test_v6_completion.py` 收口 |
| `test_p0_audit_0916.py` | 归档，旧审计批次 |
| `test_p216_completion_scoring.py` | 归档，完成度评分删除 |
| `test_p217_action_summary.py` | 归档，动作汇总评分删除 |
| `test_p220_audit_defects.py` | 归档，旧审计批次 |
| `test_p221_audit_defects.py` | 归档，旧审计批次 |
| `test_phase4_p2p3.py` | 归档，点位层改由 `tests/test_generator.py` 与 `tests/test_lifecycle.py` 覆盖 |
| `test_institutional_fund_flow.py` | 归档，机构资金评分删除；龙虎榜闸门由 `tests/test_core_context.py` 覆盖 |
| `test_live_scheduler_summary.py` | 归档，实时调度层删除；任务序由 `tests/test_jobs.py` 覆盖 |
| `test_observation_reasons.py` | 归档，观察层删除 |
| `test_observation_risk.py` | 归档，观察层删除 |
| `test_orchestrator_low_confidence.py` | 归档，orchestrator 删除 |
| `test_oversold_exit_audit.py` | 归档，超卖与配对出场逻辑删除 |
| `test_rules_version.py` | 归档，旧规则版本层删除；策略配置由 `tests/test_generator.py` 覆盖 |
| `test_strategy_kill_switch.py` | 归档，策略开关层删除 |
| `test_technical_votes.py` | 归档，多维投票删除；保留量价语义由 `tests/test_volume.py`、`tests/test_trend.py` 覆盖 |
| 其余无对应能力的旧测试 | `legacy/` 只读回查，不迁入新测试 |

## 6. 迁移核对口径

迁移输出 `pointtable/data/migration/reconciliation.json`，至少检查：

1. `signal_events.source == signals.inserted + signals.skipped`。
2. `signal_event_logs.source == journal.inserted + journal.skipped`。
3. 抽样核对 `signal_events.event_id` 与 `signals.id`；`signal_event_logs.event_id` 与 `journal.signal_id`。
4. 抽样核对旧状态与 `_STATUS_MAP` 转换后的新状态。
5. 抽样核对 `trade_logs.user_action` 与 `receipts.verdict`。
6. 抽样核对 `pool.symbol` 能在 `theme_map` 或 `unknown` 中得到解释。

## 7. 明确不迁移的能力

以下能力连同测试一起冻结在 `legacy/`，不因回补要求恢复：基本面闸门、估值透镜、标签语义、RRR 纪律、EV 闸门、回测引擎、XYZW 假说、配对 Z/W 出场、旧评分层、旧调度层、旧组合层。
