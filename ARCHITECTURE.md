<!--
  精简架构文档 — 只覆盖调用链、模块职责和踩坑点。
  超过 60 行或新增模块后应 review 是否过时。
-->

## 项目概览

A股短线交易 Agent，支持盘前预案 / 盘中实时信号 / 盘后复盘 / 周报四阶段运行。
入口：`python -m src.main run --phase <phase>`

## 目录地图

```
src/
├── main.py                  # CLI 入口
├── config_models.py         # 统一配置加载 (YAML → dataclass)
├── db.py                    # SQLite 初始化、sector_scan_history / data_cache 表
├── orchestrator/
│   ├── engine.py            # Orchestrator: 四阶段调度中枢 + 推送触发
│   └── unified_engine.py    # UnifiedSignalBatch: 不区分持仓/自选的全量进场/出场扫描
├── data_layer/              # 数据获取层（只读，无副作用）
│   ├── akshare_adapter.py   # AKShare 封装：Session patch、反爬、数据源开关
│   ├── sw_industry.py       # 申万行业数据：SW_LEVEL1(31个)/SW_LEVEL2(113个)、normalize_sector、calc_sector_metrics
│   ├── stock_data.py        # 个股K线/指标 (KDJ/MACD/RSI/BOLL/均线)
│   ├── skill_wrapper.py     # 技能包装器：注册 22 个外部分析技能
│   └── data_cache.py        # 本地数据缓存
├── analyzers/               # 分析层（纯计算，不持有状态）
│   ├── sector_scanner.py    # SectorScanner: 板块三级分类 (主线/支线/退潮) + 轮动检测 + 交叉诊断
│   ├── market_scorer.py     # MarketScorer: 市场评分 → attack/defend/retreat
│   ├── timing_engine.py     # TimingEngine: 技术面进场/出场信号、仓位建议
│   ├── stock_filter.py      # StockFilter: 个股筛选
│   ├── external_market.py   # 外盘 (美股/港股) 环境评分
│   └── gem_sci_tech_scorer.py  # 创业板/科创板评分
├── decision/                # 决策聚合层
│   ├── aggregator.py        # Aggregator: 四层决策 → _collect_relevant_sectors + 信号汇总 + 风控守卫
│   ├── position_builder.py  # 仓位构建器
│   └── insight_miner.py     # 文章/观点挖掘
├── loop/                    # 离线/回测
│   ├── market_mode_adaptive.py  # 市场模式自适应（自适应仓位上限）
│   ├── stockagent_tuned_v3_signals.py  # 策略调优 v3：动态板块+事件驱动
│   ├── backtest_engine.py   # 回测引擎 (滑点/手续费/T+1/涨跌停)
│   └── data_loader.py       # 回测数据加载
├── push/
│   ├── pushplus.py          # PushPlus 微信推送 (200条/天, ~5 req/s)
│   └── templates.py         # 推送模板渲染
└── feedback/
    ├── daily_review.py      # 每日复盘
    ├── weekly_report.py     # 周报
    └── trade_logger.py      # 交易日志

config/                       # YAML 配置文件
skills/                       # 22 个外部分析技能 (K线形态、缠论、聪明钱等)
data/logs/                    # 运行日志
```

## 四阶段调用链（v3 — pre_market/intraday 已合并）

```
pre_market / intraday:  环境评估(自适应模式+外盘+双创) → 板块扫描 → unified_engine 全量扫 → 合并推送(环境+买卖信号)
post_market:            daily_review.generate → 推送复盘
weekly:                 weekly_report.generate → 推送周报
```

## 关键模块依赖方向

```
data_layer (只读) → analyzers (纯计算) → decision (聚合) → orchestrator (调度+推送)
                                              ↑
                                          loop (回测，只读决策层)
```



## 外部数据源

- **AKShare** (东方财富): 行情、涨停池、申万指数、行业资金流。Session 需 patch 静态 headers 防反爬。
- **PushPlus**: 微信推送，每日 200 条限额，~1.2s/条间隔。
- **SQLite**: 本地 `data/` 目录，存 sector_scan_history 和 data_cache。
