# B → v3.1 迁移方案（v2.0 终版）

## 0. 裁决标准（迁移期间不再变更）


## 1. 最终处置清单

**留（平移）：**

| 资产 | 落点 | 备注 |
|---|---|---|
| akshare 封装（重试/缓存） | datafeed/akshare_client.py | 仅改包路径 |
| DataFreshnessGuard | datafeed 出口统一断言 | |
| 脏数据闸门 | datafeed | "尊重成交量"的前提是量是真的 |
| 量能外推 | core/volume.py | 并入 |
| 财报事件日历 | 数据层 | premise 校验输入（"规避英伟达财报"） |
| theme_map.yaml | config/，薄板块层归属依据 | 股票→单一主板块，人工确认 |
| 回执 trade_feedback | receipts 表 + cli.py | **交互一字不改** |
| 30/50笔统计 | stats 信号质量层 | 统计对象 strategy→strategy_id |
| trade_logs | journal 表 | 迁移脚本直灌 |
| TimingEngine 调度与轮询 | 盘中链路 | **机制保留，回调替换，频率分段**（见 §3.4） |

**退役（逻辑吸收）：**

| 模块 | 吸收方式 |
|---|---|
| XYZW 假说门 | premise 文本快照 + strategies.yaml 的 y/z/w 字段 |
| 配对出场 Z/W | 策略配置的 z/w 字段 |
| 再入场 max 2 次 | on_stop 链（破5→10→20，最多2次） |
| EV 闸门 | 一个 if：满30笔且滚动期望≤0 → EV_NEG 仅记录不推送 |
| position_state 状态机 | 仓位布尔推算（回执驱动） |
| arbiter | "STOP 排最前"一句排序 |

**删（无赦免）：**

板块 S1–S3 规则与同花顺31次调度、LLM 调用、基本面第七问、估值透镜、防守追强三重门、RRR 钳制、风险乘数引擎、振幅降档、同板块并发上限、position_budget、解禁日历。




当前结构：

• stock-agent2/
  ├── README.md
  ├── ARCHITECTURE.md
  ├── CHANGELOG.md
  ├── requirements.txt
  ├── config/                     # 所有 YAML 配置
  │   ├── timing.yaml             # 时机引擎阈值（止损/Z线/量能/生命周期）
  │   ├── scoring_weights.yaml    # 市场/个股评分权重
  │   ├── market_scoring.yaml     # 市场环境评分规则
  │   ├── position.yaml           # 仓位构建规则
  │   ├── portfolio.yaml          # 组合约束（预算/敞口上限）
  │   ├── risk.yaml               # 风险乘数/振幅降档
  │   ├── push.yaml               # 推送通道
  │   ├── schedule.yaml           # 调度时刻表
  │   └── sector_map.yaml         # 板块映射
  ├── src/
  │   ├── main.py                 # CLI 入口（run --phase / --stats / --strategies）
  │   ├── config_env.py           # 环境变量加载
  │   ├── config_models.py        # YAML → dataclass
  │   ├── db.py                   # SQLite（insights/trade_logs/signal_events/point_entry…15 张表）
  │   ├── rules_version.py        # 规则版本号（事件出生时锁定）
  │   ├── signal_states.py        # 信号状态枚举
  │   ├── data_layer/             # "把数据拿进来并保证可信"
  │   │   ├── akshare_adapter.py  # 行情/板块/龙虎榜，重试
  │   │   ├── akshare_safe.py     # 限速 + 异常包装
  │   │   ├── iwencai_api.py      # 问财选股
  │   │   ├── stock_data.py       # 统一行情接口
  │   │   ├── data_cache.py       # DB 级缓存
  │   │   ├── ths_cache.py        # 同花顺缓存
  │   │   └── sw_industry.py      # 申万行业
  │   ├── cache/                  # 板块/板块快照服务
  │   │   ├── sector_map_service.py
  │   │   ├── board_snapshot.py
  │   │   ├── snapshot_store.py
  │   │   ├── builders.py
  │   │   └── ttl_cache.py
  │   ├── analyzers/              # "纯判断与纯生成：不碰 IO 不碰库"
  │   │   ├── market_scorer.py    # 市场环境评分
  │   │   ├── market_env.py       # 环境判定（attack/defend/retreat）
  │   │   ├── market_resonance.py # 市场共振
  │   │   ├── external_market.py  # 外围市场
  │   │   ├── gem_sci_tech_scorer.py
  │   │   ├── sector_ranker.py    # 板块排名
  │   │   ├── sector_rs_tracker.py
  │   │   ├── sector_pool.py      # 主线/轮动/退潮判定
  │   │   ├── theme_attribution.py
  │   │   ├── timing_engine.py    # 时机引擎入口（五策略调度）
  │   │   ├── timing_entry_checkers.py  # 五种入场策略判定
  │   │   ├── timing_exit_checker.py    # 卖出分级 OR
  │   │   ├── timing_stop_loss.py # Z 线计算
  │   │   ├── timing_models.py    # EntrySignal / StopLossCalc
  │   │   ├── timing_t0.py        # T0 盘中触发
  │   │   ├── timing_data_fetch.py
  │   │   ├── timing_utils.py
  │   │   ├── timing_lifecycle.py # 事件生命周期
  │   │   ├── signal_lifecycle.py # 事件状态机
  │   │   ├── signal_plan.py      # 执行计划（档位/量能外推）
  │   │   ├── volume_pattern.py   # 量能形态 P 档
  │   │   ├── volume_projection.py# 量能外推
  │   │   ├── stock_filter.py     # 风控过滤
  │   │   ├── institutional_scorer.py   # 机构票权
  │   │   ├── institutional_trapped.py
  │   │   ├── lhb_scorer.py       # 龙虎榜
  │   │   ├── driver_attribution.py
  │   │   ├── event_calendar.py   # 事件日历
  │   │   └── suction_index.py    # 吸筹指数
  │   ├── decision/               # "决策层：模式 × 仓位 × 调度"
  │   │   ├── aggregator.py       # 信号聚合
  │   │   ├── mode_rules.py       # 模式规则
  │   │   ├── position_builder.py # 仓位构建
  │   │   ├── position_analyzer.py
  │   │   ├── live_scheduler.py   # 盘中调度（档位/止盈/风险乘数）
  │   │   ├── holding_health.py
  │   │   └── insight_miner.py
  │   ├── orchestrator/           # "编排时刻：什么时候干什么"
  │   │   ├── unified_engine.py   # 统一引擎（信号→批次→推送→落库）
  │   │   └── engine.py           # 日报编排（板块管道/复盘）
  │   ├── plan/                   # 点位表层
  │   │   ├── point_table.py      # 点位表
  │   │   ├── point_decision.py   # 点位决策
  │   │   ├── point_overlay.py    # 点位叠加
  │   │   └── sector_playbook.py  # 板块剧本
  │   ├── feedback/               # "收盘回执 + 分层统计"
  │   │   ├── trade_logger.py     # 交易日志
  │   │   ├── daily_review.py     # 每日复盘
  │   │   ├── daily_review_parts.py
  │   │   ├── weekly_report.py
  │   │   ├── strategy_stats.py   # 分层胜率/期望
  │   │   ├── completion_scoring.py
  │   │   ├── signal_ledger.py
  │   │   ├── signal_quality.py
  │   │   ├── event_tracker.py
  │   │   ├── observation_tracker.py
  │   │   ├── distribution_watch.py
  │   │   ├── sector_changes.py
  │   │   └── param_appendix.py   # 参数附录
  │   ├── push/                   # "通道 + 渲染"
  │   │   ├── pushplus.py         # PushPlus 推送
  │   │   └── templates.py        # 信号/报告模板
  │   ├── loop/                   # 回测层
  │   │   ├── stockagent_tuned_v3_signals.py
  │   │   ├── signal_evaluator.py
  │   │   ├── market_mode_adaptive.py
  │   │   ├── data_loader.py
  │   │   ├── data_freshness.py
  │   │   └── metrics.py
  │   └── utils/
  │       ├── trading_calendar.py
  │       ├── structured_logger.py
  │       ├── session_cache.py
  │       └── numbers.py
  ├── scripts/
  │   ├── s_daily_run.py          # 板块管道定时
  │   ├── trade_feedback.py       # 回执 CLI
  │   ├── run_backtest_v2.py
  │   ├── run_live_push.py
  │   ├── build_sector_mapping.py
  │   └── rebuild_sector_map.py
  └── tests/                      # pytest，按模块命名 test_*.py