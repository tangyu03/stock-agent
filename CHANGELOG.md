# Changelog

## Unreleased

- Aligned the runtime config layer to the repository-root `config/` directory described by `fangan.md`.
- Moved package-local model and migration tests into the root `tests/` directory.
- 2026-10-27：`pointtable/jobs` 收敛到五个核心任务文件；数据读取、计划生成、尾盘快照、调度与统计分别移入 `db/session`、`core/generator`、`restore_check`、`cli` 与 `stats`；全量测试通过。
- 2026-10-27：修复审计暴露的投影与清点问题：候选只取信号日、破位/判决只看报告日、同日 T 到期、池内/候选区间重合交叉提示；收盘清点可用当日高低价兜底触发 watch 漏触。
- 2026-10-27：候选区增加尾盘可达性标注：盘中穿越但收盘未回标不可达；未穿越但现价偏离只标未到位，不提前删除次日候选。
