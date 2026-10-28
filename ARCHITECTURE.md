# pointtable v6 architecture

pointtable is a signal service, not an account tracker or execution engine. It turns a manual pool into four kinds of rows: entry points, protective stops, profit exits, and context/triggers. It does not record or infer human execution.

## Layers

```text
config/       # strategies, runtime config, theme map and private push config

pointtable/
├── models/     # Signal, JournalEntry, PoolEntry domain contracts
├── db/         # SQLite four-table schema, migrations and persistence adapters
├── data/       # akshare safe wrapper, TTL cache, mock source and dirty-data guard
├── core/       # pure math/judgment: MA, trend, volume, auction, LHB, sector, generator, lifecycle
├── jobs/       # five backend routines: auction_call, watch, restore_check, settle, evening
├── notify/     # report rendering and PushPlus channel
├── stats.py    # signal-quality and behavior reports
└── cli.py      # stats/holdings/pool/shadow/cutover entry point
```

## Rules

- `core/` must not import `data/`, `db/` or `notify/`.
- Signal state has only four values: `ACTIVE`, `TRIGGERED`, `EXPIRED`, `INVALIDATED`; all transitions go through `core.lifecycle.transition`.
- BUY touches require a second cycle or high volume; SELL/STOP triggers are single point.
- Entry and protection rows preserve the same parent chain; pool protection is not duplicated while a retest observation is active.
- LOW market volume halves BUY size hints; it does not suppress the plan.
- Push volume is intentionally low: point events and summary reports only, not analysis noise.
- PushPlus is a manually invoked, one-way notification channel. There is no background scheduler, automatic routine push, or button/callback interaction; T-size choices are CLI commands.
