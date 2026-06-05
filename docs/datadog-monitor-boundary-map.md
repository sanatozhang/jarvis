# Datadog 原生 Monitor 能力边界（实测 2026-06-05）

对照 `coreguard` 自建告警逻辑，逐条记录 Datadog 原生 Monitor 能/不能做。
全部结论由 `POST /api/v1/monitor/validate`（只校验不创建，零副作用）+ 真实创建 3 个 muted monitor 实测得出。

## 探索方法

- **validate 端点是关键工具**：`POST /api/v1/monitor/validate` 校验语法返回 `{}`(200) 或错误(400)，不产生任何 monitor，适合迭代 query 语法。
- query 字符串里的比较阈值必须与 `options.thresholds.critical` **数值一致**，否则 400（`Alert threshold (X) does not match that used in the query (Y)`）。
- validate 只校验语法，**不校验 metric 是否有数据** —— 需另用 `GET /api/v1/query` 确认数据存在。

## 逐条能力对照

| coreguard 自建能力 | Datadog 原生对应 | 实测结论 | 备注 |
|---|---|---|---|
| **SHoW 同周同时对比** | formula 内 `week_before()` | ✅ **可替代** | `change(formula(...),last_1w)` → 400（change 不能包 formula）；改 `现值 - week_before(现值)` → 200。这是 SHoW 的原生等价物 |
| **多 query RUM formula**（Crash-free %） | `rum alert` + `formula(...)` + `options.variables` | ✅ **可做** | 子查询（cardinality/count + search）定义在 `options.variables`，formula 引用变量名。可经 API 建，无需 UI |
| **绝对阈值**（Hang Rate） | metrics formula `query alert` | ✅ **可做** | dashboard 的 cutoff_min/cutoff_max formula 直接 inline 进 query，validate 200 |
| **趋势异常检测** | `anomalies(<metric>, 'agile', N, seasonality='weekly')` | ⚠️ **仅限真实 metric** | `anomalies()` **不接受** `rum(...)` query（400）；只能套 metric query。且该 metric 必须有数据 |
| `min_users` 样本量地板 | composite monitor 或 query 内嵌过滤 | ❌ **无优雅原生方案** | 单 monitor 一条 query，无法像 coreguard 那样另查一次 distinct-user 做门禁。需 composite（A 突破 && B sessions>N），较繁琐。本次未实现 |
| N=2 连续 breach 防抖 | `options.threshold_windows`（anomaly）/ 评估窗口 | ✅ 内置 | anomaly 用 `trigger_window`/`recovery_window`；阈值类用评估窗口长度 |
| P0/P1/P2 分级 | `priority`(1/3/5) + `tags` | ✅ 内置 | |
| dedup / renotify / 自动 resolve | `options.renotify_interval` / 自动恢复 | ✅ 内置，比自建好用 | coreguard 自己维护 dedup_window，原生免费给 |
| RUM 入仓延迟缓冲 | `options.evaluation_delay` | ✅ 内置 | 设 900s（15min），对齐 coreguard hourly_watch 经验 |

## 关键边界结论

1. **SHoW 能原生替代**，但不是用 `change(...,last_1w)`（它不能作用于 formula），而是 formula 内 `week_before()` 做差。这是本次最重要的发现。
2. **复杂 RUM 多 query formula 能经 API 建**（`options.variables`），不必依赖 UI 导出——推翻了设计阶段"头号边界可能必须 UI"的预判。
3. **anomaly 是 metric-only**：dashboard 里基于 RUM event 的指标（如 `pc95(@resource.duration)`）没有对应标准 metric（`rum.resource.duration` 查询返回 0 series），无法直接做 anomaly。要做 anomaly 得先有沉淀成 metric 且有数据的指标（如 `rum.measure.session.time_spent` / `rum.measure.error.hang.duration`，实测均有数据）。
4. **`min_users` 样本量地板是原生的真短板**——这是 coreguard 仍有价值的地方之一。

## 三个样板 monitor 最终状态（真实创建，全部 muted）

| 样板 | monitor id | 类型 | query 关键 | overall_state |
|---|---|---|---|---|
| Hang Rate (threshold) | 291314589 | query alert (metrics formula) | inline cutoff formula `> 1.2e9` | OK |
| Crash-free (change/SHoW) | 291314586 | rum alert + variables | `现值 - week_before(现值) < -0.5` | OK |
| Session 时长总量 (anomaly) | 291314594 | query alert | `anomalies(session.time_spent, weekly) >= 1` | OK |

- 三者均 `options.silenced = {"*": None}`（静音，不发告警），`overall_state=OK`（正常评估、无 query 报错）。
- tag 统一 `source:coreguard`，可用 `python scripts/datadog_monitor.py list` 列出。
- 通知句柄 `@sanato.zhang@plaud.ai` 已写入 message；因静音未实际投递，**取消静音（`muted_on_create:false` 重 sync 或 UI unmute）后即生效**。未强制制造触发以免打扰。

## 结论：原生能替代 coreguard 哪些 / 不能替代哪些

**能替代**：绝对阈值、SHoW 周同比（week_before）、多 query RUM formula、分级/标签、去重/重通知/自动恢复、入仓延迟缓冲、趋势异常（限有数据的 metric）。这些原生做得比自建省心。

**不能（或不优雅）替代**：
- `min_users` 这类"先查样本量再决定是否告警"的门禁逻辑（需 composite，繁琐）。
- 基于 RUM event 但未沉淀成 metric 的指标做 anomaly。
- def yaml 注释会被 sync 回写 id 时的 yaml round-trip 抹掉（工具侧小坑，边界文档保留结论即可）。

**判断**：核心阈值/同比/分级类告警可逐步迁到原生 Monitor 降低维护成本；coreguard 在"样本量门禁 + 多指标聚合飞书卡片 + 早报"这些组合编排上仍有不可替代价值。建议互补，而非全量替换。
