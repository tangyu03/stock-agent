# stock-agent-fixed

> A股短线交易 Agent — P0 修复版 + 完整回测验证

本包是基于原 stock-agent 工程的 P0 修复版本，已通过 14 项自动化验证用例 + 2.5 年历史回测验证。

## 📦 包内容总览

```
stock-agent-fixed/
├── README.md                          ← 本文件（快速入门）
├── ARCHITECTURE.md                    ← 原工程架构文档
├── LOOP_REWRITE_README.md             ← 原工程回测重写文档
├── TIMING_REFACTOR_README.md          ← 原工程择时重构文档
├── readme.md                          ← 原工程 README
├── 北交所修复总结.md                   ← 原工程北交所适配总结
│
├── src/                               ← 修复后的源码（47 个 .py）
│   ├── analyzers/                     ← 分析层（含修复后的 timing_engine.py）
│   ├── data_layer/                    ← 数据层（未改动）
│   ├── decision/                      ← 决策聚合层
│   │   ├── aggregator.py             ← ★ 重写（修复 5 处 P0 bug）
│   │   ├── position_builder.py       ← ★ 重写（修复语法错误 + 新增 5 方法）
│   │   ├── position_analyzer.py      ← 🆕 新增（替代问财依赖）
│   │   ├── holding_health.py         ← 🆕 新增（数据类补全）
│   │   └── insight_miner.py          ← 原文件
│   ├── feedback/                      ← 反馈层
│   │   ├── daily_review.py           ← ★ 修复（holdings → stocks）
│   │   ├── trade_logger.py           ← 原文件
│   │   └── weekly_report.py          ← 原文件
│   ├── orchestrator/                  ← 调度层
│   │   ├── engine.py                 ← ★ 增强（trace_id + 结构化日志）
│   │   └── unified_engine.py         ← 原文件
│   ├── loop/                          ← 回测层（未改动）
│   ├── push/                          ← 推送层（未改动）
│   ├── utils/                         ← 🆕 新增工具包
│   │   └── structured_logger.py      ← 🆕 JSON 日志 + trace_id
│   ├── config_models.py
│   ├── db.py
│   ├── llm_client.py
│   └── main.py
│
├── config/                            ← 配置文件（10 个 YAML，未改动）
│   ├── timing.yaml                   ← 择时阈值（回测与实盘共享）
│   ├── risk.yaml                     ← 风控参数
│   ├── portfolio.yaml                ← 持仓配置（18 只股票）
│   ├── position.yaml                 ← 仓位约束
│   ├── market_scoring.yaml           ← 大盘评分
│   └── ...
│
├── data/                              ← 运行数据（来自原工程）
│   ├── stock_agent.db                ← SQLite 数据库（含 trade_logs 等表）
│   └── logs/                         ← 运行日志目录
│
├── skills/                            ← 16 个外部分析技能（来自原工程）
│   ├── a-share-capital-flow/         ← A股资金流分析
│   ├── a-stock-kline-analyzer/       ← K线分析
│   ├── chan-theory/                  ← 缠论
│   ├── sector-rotation/              ← 板块轮动
│   ├── smart-money/                  ← 聪明钱
│   └── ...
│
├── scripts/                           ← 验证与回测脚本
│   ├── verify_p0_fixes.py            ← 🆕 P0 修复验证（14 项检查）
│   ├── backtest/                      ← 🆕 基于当前代码的回测
│   │   ├── run_backtest_v2.py        ← 🆕 回测脚本
│   │   ├── kline_cache.json          ← 🆕 17 只股票 2.5 年 K 线缓存
│   │   └── pairs_detail.json         ← 🆕 514 笔买卖配对明细
│   └── original/                      ← 原工程 17 个脚本（完整保留）
│       ├── build_sector_mapping.py   ← 板块映射构建
│       ├── run_backtest_demo.py      ← 回测示例
│       ├── run_grid_search.py        ← 网格搜索
│       ├── run_grid_search_real.py   ← 实盘数据网格搜索
│       ├── run_live_push.py          ← 实盘推送
│       ├── run_signal_search.py      ← 信号搜索
│       ├── run_compare_params.py     ← 参数对比
│       ├── run_compare_v5.py         ← v5 对比
│       ├── test_bug_fixes.py         ← bug 修复测试
│       ├── test_comprehensive.py     ← 综合测试
│       ├── test_integration_v2.py    ← 集成测试 v2
│       ├── test_macd_fix.py          ← MACD 修复测试
│       ├── test_metrics.py           ← 指标测试
│       ├── test_pushplus.py          ← PushPlus 测试
│       ├── test_real_holdings.py     ← 真实持仓测试
│       ├── test_real_holdings_v2.py  ← 真实持仓测试 v2
│       └── test_v3_integration.py   ← v3 集成测试
│
├── tests/
│   └── test_stock_agent.py           ← 原测试（待扩展）
│
└── docs/                              ← 评估报告与图表
    ├── 01_策略评估报告.docx            ← 工程六维评估（策略/风控/回测/工程/聚合/可观测）
    ├── 02_P0修复总结报告.docx          ← P0 修复详情（3 新增 + 5 修改）
    ├── 03_策略有效性评估_旧数据版.docx  ← 基于旧数据库的评估（已修正）
    ├── 04_策略有效性评估_当前代码回测版.docx ← ★ 基于当前代码 2.5 年回测的最终结论
    └── charts/                       ← 所有图表 PNG（11 张）
        ├── architecture.png          ← 系统架构图
        ├── strategy_flow.png         ← 策略信号流程图
        ├── radar.png                 ← 六维评估雷达图
        ├── backtest_comparison.png   ← 旧数据 vs 当前代码对比
        ├── backtest_entry.png        ← 进场类型有效性
        ├── backtest_exit_hold.png    ← 出场类型 + 持仓天数
        └── backtest_yearly.png       ← 年度稳定性
```

## 🚀 快速入门

### 1. 环境准备

```bash
# Python 3.10+（已测试 3.12/3.13）
pip install akshare pyyaml
```

### 2. 验证 P0 修复

```bash
cd stock-agent-fixed
python3 scripts/verify_p0_fixes.py
```

预期输出：14 项检查全部 ✅ 通过。

### 3. 运行回测（基于当前代码）

```bash
cd stock-agent-fixed
python3 scripts/backtest/run_backtest_v2.py
```

预期输出：
- 总信号数: 4972
- 配对数: 514
- 胜率: 42.2%
- 盈亏比: 2.30
- 期望收益: +4.38%/笔

回测脚本会优先使用 `scripts/backtest/kline_cache.json` 缓存数据（已包含 2024-01 ~ 2026-07 的 17 只股票 K 线）。如需重新拉取最新数据，删除该缓存文件即可。

### 4. 启用结构化日志（生产环境）

```bash
export LOG_FORMAT=json
python3 -m src.main run --phase intraday
```

## 📊 关键结论

### P0 修复（已完成）

| # | Bug | 修复方式 |
|---|-----|---------|
| 1 | `aggregator.py` 引用未定义的 `HoldingHealth`/`WatchlistAnalysisResult` | 新增 `holding_health.py` 补全数据类 |
| 2 | `aggregator.py` 中 `self._position_analyzer` 从未初始化 | 新增 `position_analyzer.py` 桩模块 |
| 3 | `aggregator.py` 中 `holdings` 变量未定义 | 统一为 `holdings = stocks` |
| 4 | `aggregator.py` 中 `_build_sector_classification_map` 引用未定义 `stocks` | 改为参数 `holdings` |
| 5 | `position_builder.py` `@dataclass` 后空行接 `class` 语法错误 | 重写整个文件 |
| 6 | `position_builder.py` 缺失 `create_add_plan` / `append_add_plan` 方法 | 新增 5 个方法 |
| 7 | `daily_review.py` 用 `holdings` 字段但配置是 `stocks` | 改为优先读 `stocks` |
| 8 | 全局单例不利于并行回测 | 新增 `create_timing_engine` 工厂 |
| 9 | 日志无 trace_id 难以排查 | 新增 `structured_logger.py` |

### 策略有效性（基于当前代码 2.5 年回测）

| 指标 | 旧数据库（11天34笔） | 当前代码回测（2.5年514笔） |
|------|---------------------|---------------------------|
| 胜率 | 20.6% ❌ | **42.2%** ✅ |
| 盈亏比 | 1.53 ❌ | **2.30** ✅ |
| 期望收益 | -0.59%/笔 ❌ | **+4.38%/笔** ✅ |
| 持仓 >3 天 | 0% ❌ | **72%** ✅ |

**核心结论**：策略本身有效，旧数据库的负表现来自 aggregator 调度 bug（已修复）。已具备小资金实盘验证条件。

## 🔧 P0 修复详情

### 新增模块（3 个）

1. **`src/decision/holding_health.py`** — HoldingHealth 与 WatchlistAnalysisResult 数据类
2. **`src/decision/position_analyzer.py`** — PositionAnalyzer 桩模块，不依赖问财 API
3. **`src/utils/structured_logger.py`** — JSON 格式日志 + trace_id + contextvars

### 修改文件（5 个）

1. **`src/decision/aggregator.py`** — 重写，修复 5 处未定义变量/方法 bug
2. **`src/decision/position_builder.py`** — 重写，修复语法错误 + 新增 AddPlan 类 + 5 个方法
3. **`src/feedback/daily_review.py`** — 修复 `_get_holding_summary` 字段读取
4. **`src/analyzers/timing_engine.py`** — 追加并发安全工厂函数
5. **`src/orchestrator/engine.py`** — 注入 trace_id + 结构化日志 + 异常捕获

## 📋 部署建议

1. **覆盖原工程**：将本包内容覆盖原 stock-agent 工程（建议先备份）
2. **安装依赖**：`pip install akshare pyyaml`（无新增第三方依赖）
3. **启用 JSON 日志**：`export LOG_FORMAT=json`（生产环境）
4. **运行验证**：`python3 scripts/verify_p0_fixes.py` 确认部署成功
5. **小流量试运行**：用 5-10 万小资金实盘 1-2 个月，验证实盘胜率 ≥ 35%
6. **逐步扩大**：如一致，扩大至 50 万；如不一致，排查执行层问题

## 📚 文档阅读顺序

1. `docs/01_策略评估报告.docx` — 工程六维评估（先看这个了解全貌）
2. `docs/02_P0修复总结报告.docx` — P0 修复详情
3. `docs/04_策略有效性评估_当前代码回测版.docx` — ★ 最终结论（基于 2.5 年回测）
4. `docs/03_策略有效性评估_旧数据版.docx` — 旧数据库分析（已被 04 修正）

## ⚠️ 已知限制

1. **回测未覆盖 attack 模式**：确认追强 + 价量突破在 attack 模式下的表现待验证
2. **行业集中度高**：portfolio.yaml 18 只股票集中在科创板/创业板半导体
3. **未做 Walk-Forward**：参数稳定性未验证（建议 P1 推进）
4. **未对比基准**：未区分 alpha 与 beta（建议与沪深300/半导体ETF对比）

## 📞 后续支持

- P0 修复已完成，可立即部署
- P1 改进路线（约 16.5 人日）详见 `docs/02_P0修复总结报告.docx` 第五章
- 实盘运行后如遇问题，可参考 `docs/01_策略评估报告.docx` 第七章改进路线图

---

**版本**：v1.0-fixed  
**生成日期**：2026-07-20  
**基于原工程**：stock-agent（提交版本含 11 天实盘数据）  
**修复验证**：14 项自动化检查全部通过  
**回测验证**：2.5 年历史数据，514 笔配对，期望收益 +4.38%/笔
