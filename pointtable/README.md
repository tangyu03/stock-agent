# pointtable v6

这是 `qianyi.md` 的地基：`models/` 领域对象、`db/session.py` schema 和幂等 ETL 脚本。`signals`、`journal` 均有幂等唯一约束；`pool` 使用日期、标的和主题去重。

第 2 周新增 `core/` 纯判断层、`data/akshare.py` 安全封装与 `data/guard.py`；第 3 周新增 `jobs/`、`notify/render.py`、`notify/push.py` 和 `stats.py`。`core/` 不允许 import `data/`、`db/`、`notify/`，该约束由 `tests/test_deps.py` 锁定。

2026-11-02 重构（行为保持，金样四投影 sha256 逐字节一致）：`data/normalize.py` 行归一 + `data/resolvers.py` 声明式 K 线兜底引擎——`akshare.py` 只声明 `_INDEX_*`/`_STOCK_*`/`_SECTOR_*` fetcher 链与语义参数（min_rows / require_amount / last_resort），兜底顺序不再藏在 if-else 里；`domain/`（rules.py / reconciliation.py）收拢候选评估与 §8 对账纯函数；`jobs/reporting.py` 报告投影唯一入口、`jobs/pool_admin.py` 池子命令实现；`db/repo.py` 承接表级读写，`db/session.py` 只留连接生命周期与 schema 迁移。既有 import 入口（`db.session`、`jobs.evening` 等）经 re-export 保持不变，旧调用零改动。

命令行：

```bash
python -m pointtable.cli stats
python -m pointtable.cli stats --date 2026-10-22
python -m pointtable.cli holdings
python -m pointtable.cli pool
python -m pointtable.cli pool-sync
```

池子的常规编辑入口是 `config/pool.yaml`（声明式全量定义）：编辑后执行 `pool-sync`，把整份声明落成当日快照并输出 added/removed/changed/kept 对账（当日行先删后插，历史日期不改写；同日重跑幂等）。`pool-add` 保留用于临时补单票。

常用节奏命令：

```bash
python -m pointtable.cli market-input --date 2026-10-22 --output reports/market-2026-10-22.json
python -m pointtable.cli watch --date 2026-10-22 --price 10.5 --second-cycle --context-json reports/break-context.json
python -m pointtable.cli tail-snapshot --date 2026-10-22
python -m pointtable.cli restore-check --date 2026-10-22
python -m pointtable.cli pool-performance --date 2026-10-15 --window 5
python -m pointtable.cli report --date 2026-10-22 --market-json reports/market-2026-10-22.json --output reports/2026-10-22.md
python -m pointtable.cli push-file --title "复盘" --file reports/2026-10-22.md
```

统计依赖两张快照表：`market_snapshots` 保存当日行情，`zone_snapshots` 保存区间历史。STOP 后 5 日追踪读取 `closed_at` 之前最近的区间快照，因此后续 STOP 滚动不会改变历史统计。

## 迁移

```bash
python -m pointtable.db.migrate --source-db data/stock_agent.db
```

输出：

- `pointtable/data/pointtable.db`：`signals`、`journal`、`pool` 及当日 `tail_snapshots` 表。
- `pointtable/data/migration/*.csv`：旧历史表冻结导出。
- `pointtable/data/migration/reconciliation.json`：源表行数与迁移行数对账。
- `config/theme_map.yaml`、`push.yaml`：资产迁移。
- `config/config.yaml` 内置 `regulator_list`、`lhb_dumpers` 人工维护名单种子。

迁移规则：

- `signal_events` → `signals`：旧事件作为 `enter` 信号，区间由原入场价与目标下沿取上下沿组成，兼容恐慌抄底等倒序区间。
- `signal_event_logs` → `journal`：仅保留 `record_type` 分类，`confirmed/touched_only` 已移除。
- `trade_logs`：只做原始 CSV 导出；人工执行结论不再迁移。
- 持仓/池资产先从 `portfolio.yaml` 与 `trade_logs` 提取，板块主题用 `theme_map.yaml` 人工映射兜底。

当前真实库对账：`signals 61/61`，`journal 152/152`，`pool 131`。`reconciliation.json` 的 `totals` 记录总落表数；各模块 `inserted` 只表示本次运行新增数，幂等重跑时可以为 0。
