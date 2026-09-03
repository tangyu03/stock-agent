# stock-agent

> A股短线交易 Agent — 阶段二核心规则修正版

基于大V实盘帖子提炼的量化交易策略，覆盖环境判定/进场/出场/仓位/数据层全链路。已完成两阶段整改（证据链修复 + 核心规则修正），回测验证 2021-2026 共 1341 交易日。

## 📊 整改成果

| 指标 | 原始策略 | 整改后 | 变化 |
|---|---|---|---|
| 总收益率% | +135.78 | **+156.09** | +20.31pp |
| 夏普比率 | 0.6011 | **0.6591** | +0.0580 |
| 最大回撤% | 56.07 | 50.54 | -5.53pp |
| 交易笔数 | 375 | **92** | -75%（低频） |
| 期望/笔% | +1.64 | **+6.90** | +321%（高质量） |
| 均盈均亏比 | 1.38 | **2.15** | 大赚小亏 |
| **2024段期望%** | **-0.90** | **+5.33** | **专项关卡通过** |

策略形态从"高频小赚"转为"低频大赚"：交易笔数降 75%，单笔期望提升 3 倍。

## 📦 项目结构

```
stock-agent/
├── src/                          # 源码
│   ├── analyzers/                # 分析层
│   │   ├── timing_engine.py      # 择时核心（B1/C1/C2/D1-D7整改）
│   │   ├── market_mode_adaptive.py # 环境判定（B1五日线回归）
│   │   ├── lhb_scorer.py         # 🆕 F10龙虎榜评分（百分制）
│   │   ├── event_calendar.py     # 🆕 F5事件日历（解禁+财报）
│   │   ├── institutional_trapped.py # 🆕 F2机构被套套利
│   │   ├── institutional_scorer.py # F9融资余额替换北向
│   │   ├── sector_scanner.py     # 板块三级分类
│   │   ├── sector_ranker.py      # 板块涨跌幅排名
│   │   ├── market_scorer.py      # 大盘评分
│   │   ├── market_env.py         # 市场环境增强
│   │   ├── gem_sci_tech_scorer.py # 双创技术位
│   │   ├── external_market.py    # 外盘扰动
│   │   └── stock_filter.py       # 个股过滤
│   ├── decision/                 # 决策层
│   │   ├── live_scheduler.py     # 🆕 P3实盘信号调度器
│   │   ├── aggregator.py         # 四层决策汇总
│   │   ├── position_builder.py   # 递进加仓+AddPlan
│   │   ├── position_analyzer.py  # 持仓健康度
│   │   ├── holding_health.py     # 健康度数据类
│   │   └── insight_miner.py      # 观点挖掘
│   ├── loop/                     # 回测层
│   │   ├── backtest_engine.py    # 真实T+1回测引擎
│   │   ├── market_mode_adaptive.py # 回测用环境判定
│   │   ├── metrics.py            # A1三分类胜率+expectancy
│   │   ├── signal_evaluator.py   # 事件研究法
│   │   ├── walk_forward.py       # 滚动验证
│   │   ├── data_loader.py        # 数据加载
│   │   └── stockagent_tuned_v3_signals.py # v3信号生成
│   ├── data_layer/               # 数据层
│   │   ├── akshare_adapter.py    # AKShare多源切换+熔断
│   │   ├── stock_data.py         # K线+技术指标
│   │   ├── sw_industry.py        # 同花顺行业体系
│   │   ├── iwencai_api.py        # 问财OpenAPI
│   │   ├── skill_wrapper.py      # 22个外部分析skill
│   │   └── data_cache.py         # SQLite缓存
│   ├── orchestrator/             # 调度层
│   │   ├── engine.py             # 四阶段调度（P3集成调度器）
│   │   └── unified_engine.py     # 全量买卖信号扫描
│   ├── push/                     # 推送层
│   │   ├── pushplus.py           # 微信推送
│   │   └── templates.py          # HTML模板
│   ├── feedback/                 # 反馈层
│   │   ├── daily_review.py       # 每日复盘
│   │   ├── weekly_report.py      # 周报
│   │   └── trade_logger.py       # 交易日志
│   ├── utils/
│   │   └── structured_logger.py  # 结构化日志+trace_id
│   ├── config_models.py          # 配置模型
│   ├── db.py                     # SQLite
│   ├── llm_client.py             # LLM客户端
│   └── main.py                   # 入口
├── config/                       # 配置
│   ├── timing.yaml               # 择时参数（78阈值）
│   ├── portfolio.yaml            # 持仓清单（17只）
│   ├── position.yaml             # 仓位参数
│   ├── market_scoring.yaml       # 大盘评分6维
│   ├── sector_scanner.yaml       # 板块三级分类
│   ├── risk.yaml                 # 风控
│   ├── schedule.yaml             # 7时段调度
│   ├── push.yaml                 # 推送（token环境变量化）
│   ├── llm.yaml                  # LLM（key环境变量化）
│   ├── insights.yaml             # 观点
│   ├── event_calendar.yaml       # 🆕 F5人工维护事件日历
│   └── blacklist.yaml            # 🆕 D6板块黑名单（可选）
├── scripts/                      # 脚本
│   ├── run_live_push.py          # 实盘推送
│   ├── run_backtest_v2.py        # 回测（简化版，已废弃）
│   ├── run_grid_search_real.py   # 网格搜索
│   ├── run_signal_search.py      # 信号质量搜索
│   ├── build_sector_mapping.py   # 板块映射构建
│   └── test_*.py                 # 测试脚本
├── tests/
│   └── test_stock_agent.py       # 单元测试
├── docs/                         # 文档
│   ├── GOVERNANCE.md             # 溯源治理+勘误登记
│   ├── 01_策略评估报告.docx
│   ├── 02_P0修复总结报告.docx
│   └── 04_策略有效性评估_当前代码回测版.docx
├── data/                         # 运行时数据（.gitignore）
│   ├── logs/
│   └── stock_agent.db
├── ARCHITECTURE.md               # 架构文档
├── LOOP_REWRITE_README.md        # 回测重写文档
├── TIMING_REFACTOR_README.md     # 择时重构文档
├── requirements.txt              # 依赖
└── .gitignore
```

## 🚀 快速开始

### 环境准备

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量（凭证已从config移除）
export DEEPSEEK_API_KEY="your_deepseek_key"    # LLM用
export TOKEN_PUSH="your_pushplus_token"        # 推送用
export IWENCAI_API_KEY="your_iwencai_key"      # 问财用（可选）

# 3. 初始化数据库
python -m src.main init
```

### 实盘运行

```bash
# 盘前/盘中统一检查（环境评估+信号生成+调度+推送）
python -m src.main run --phase intraday

# 盘后复盘
python -m src.main run --phase post_market

# 周报
python -m src.main run --phase weekly

# 处理用户文章/观点
python -m src.main article --text "大V帖子内容" --source "雪球"
```

**信号服务模式**：盘中/盘前只输出买卖建议信号，不读取、不维护持仓——
卖出信号全量输出（持仓由用户自行核对），买入按入场类型优先级排序
（价量突破 > 套利低吸 > 恐慌抄底 > 确认追强，同优先级按信心/紧急度/代码兜底）
并全量输出；不做数量上限、预算或碎单拦截，资金管理由用户自行处理。
`scripts/trade_feedback.py` 回执闭环保留为可选工具，不再参与调度。

### 观察指标口径

- 换手率优先取腾讯实时行情，东财 ulist 与历史 K 线作备源；推送按百分比显示，当前仅展示，不参与技术投票。
- 执行计划统一用建议入场价作为基准价计算 RRR，不用现价；低置信度买入自动转入观察卡，不进入调度买入列表。
- 量能快照用截至前一交易日的 60 日分位数判断缩量/过热；换手过热建议仓位乘 0.5，股东户数增幅超过 20% 建议仓位乘 0.8。
- 主力资金流主源为问财逐日序列；当问财返回超大单/大单序列时直接使用，缺失时用 AKShare 逐日明细补齐。AKShare 明细失败只做短退避，不冻结整个程序。
- 前十大流通股东机构占比来自季报 F10，按最新和上一期披露做百分点变化展示；这是慢频披露数据，不进入盘中投票。

### 回测验证

```bash
# 完整回测（需先准备 kline_cache_full.json 数据）
python /path/to/scripts/b1_gen_signals.py     # 生成信号
python /path/to/scripts/b1_verify.py          # 跑三曲线+2024段专项关卡

# 十交易日回归测试
python /path/to/scripts/p4_regression_test.py

# 实盘影子记录
python /path/to/scripts/shadow_trading.py --record   # 每日记录
python /path/to/scripts/shadow_trading.py --review   # T+10验证
python /path/to/scripts/shadow_trading.py --report   # 生成报告
```



### 工程定位

大V的规则框架 + 机器的执行纪律：
- 止盈不犹豫（C5市况自适应落袋）
- 破位不心软（C1硬触发+ATR止损价）
- 熔断不讨价还价（B1跌破5日线即retreat）

## ⚠️ 已知局限

1. **F4/F11 需分钟级数据**：日内时段规则和竞价数据在日频引擎无法验证
2. **F2 机构买入均价近似**：用VWAP=(O+H+L+C)/4 近似，不精确
3. **F5 解禁数据为历史**：stock_restricted_release_summary_em 返回已发生数据，未来预警依赖人工维护
4. **C3 规则默认观察级**：四条卖出规则触发太频繁会破坏低频大赚模式，默认只log不执行
5. **回测中机构打分被禁用**：monkey-patch score_institutional_holding 返回中性，实盘启用F9融资余额

## 📚 文档

- `docs/GOVERNANCE.md` — 溯源治理 + 勘误登记表 + 推断参数登记册
- `ARCHITECTURE.md` — 精简架构文档
- `LOOP_REWRITE_README.md` — 回测引擎重写说明
- `TIMING_REFACTOR_README.md` — 择时引擎重构说明

## 🔒 安全说明

- LLM API key 和 PushPlus token 已从 config 移除，通过环境变量读取
- `.gitignore` 已覆盖日志/数据库/缓存文件
- 首次部署需设置环境变量：`DEEPSEEK_API_KEY` / `TOKEN_PUSH` / `IWENCAI_API_KEY`


     
  今天跑(默认今天,周六非交易日会跳过):
  python scripts/s_daily_run.py

  指定日期(回补,比如 08-14):
  python scripts/s_daily_run.py --date 2026-08-14
  
  只评估已有快照、不重新拉取(重放):
  python scripts/s_daily_run.py --date 2026-08-14 --skip-snapshot

  挂任务计划的话,在命令里直接写 python scripts/s_daily_run.py
  即可,脚本自己会判非交易日跳过;一天挂两次(16:00/17:00)对应 config 里 snapshot_times 的固定时点。


  
     
  一次性准备
     
  cd C:\Users\15831\Documents\code\stock-agent
  pip install -r requirements.txt
     
  # 环境变量（凭证已从 config 移到环境变量）
  export DEEPSEEK_API_KEY="..."
  export TOKEN_PUSH="..."
  export IWENCAI_API_KEY="..."   # 可选

  # 初始化数据库（幂等，已建好会跳过）
  python -m src.main init

  Windows 控制台是 GBK,跑任何带中文输出的命令前先加 PYTHONIOENCODING=utf-8,否则会撞到 emoji 编码错误:

  export PYTHONIOENCODING=utf-8

  每个交易日,按时间走

  盘中统一检查(盘前 8:50 或盘中)——实盘信号推送

  python -m src.main run --phase intraday

  做一件事:环境评估(自适应模式)→ 全量自选池信号 → 调度器按期望排序、套预算和总仓位闸门(P0-2,defend
  时买入合计 ≤ 50 万)→ 一条推送。pre_market 已合并进来,不用单独跑。

  非交易日(周六/周日)照常跑也不跳过:按上一交易日收盘数据复盘(P2-13 审计 2026-08-22,
  周末任意时间可查最近交易日情况;工作日深夜/清晨仍默认跳过,需加 --force 强制推送)。

  推送后——等待回执(P1-3 闭环,这套工程刚修好的部分)

  推送的信号此刻在 trade_logs 里是 pending,不会影响持仓。收盘后你看实际成交,回执:

  # 看今天推了什么、还没回执
  python scripts/trade_feedback.py --list

  # 真买了 → 成交价(实际成交价没填就用 --price 补)
  python scripts/trade_feedback.py --execute <id> --price 127.5

  # 没执行 → 忽略
  python scripts/trade_feedback.py --ignore <id>

  # 改了执行(比如减半仓) → 标记修改
  python scripts/trade_feedback.py --modified <id> --price 127.5

  # 随时看聚合持仓(executed 记录 buy加仓/sell减仓,成本取最近买入价)
  python scripts/trade_feedback.py --holdings

  关键点:你回执的 executed 记录就是下一轮调度的真实持仓来源(P0-1 修的),不再是 add_plans
  占位。所以回执不能偷懒——不执行,系统就永远当空仓跑。

  收盘后——板块管道(16:00/17:00)

  python scripts/s_daily_run.py            # 快照 + 新鲜度守卫 + S3 评估 + 板块池
  python scripts/s_daily_run.py --date 2026-08-14   # 补某天
  python scripts/s_daily_run.py --skip-snapshot     # 只重放评估,不拉新数据

  周末自动跳过(今天是周六,直接跑会打印"周末非交易日,跳过")。这步喂给 B5 板块池和 S3
  主线预期,是盘前判断的输入。

  盘后复盘 + 周报

  python -m src.main run --phase post_market    # 15:30
  python -m src.main run --phase weekly         # 周五收摊

  自动化(建议)

  Windows 任务计划按这个表挂,命令里直接写 python scripts/xxx.py,用绝对路径:

  ┌────────────────┬────────────────────────────────────────────┐
  │      时点      │                    命令                    │
  ├────────────────┼────────────────────────────────────────────┤
  │ 8:50 盘中检查  │ python -m src.main run --phase intraday    │
  ├────────────────┼────────────────────────────────────────────┤
  │ 15:30 盘后复盘 │ python -m src.main run --phase post_market │
  ├────────────────┼────────────────────────────────────────────┤
  │ 16:00 板块管道 │ python scripts/s_daily_run.py              │
  ├────────────────┼────────────────────────────────────────────┤
  │ 周五           │ python -m src.main run --phase weekly      │
  └────────────────┴────────────────────────────────────────────┘

  回执 --list 也可以在推送后挂一个提醒(不是自动执行——成交只有你知道,系统不能替你判断)。

  当前要注意的两件事

  1. 持仓现在是空的。闭环刚建,还没有任何 executed 回执,--holdings
  会显示空。第一笔回执后它才开始聚合。在此之前,调度按空仓跑,买入受 position_limit 总闸门兜底(defend 50
  万),不会冲满仓。
  2. 回执要当天做。--list 按日期过滤,跨天不去清,后面的持仓聚合会被陈旧 pending 干扰。

  一句话总结每天闭环:盘中推 → 收盘回执 → 盘后跑板块 →
  明天盘中读真实持仓再调度。你只需管"回执"这一环,其余都自动化了。
