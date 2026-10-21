# B → v3.1 迁移方案（v2.0 终版）

## 0. 裁决标准（迁移期间不再变更）


## 1. 最终处置清单

**留（平移）：**

| 资产 | 落点 | 备注 |
|---|---|---|
| akshare 封装（重试/缓存） | datafeed/akshare_client.py | 仅改包路径 |
| DataFreshnessGuard | datafeed 出口统一断言 | |
| 脏数据闸门 | datafeed | "尊重成交量"的前提是量是真的 |
| 量能外推 | core/volume.py | 并入 |
| 财报事件日历 | 数据层 | premise 校验输入（"规避英伟达财报"） |
| theme_map.yaml | config/，薄板块层归属依据 | 股票→单一主板块，人工确认 |
| 回执 trade_feedback | receipts 表 + cli.py | **交互一字不改** |
| 30/50笔统计 | stats 信号质量层 | 统计对象 strategy→strategy_id |
| trade_logs | journal 表 | 迁移脚本直灌 |
| TimingEngine 调度与轮询 | 盘中链路 | **机制保留，回调替换，频率分段**（见 §3.4） |

**退役（逻辑吸收）：**

| 模块 | 吸收方式 |
|---|---|
| XYZW 假说门 | premise 文本快照 + strategies.yaml 的 y/z/w 字段 |
| 配对出场 Z/W | 策略配置的 z/w 字段 |
| 再入场 max 2 次 | on_stop 链（破5→10→20，最多2次） |
| EV 闸门 | 一个 if：满30笔且滚动期望≤0 → EV_NEG 仅记录不推送 |
| position_state 状态机 | 仓位布尔推算（回执驱动） |
| arbiter | "STOP 排最前"一句排序 |

**删（无赦免）：**

板块 S1–S3 规则与同花顺31次调度、LLM 调用、基本面第七问、估值透镜、防守追强三重门、RRR 钳制、风险乘数引擎、振幅降档、同板块并发上限、position_budget、解禁日历。


