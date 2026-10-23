# stock-agent

旧表字段映射与测试模块映射见 `MIGRATION_MAPPING.md`。

推送变少是设计不是故障：v6 只做人工触发的单向通知，不做定时自动推送，也不做按钮回传。所有执行/否决/T 档动作都通过 CLI 回执完成。

## v6 当前形态

- 新主链路在 `pointtable/`：四领域模型、四表 schema、点位生成器、生命周期、任务、报告、推送和统计。
- 保留 `theme_map`、`pointtable/data/akshare.py` 安全封装、`DataFreshnessGuard` 和脏数据闸门；决策层评分、调度、组合、回测引擎已移除。
- 指数与股票 K 线优先走腾讯日线；东财、新浪保留为兜底。腾讯请求强制 `trust_env=False`，避免 Windows 系统代理把请求注入到 `127.0.0.1:9`。
- 旧源码冻结在 `legacy/`，只作一个月回查归档，不参与新链路。
- `pushplus.mode` 默认 `shadow`：普通推送走 `test_token`；`cutover --yes` 会切回 `formal` 并走正式 token。正式链路入口只有 `python -m pointtable.cli`，旧的 `python -m src.main` 已删除。

## 运行

```bash
python -m pointtable.cli --db pointtable/data/pointtable.db receipt <signal_id> --verdict executed --size-filled 100
python -m pointtable.cli --db pointtable/data/pointtable.db veto <signal_id>
python -m pointtable.cli --db pointtable/data/pointtable.db stats
python -m pointtable.cli --db pointtable/data/pointtable.db holdings
python -m pointtable.cli --db pointtable/data/pointtable.db pool
python -m pointtable.cli --db pointtable/data/pointtable.db pool-add --date 2026-10-22 --symbol 300001 --theme AI --role candidate
python -m pointtable.cli --db pointtable/data/pointtable.db pool-audit
python -m pointtable.cli --db pointtable/data/pointtable.db migration-status
python -m pointtable.cli --db pointtable/data/pointtable.db auction --date 2026-10-22 --pattern neutral
python -m pointtable.cli --db pointtable/data/pointtable.db watch --price 10.1 --second-cycle --volume-ratio 1.3 --broken-id <signal_id>
python -m pointtable.cli --db pointtable/data/pointtable.db settle --market-json pointtable/data/evening-2026-10-22.json --close-prices-json close.json --volume-ratios-json volume.json --break-contexts-json break.json
python -m pointtable.cli --db pointtable/data/pointtable.db restore --close-price 10.1
python -m pointtable.cli --db pointtable/data/pointtable.db evening --input evening.json
python -m pointtable.cli --db pointtable/data/pointtable.db market-input --date 2026-10-22 --output evening.json
python -m pointtable.cli --db pointtable/data/pointtable.db next-routine
python -m pointtable.cli --db pointtable/data/pointtable.db t-size <signal_id> --choice same
python -m pointtable.cli --db pointtable/data/pointtable.db report --date 2026-10-21 --output reports/dry-run.md
```

影子验证对比“应有信号”和“实际落库信号”，不写信号状态：

```bash
python -m pointtable.cli --db pointtable/data/pointtable.db shadow-run --date 2026-10-22 \
  --expected-id 2026-10-22-300001-first_move_5d \
  --expected-id 2026-10-22-300001-first_move_5d-z
```

复制 `pointtable/config/push.yaml.example` 为 `pointtable/config/push.yaml`；配置 `pushplus.test_token` 后，可加 `--push-test` 把影子结果发到测试通道。正式 token 不入库。

报告也只在明确执行命令时推送；`push-file` 是单向通知，不等待、不接收任何交互回传：

```bash
python -m pointtable.cli --db pointtable/data/pointtable.db push-file \
  --title "2026-10-22 复盘" \
  --file reports/2026-10-22.md \
  --channel test
```

## 行情数据

`market-input` 会读取池内票、指数量能/趋势、个股 K 线、板块 K 线、监管名单和龙虎榜状态，生成 `evening --input` 可直接使用的 JSON；它同时输出 `stop_zones`，供 15:05 清点执行 Z 滚动重算：

```bash
python -m pointtable.cli --db pointtable/data/pointtable.db market-input --date 2026-10-22 --output pointtable/data/evening-2026-10-22.json
python -m pointtable.cli --db pointtable/data/pointtable.db tail-snapshot --date 2026-10-22
python -m pointtable.cli --db pointtable/data/pointtable.db evening --input pointtable/data/evening-2026-10-22.json
python -m pointtable.cli --db pointtable/data/pointtable.db report --date 2026-10-22 --market-json pointtable/data/evening-2026-10-22.json --output reports/2026-10-22.md
```

`evening --input` 额外支持 `holdings` 数组；每行需要 `symbol`、`parent_id`、`trend`、`stop_zone`、`support_zone`、`resistance_zone`。UP 趋势生成 STOP/T_BUY/T_SELL，DOWN 趋势只生成 STOP，`volatility_exceeded=true` 会跳过 T 行。

`settle` 的观察哨窗口、放量站回阈值和节假日默认读取 `config.yaml`；上面三个 JSON 文件可覆盖收盘价、量比和破位上下文。`watch` 会自动使用同一 `retest_volume_min`。同票多策略不去重；活跃 `retest_confirm` 期间不生成该票 BUY。

`market-input` 会回溯最新 ENTER 链：没有触发 STOP 时用 `first_move_5d`；触发后下一档切 `first_move_10d` 并携带旧 ENTER 作为 `parent_id`；达到两层链深后输出 `reentry_limit` 拒绝。

个股 K 线经过 `DataFreshnessGuard` 检查；监管和龙虎榜结果会写入每只池内票的 `premise_facts`。

`pool.role` 区分 `holding/index/sector/candidate`；旧库自动补列，存量默认 `holding`。`holdings` 和报告只读取 `holding`，`market-input` 会按 `watchlist_limit` 和 priority 截断全部池内票。

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

迁移后检查 `migration-status`；它会核对正式推送、迁移审计、旧信号冻结和池子板块归属。用户已裁决跳过 7 天稳定观察；2026-10-22 切流已完成，旧源码回查保留到 2026-11-22。

## 测试

```bash
python -X utf8 -m pytest -q -p no:tmpdir -p no:cacheprovider --ignore=legacy --ignore=pytest-basetemp --ignore=tmp6zd291xl --ignore=tmpv7dfstrn
```

验收重点：均线锚点误差小于 0.5%，影子期无漏报，切流后旧信号全量失效，两层统计可正常产出。
