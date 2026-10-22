# pointtable v6

这是 `qianyi.md` 的第 1 周地基：`models/` 四个领域对象、`db/session.py` 四表 schema 和幂等 ETL 脚本。`signals`、`journal`、`receipts` 均有幂等唯一约束；`pool` 使用日期、标的和主题去重。

第 2 周新增 `core/` 纯判断层、`data/akshare.py` 安全封装与 `data/guard.py`；第 3 周新增 `jobs/`、`notify/render.py`、`notify/push.py` 和 `stats.py`。`core/` 不允许 import `data/`、`db/`、`notify/`，该约束由 `tests/test_deps.py` 锁定。

命令行：

```bash
python -m pointtable.cli receipt <signal_id> --verdict executed
python -m pointtable.cli veto <signal_id>
python -m pointtable.cli stats
python -m pointtable.cli holdings
python -m pointtable.cli pool
```

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

## 迁移

```bash
python -m pointtable.db.migrate --source-db data/stock_agent.db
```

输出：

- `pointtable/data/pointtable.db`：`signals`、`journal`、`receipts`、`pool` 四表。
- `pointtable/data/migration/*.csv`：旧历史表冻结导出。
- `pointtable/data/migration/reconciliation.json`：源表行数与迁移行数对账。
- `pointtable/config/theme_map.yaml`、`push.yaml`：资产迁移。
- `pointtable/config/config.yaml` 内置 `regulator_list`、`lhb_dumpers` 人工维护名单种子。

迁移规则：

- `signal_events` → `signals`：旧事件作为 `enter` 信号，区间由原入场价与目标下沿取上下沿组成，兼容恐慌抄底等倒序区间。
- `signal_event_logs` → `journal`：触发类迁移标 `confirmed`，其余标 `touched_only`。
- `trade_logs` → `receipts`：只迁移已有 `executed/ignored/veto` 回执，`pending` 不假装有结论。
- 持仓/池资产先从 `portfolio.yaml` 与 `trade_logs` 提取，板块主题用 `theme_map.yaml` 人工映射兜底。

当前真实库对账：`signals 61/61`，`journal 152/152`，`receipts 0`（源库 4 条旧结论均缺 `event_id`），`pool 131`。`reconciliation.json` 的 `totals` 记录总落表数；各模块 `inserted` 只表示本次运行新增数，幂等重跑时可以为 0。
