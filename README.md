# stock-agent

推送变少是设计不是故障：v6 点位表只推送 ENTER、Z、W、T、复牌确认与到期提醒，不再推送分析流水。

## v6 当前形态

- 新主链路在 `pointtable/`：四领域模型、四表 schema、点位生成器、生命周期、任务、报告、推送和统计。
- 保留 `theme_map`、`pointtable/data/akshare.py` 安全封装、`DataFreshnessGuard` 和脏数据闸门；决策层评分、调度、组合、回测引擎已移除。
- 旧源码冻结在 `legacy/`，只作一个月回查归档，不参与新链路。
- `pushplus.mode` 默认 `shadow`：普通推送走 `test_token`；`cutover --yes` 会切回 `formal` 并走正式 token。

## 运行

```bash
python -m pointtable.cli --db pointtable/data/pointtable.db receipt <signal_id> --verdict executed --size-filled 100
python -m pointtable.cli --db pointtable/data/pointtable.db veto <signal_id>
python -m pointtable.cli --db pointtable/data/pointtable.db stats
python -m pointtable.cli --db pointtable/data/pointtable.db holdings
python -m pointtable.cli --db pointtable/data/pointtable.db pool
python -m pointtable.cli --db pointtable/data/pointtable.db pool-audit
python -m pointtable.cli --db pointtable/data/pointtable.db auction --date 2026-10-22 --pattern neutral
python -m pointtable.cli --db pointtable/data/pointtable.db watch --price 10.1 --second-cycle --volume-ratio 1.3
python -m pointtable.cli --db pointtable/data/pointtable.db settle
python -m pointtable.cli --db pointtable/data/pointtable.db restore --close-price 10.1
python -m pointtable.cli --db pointtable/data/pointtable.db evening --input evening.json
```

影子验证对比“应有信号”和“实际落库信号”，不写信号状态：

```bash
python -m pointtable.cli --db pointtable/data/pointtable.db shadow-run --date 2026-10-22 \
  --expected-id 2026-10-22-300001-first_move_5d \
  --expected-id 2026-10-22-300001-first_move_5d-z
```

复制 `pointtable/config/push.yaml.example` 为 `pointtable/config/push.yaml`；配置 `pushplus.test_token` 后，可加 `--push-test` 把影子结果发到测试通道。正式 token 不入库。

## 切流

```bash
python -m pointtable.cli --db pointtable/data/pointtable.db cutover --yes
```

切流会：

1. 确认正式推送 token 已配置。
2. 把所有 `ACTIVE/TRIGGERED` 旧信号置为 `EXPIRED(status_reason=system_migration)`。
3. 在 journal 记录迁移审计；重复执行是幂等的。

回查迁移前信号（保留一个月）：

```bash
python -m pointtable.cli --db pointtable/data/pointtable.db --legacy
```

## 测试

```bash
python -X utf8 -m pytest -q -p no:tmpdir -p no:cacheprovider --ignore=legacy --ignore=pytest-basetemp --ignore=tmp6zd291xl --ignore=tmpv7dfstrn
```

验收重点：均线锚点误差小于 0.5%，影子期无漏报，切流后旧信号全量失效，两层统计可正常产出。
