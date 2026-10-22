# pointtable v6 architecture

pointtable is a signal service, not an account tracker or execution engine. It turns a manual pool into four kinds of rows: entry points, protective stops, profit exits, and context/triggers. Human receipts annotate behavior; they never drive signal state.

## Layers

```text
pointtable/
├── models/     # Signal, JournalEntry, ReceiptEntry, PoolEntry domain contracts
├── db/         # SQLite four-table schema, migrations and persistence adapters
├── data/       # akshare safe wrapper, TTL cache, mock source and dirty-data guard
├── core/       # pure math/judgment: MA, trend, volume, auction, LHB, sector, generator, lifecycle
├── jobs/       # clock orchestration and routines: evening, auction, watch, restore, settle, shadow, cutover
├── notify/     # report rendering and PushPlus channel
├── stats.py    # signal-quality and behavior reports
└── cli.py      # receipt/veto/stats/holdings/pool/shadow/cutover entry point
```

## Rules

- `core/` must not import `data/`, `db/` or `notify/`.
- Signal state has only four values: `ACTIVE`, `TRIGGERED`, `EXPIRED`, `INVALIDATED`; all transitions go through `core.lifecycle.transition`.
- BUY touches require a second cycle or high volume; SELL/STOP triggers are single point.
- Entry rows generate their protective stop and profit row together with the same parent chain.
- LOW market volume halves BUY size hints and, with a DOWN index, forces a zero-plan day.
- Push volume is intentionally low: point events and summary reports only, not analysis noise.
