# Datadog 原生 Monitor 能力边界探索 — 设计文档

- 日期：2026-06-05
- 作者：sanato
- 状态：设计待评审

## 1. 背景与动机

`jarvis/backend/app/coreguard/` 已有一套**自建**的核心指标告警系统：每小时拉 Datadog
dashboard `4h8-qff-zra` 的指标，在 Python 里做 SHoW（同周同时段）对比、阈值判定、样本量
地板、N=2 防抖，再自己发飞书卡片。这套逻辑成熟但**维护成本高**（轮询、判定、通知全自建）。

本次目标：**单独探索 Datadog 原生 Monitor 的报警能力与边界**，搞清楚原生能力能替代
coreguard 自建逻辑里的哪些部分、哪些做不到。**纯 Datadog 探索，不涉及飞书。**

非目标：本次不迁移 coreguard、不下线 coreguard、不做 Datadog→飞书通道。coreguard 继续照常跑。

## 2. 产出（4 项，skill 为第一产出）

1. **`datadog-monitor-builder` skill**（第一产出）— 放 `Plaud2/.cursor/skills/`。
   必须能真正跑通：别人喂一个「指标 query + 检测意图 + 阈值」，跑 skill 就能在 Datadog
   建出一个真 monitor。
2. **monitors-as-code 代码层** — 放 `jarvis`。Datadog Monitors API 薄客户端 + 定义文件 + sync 脚本。
3. **3 个真实 Monitor** — 在 Datadog 上跑起来，各覆盖一种检测类型，且**由 skill 本身创建**（吃狗粮）。
4. **能力边界对照文档** — coreguard 每条能力 → Datadog 原生怎么做 / 做不到。

## 3. 架构与组件

```
jarvis/
  backend/app/coreguard/monitors/
    client.py          # Datadog Monitors API 薄客户端（create/update/get/list/mute）
                       # 复用 datadog_scalar.py 的 httpx + CRASHGUARD_DATADOG_* key 模式
    builder.py         # def(yaml) → Datadog monitor payload 的纯函数（无网络，易单测）
    defs/
      crash_free_sessions.change.yaml   # 样板①
      hang_rate.threshold.yaml          # 样板②
      api_latency_p95.anomaly.yaml      # 样板③
    sync.py            # 读 defs/*.yaml → builder → 幂等 create/update（按回写进 yaml 的 id）
  scripts/datadog_monitor.py            # CLI：sync / list / dry-run / mute
docs/superpowers/specs/2026-06-05-datadog-native-monitors-design.md   # 本文档
docs/datadog-monitor-boundary-map.md   # 能力边界对照
Plaud2/.cursor/skills/datadog-monitor-builder/SKILL.md
```

**数据流**：`defs/*.yaml`（人写的监控意图）→ `builder.py` 构造 payload → `client.py`
`POST/PUT /api/v1/monitor` → 返回的 `id` 回写进 yaml（幂等键，再次 sync 走 update）。

**单一职责拆分**：`client.py` 只管 HTTP 与鉴权；`builder.py` 只管 yaml↔payload 转换（纯函数）；
`sync.py` 只管幂等编排；CLI 只管参数解析。

## 4. 三个样板 Monitor 规格

| | 样板① Change Alert | 样板② Threshold | 样板③ Anomaly |
|---|---|---|---|
| 指标 | Crash-free sessions (P0) | Hang Rate (P0) | API 延迟 P95 (P1) |
| Datadog 类型 | `query alert` + `change(avg(last_15m),last_1w)` | `metric alert` / `rum alert` 绝对阈值 | `query alert` + `anomalies(..., 'agile', seasonality='weekly')` |
| 阈值 | 跌 ≥ 0.5pp（同 metrics.yaml v3） | > 1.2e9 ns/h（dashboard red line） | anomaly 偏离带 |
| 对标 coreguard | SHoW 同周同时对比（最直接等价物） | `absolute_threshold` | 无（高级款） |
| 边界看点 | RUM formula 能否塞进 change()；`last_1w` 是否等价 SHoW | 最简单，预期完全可行 | 季节性调参难度、误报率 |

monitor 的 query 从 dashboard widget 现成的 `queries + formula` 推导（coreguard
`dashboard_loader` 已能抽）。**Crash-free 这种 RUM formula 能不能在 monitor 里复现，是头号边界。**

## 5. skill 设计：`datadog-monitor-builder`

**触发词**：「建 Datadog 监控」「加个告警监控」「create datadog monitor」「新增 Datadog Monitor」

**skill 固化的知识**：
- Monitors API 用法：端点 `https://api.{site}/api/v1/monitor`，鉴权头 `DD-API-KEY` /
  `DD-APPLICATION-KEY`，key 从 `.env` 的 `CRASHGUARD_DATADOG_*` 取（复用现有模式）。
- 三种类型模板 + **何时用哪种**：突变看绝对值→threshold；周期性指标看同比→change；趋势异常→anomaly。
- 组织约定：命名 `[coreguard][P0] <指标> <类型>`、tags（`source:coreguard tier:p0`）、
  priority 映射、通知目标（POC：email 到 sanato.zhang@plaud.ai）。
- 从 dashboard widget 推导 query 的方法（复用 `dashboard_loader` / `metrics.yaml`）。
- monitors-as-code：写 def yaml → 跑 `scripts/datadog_monitor.py sync` → 回写 id。
- **安全护栏**：新建默认静音（`options.muted` 或不 @人），先在 UI 验证评估正常，再开通知，
  避免上来就误报轰炸。

**skill 工作流**：①确认 指标+检测意图+阈值 → ②选类型 → ③生成 def yaml → ④dry-run 看 payload
→ ⑤sync 创建（默认静音）→ ⑥UI 验证评估 → ⑦开通知。

## 6. 通知

POC 阶段：Datadog 原生 **email 通知到 sanato.zhang@plaud.ai**，或先在 Datadog UI 看评估状态。
保持纯 Datadog，无外部通道。

## 7. 能力边界对照（待文档实测填充）

| coreguard 自建能力 | Datadog 原生对应 | 预判边界 |
|---|---|---|
| SHoW 同周同时对比 | `change(avg(last_15m), last_1w)` | `last_1w` 是否真等价"上周同时刻" |
| Crash-free % 等 RUM formula | RUM/metric monitor query | 多 query + formula 能否塞进 monitor（头号边界） |
| `min_users` 样本量地板 | composite monitor（A 突破 && B sessions>N）或把门槛写进 query | 真难点，可能不优雅 |
| N=2 连续 breach 防抖 | "for the last N windows" 评估窗 | 内置，预期可行 |
| P0/P1/P2 分级 | monitor priority + tags | 内置 |
| dedup / renotify / 自动 resolve | 内置通知设置 | 内置，预期比自建好用 |
| 趋势异常（coreguard 无） | anomaly detection (weekly) | 季节性调参、误报率 |

## 8. 测试

- 单测：`builder.py` 的 `def yaml → payload` 纯函数（无网络，覆盖 3 种类型 + 边界字段）。
- dry-run 模式：构造 payload 打印不 POST。
- 手验：3 个 monitor 出现在 Datadog、能正常评估、触发时收到邮件。

## 9. 验收标准

- [ ] 至少 1 个（目标 3 个）样板 monitor 是**通过跑 skill** 创建的，能在 Datadog UI 看到并正常评估。
- [ ] skill 自洽：喂一个新指标能照流程建出新 monitor（非样板也能）。
- [ ] 触发条件满足时收到 Datadog email 告警。
- [ ] `builder.py` 单测通过。
- [ ] 边界对照文档据实测填完 §7 表格。

## 10. 明确不做（out of scope）

- ❌ Datadog→飞书 / 任何外部通道。
- ❌ 把 22 个 alert_enabled 指标全量搬过去。
- ❌ 下线 / 改动现有 coreguard。
