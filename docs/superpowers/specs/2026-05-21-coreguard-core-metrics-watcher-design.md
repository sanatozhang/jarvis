# Coreguard — Core Metrics Hourly Watcher

**Status**: Draft for review
**Date**: 2026-05-21
**Owner**: sanato
**Related ADRs**: 参照 `docs/adr/0001-crashguard-isolation.md`（同一隔离合约思路）

---

## 1. 背景与动机

Crashguard 已闭环了「崩溃 → 分析 → 自动 PR」。但 App 健康度不只看 crash —— 还有 60+ 业务核心指标（成功率、延迟、计数）藏在 Datadog dashboard `app-all-in-one-core-metrics`（id `4h8-qff-zra`）。当前没有自动监控这些指标：

- 工程师靠手动看 dashboard，遗漏滞后
- "vs Previous Week" 对比要人眼比对
- 出问题时通常用户先发现

**目标**：把这个 dashboard 的指标做成小时级自动监控 + SHoW（Same Hour-of-Week）对比 + 飞书告警闭环，让"出现异常立刻有人接、可追、可关"。

## 2. 目标 / 非目标

### 目标 (MVP)

- ✅ 拉取 Datadog dashboard `4h8-qff-zra` 的 40 个 `query_value` widget，小时级监控
- ✅ 每个指标与「上周同 weekday 同小时」对比，超阈值触发告警
- ✅ 三层分层：**P0**（11 个，立即 @ oncall）/ **P1**（约 20 个，摘要卡，不 @）/ **P2**（其他，只入库不告警）
- ✅ 告警闭环 5 件套：去重 / Ack / 升级 / Oncall 路由 / Resolved 自检
- ✅ 双频率 Crash-free：10min 快路（突发尖峰）+ 1h 慢路（持续恶化），下线 `crashguard.core_metric_alerter`
- ✅ `coreguard` 与 `crashguard` 独立模块，未来可拆分（同 ADR-0001 模式）

### 非目标

- ❌ MVP 不做前端 UI（v2 加 `/coreguard` 页面）
- ❌ 不做 sunburst / toplist / bar_chart 类 widget（分布/归因，无法用阈值表达；仅入库供日报用）
- ❌ 不做 `crashguard.hourly_alerter` 迁移（per-issue 维度，与 coreguard 整体维度正交，物理上不重叠 —— 见 §11 决策记录）
- ❌ 不做 anomaly detection / 自适应基线（v2）

## 3. 模块结构

```
backend/app/coreguard/
  __init__.py
  CLAUDE.md                 # 模块文档 + 隔离合约
  config.py                 # CoreguardSettings (env 前缀 COREGUARD_)
  models.py                 # 4 张表 (coreguard_* 前缀)
  migrations.py             # ensure_columns 增量列
  metrics.yaml              # 指标白名单 + 分层 + per-metric 阈值
  api/
    __init__.py
    coreguard.py            # /api/coreguard router
  services/
    __init__.py
    datadog_scalar.py       # POST /api/v2/query/scalar 薄封装
    dashboard_loader.py     # 启动时拉 dashboard JSON + 与 metrics.yaml 校验
    metric_watcher.py       # 主循环：fetch → SHoW compare → 入库 → emit alert
    threshold.py            # pp / pct 判定 + 防抖（连续 N 点）
    show_baseline.py        # SHoW（上周同小时）+ fallback rolling 均值
    crashfree_fast.py       # Crash-free 10min 快路（迁自 crashguard）
    feishu_alerter.py       # 卡片渲染（含 ack 按钮）+ 发飞书
    lifecycle.py            # 升级判定 + resolved 自检
    oncall_resolver.py      # 调 jarvis /api/oncall 拿当周值班
    alert_dedup.py          # 去重抑制（同 metric 6h 内不重发）
    job_heartbeat.py        # 调度心跳（同 crashguard 模式）
  workers/
    __init__.py
    scheduler.py            # 3 个 cron 任务
backend/tests/coreguard/
  test_threshold.py
  test_show_baseline.py
  test_lifecycle.py
  test_dashboard_loader.py
  test_e2e_alert_flow.py
```

### 隔离合约（CoreguardCLAUDE.md 内）

#### 禁止
1. ❌ `from app.crashguard.* import ...`（保持 coreguard 未来可独立拆分）
2. ❌ `from app.models import ...`（仅允许 `app.db.database.get_session`）
3. ❌ SQL join 到 `crash_*` 或 jarvis 其他业务表
4. ❌ 把 coreguard 字段塞进 jarvis 全局配置（独立 `coreguard:` 段）

#### 允许的对外耦合点（3 个）
| 函数 / API | 用途 |
|---|---|
| `app.services.feishu_cli.send_message` / `patch_card` | 飞书群消息 + 卡片 patch（同 crashguard 已有耦合点） |
| `GET /api/oncall/current`（HTTP 调用，不 import） | 拿当周值班 user_id；通过 HTTP 而非 Python import，保未来拆分时只换 base URL |
| `app.db.database.get_session` | 共用 connection pool |

Datadog API 是外部第三方，不计为耦合点。

#### 防腐
- 扩展 `backend/.importlinter`，新增 contract: `coreguard-isolation`
- pre-commit / CI 跑 `lint-imports`

## 4. 数据模型（DB 表，前缀 `coreguard_`）

### 4.1 `coreguard_metric_snapshot`

每小时每指标一行（含 P0/P1/P2 全部），用于历史回看 + 防抖判定。

| 列 | 类型 | 说明 |
|---|---|---|
| id | INT PK | |
| metric_key | TEXT | 与 metrics.yaml 中 key 对应 |
| window_start | DATETIME | UTC 整点（如 2026-05-21 06:00:00），即 `[start, start+1h)` |
| value | FLOAT | 当前窗口聚合值（formula 计算后） |
| baseline_value | FLOAT NULL | SHoW 基线（上周同小时） |
| baseline_source | TEXT | `show` / `rolling_7d_fallback` / `none` |
| change | FLOAT NULL | `value - baseline_value`（pp 类）或 `(value-baseline)/baseline` (pct 类) |
| sessions_count | INT NULL | 当前窗口 sessions 数（用于 min_baseline 守门） |
| breached | BOOLEAN | 单点判定是否超阈 |
| tier | TEXT | `P0` / `P1` / `P2` |
| value_type | TEXT | `percent_pp` / `latency_pct` / `count_pct` |
| created_at | DATETIME DEFAULT CURRENT_TIMESTAMP | |

约束：`UNIQUE(metric_key, window_start)`
索引：`(metric_key, window_start DESC)`, `(window_start DESC)`
保留：90 天，超出由 daily cleanup cron 删除

### 4.2 `coreguard_alert`

告警生命周期表 —— 状态机：`firing` → `acked` / `escalated` → `resolved` / `false_positive`。

| 列 | 类型 | 说明 |
|---|---|---|
| id | INT PK | |
| metric_key | TEXT | |
| window_start | DATETIME | 触发窗口（与 snapshot 关联） |
| tier | TEXT | P0 / P1 |
| status | TEXT | `firing` / `acked` / `escalated` / `resolved` / `false_positive` |
| value | FLOAT | 触发时的指标值 |
| baseline_value | FLOAT | |
| change | FLOAT | |
| feishu_message_id | TEXT NULL | 飞书 message_id，用于 patch 卡片 |
| ack_user_id | TEXT NULL | 飞书用户 id |
| ack_at | DATETIME NULL | |
| escalation_count | INT DEFAULT 0 | 升级次数 |
| last_escalated_at | DATETIME NULL | |
| resolved_at | DATETIME NULL | |
| created_at | DATETIME DEFAULT CURRENT_TIMESTAMP | |
| updated_at | DATETIME | |

约束：`UNIQUE(metric_key, window_start)`（同窗口不重复入）
索引：`(status, created_at)`, `(metric_key, created_at DESC)`
保留：180 天

### 4.3 `coreguard_alert_dedup`

短期抑制：同指标 6h 内只发一张卡片，后续触发只 patch 不发新卡。

| 列 | 类型 | 说明 |
|---|---|---|
| id | INT PK | |
| metric_key | TEXT | |
| fired_hour | DATETIME | 首次触发时所在小时（UTC 整点） |
| feishu_message_id | TEXT NULL | 用于 patch |
| created_at | DATETIME DEFAULT CURRENT_TIMESTAMP | |

约束：`UNIQUE(metric_key, fired_hour)`
保留：30 天

### 4.4 `coreguard_job_heartbeat`

同 crashguard `crash_job_heartbeats` 模式，复制结构而非共享表。

| 列 | 类型 | 说明 |
|---|---|---|
| id | INT PK | |
| job_name | TEXT | `coreguard_hourly_watch` / `coreguard_crashfree_fast` / `coreguard_lifecycle_tick` |
| fired_at | DATETIME | |
| status | TEXT | `ok` / `failed` / `partial` |
| duration_ms | INT | |
| summary | TEXT | JSON：metrics_evaluated / alerts_fired / errors |
| error | TEXT NULL | |

索引：`(job_name, fired_at DESC)`

## 5. metrics.yaml 配置

放在 `backend/app/coreguard/metrics.yaml`，与代码同步部署。

```yaml
defaults:
  # 阈值
  pp_threshold: 1.0                 # 百分比类（成功率）默认 1.0 pp
  pct_threshold: 0.20               # 计数/延迟类默认 20%
  # 守门
  min_baseline_sessions: 200        # 基线时段 sessions < 此值 → skip（防低基数假警）
  consecutive_breach: 2             # 连续 N 个小时窗口都超阈才触发
  direction: worse_only             # 仅恶化方向告警
  # 升级 / 去重
  dedup_window_hours: 6             # 同 metric 6h 内不发新卡（patch 既有卡）
  escalate_after_minutes_1st: 60    # 60min 未 ack → 第二次发并 @ oncall
  escalate_after_minutes_2nd: 180   # 再 120min 未 ack → @ TL

# Dashboard 锁定（启动时 dashboard_loader 校验 widget 顺序未变）
dashboard:
  id: "4h8-qff-zra"
  expected_widget_count: 55

# 指标白名单（widget_id 来自 dashboard JSON 的 widget index）
metrics:
  # ===== P0：立即告警 + @ oncall =====
  - key: crash_free_sessions
    title: "Crash-free sessions"
    widget_id: 0
    tier: P0
    value_type: percent_pp
    direction: down_is_bad
    threshold: { pp: 0.5 }          # P0 收紧到 0.5pp
    dual_freq:                      # ← 双频率（迁自 crashguard.core_metric_alerter）
      enabled: true
      fast_window_min: 10
      fast_baseline_min: 60         # rolling 1h
      fast_threshold_pp: 0.3

  - key: crash_free_users
    title: "Crash-free users"
    widget_id: 1
    tier: P0
    value_type: percent_pp
    direction: down_is_bad
    threshold: { pp: 0.5 }
    dual_freq: { enabled: true, fast_window_min: 10, fast_baseline_min: 60, fast_threshold_pp: 0.3 }

  - key: android_anr
    title: "Android ANR"
    widget_id: 2
    tier: P0
    value_type: percent_pp
    direction: up_is_bad
    threshold: { pp: 0.5 }

  - key: hang_rate
    title: "Hang Rate"
    widget_id: 3
    tier: P0
    value_type: latency_pct        # 单位是 ms/hr，用 pct
    direction: up_is_bad
    threshold: { pct: 0.30 }

  - key: api_call_success_rate
    title: "API Call Success Rate"
    widget_id: 37
    tier: P0
    value_type: percent_pp
    direction: down_is_bad
    threshold: { pp: 0.5 }

  - key: login_success_rate
    title: "登录成功率"
    widget_id: 42                  # sunburst 的简化：取 query 出来的总成功率 scalar
    tier: P0
    value_type: percent_pp
    direction: down_is_bad
    threshold: { pp: 1.0 }

  - key: register_success_rate
    title: "注册成功率"
    widget_id: 46
    tier: P0
    value_type: percent_pp
    direction: down_is_bad
    threshold: { pp: 1.0 }

  - key: purchase_success_rate
    title: "商业化购买成功率"
    widget_id: 44
    tier: P0
    value_type: percent_pp
    direction: down_is_bad
    threshold: { pp: 1.0 }

  - key: ai_task_transcribe_success_rate
    title: "AI任务-转写成功率"
    widget_id: 39
    tier: P0
    value_type: percent_pp
    direction: down_is_bad
    threshold: { pp: 1.0 }

  - key: cloud_upload_success_rate_v2
    title: "云同步上传成功率V2"
    widget_id: 13
    tier: P0
    value_type: percent_pp
    direction: down_is_bad
    threshold: { pp: 1.0 }

  - key: audio_import_success_rate
    title: "音频导入成功率"
    widget_id: 16
    tier: P0
    value_type: percent_pp
    direction: down_is_bad
    threshold: { pp: 1.0 }

  # ===== P1：摘要卡，不 @ =====
  - key: memory_usage_mib
    widget_id: 4
    tier: P1
    value_type: count_pct          # 双向，但 worse_only 配合 direction → 只看 up
    direction: up_is_bad
    threshold: { pct: 0.20 }

  - key: cold_startup_p90
    widget_id: 7
    tier: P1
    value_type: latency_pct
    direction: up_is_bad
    threshold: { pct: 0.20 }

  # ...（共约 20 个 P1，由 dashboard inventory 工具一次性生成，列于 metrics.yaml 完整文件中）

  # ===== P2：只入库 =====
  - key: package_size
    widget_id: 12
    tier: P2
    value_type: count_pct

  # ...（其余指标，alert_enabled 默认 false）
```

启动时 `dashboard_loader` 校验：
1. 拉 `GET /api/v1/dashboard/4h8-qff-zra`
2. 与 `metrics.yaml` 中每个 `widget_id` 比对 title 是否吻合（防 widget 顺序变动导致 query 错位）
3. 提取每个 metric 的 `requests[0].queries` + `formulas[0].formula` 缓存到内存 → 直接喂给 Datadog scalar API
4. 不吻合 → log error + 启动时 `coreguard_enabled` 自动降级为 false（不阻塞 jarvis 启动）

### 热 reload

`POST /api/coreguard/reload-config` —— 重读 metrics.yaml + 重拉 dashboard JSON，验证通过则原子替换 in-memory 配置。

## 6. 告警生命周期（5 个闭环抓手）

```
┌────────────────────────────────────────────────────────────────────────┐
│                       coreguard_hourly_watch (cron: 5 * * * *)         │
│                                                                        │
│  for each metric in metrics.yaml (alert_enabled=true):                 │
│    cur = datadog_scalar(metric.query, now-1h .. now)                   │
│    base = datadog_scalar(metric.query, now-1h-7d .. now-7d)            │
│    snap = MetricSnapshot(... breached=threshold(cur, base, metric))    │
│    db.add(snap)                                                        │
│    if last N snapshots all breached:                                   │
│      emit_alert(metric, snap)                                          │
│                                                                        │
│  emit_alert(metric, snap):                                             │
│    [a] alert_dedup: if same metric fired in last 6h:                   │
│           patch existing feishu card (update 最新值 / 趋势小图) → return│
│    [b] render card with 2 buttons: "处理中" / "误报"                   │
│           POST 飞书 → 拿 message_id → write to alert_dedup + alert     │
│    [d] for P0: query GET /api/oncall/current → @ that user             │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│                  coreguard_lifecycle_tick (cron: */15 * * * *)         │
│                                                                        │
│  for each alert where status == firing:                                │
│    [c] if (now - created_at) > escalate_after_minutes_1st              │
│          and escalation_count == 0:                                    │
│        send 2nd card @oncall, escalation_count = 1                     │
│    elif (now - created_at) > escalate_after_minutes_2nd                │
│          and escalation_count == 1:                                    │
│        send 3rd card @TL, escalation_count = 2                         │
│                                                                        │
│  for each alert where status in (firing, acked):                       │
│    [e] cur = latest snapshot of this metric                            │
│    if not cur.breached for last 2 consecutive windows:                 │
│        patch card → "已恢复 ✅" + 标 resolved                          │
│        status = resolved, resolved_at = now                            │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│  POST /api/coreguard/ack (从飞书卡片按钮跳转)                          │
│  body: { alert_id, action: "ack" | "false_positive", user_id }         │
│                                                                        │
│  if action == "ack":                                                   │
│     alert.status = acked; alert.ack_user_id = user_id; ack_at = now    │
│     patch card → "处理中 by @user_id"                                  │
│  if action == "false_positive":                                        │
│     alert.status = false_positive                                      │
│     patch card → "已标记误报，本指标 24h 内不再告警"                   │
│     alert_dedup 表里 fired_hour 提到 +24h                              │
└────────────────────────────────────────────────────────────────────────┘
```

### 飞书卡片标题区分（避免与 crashguard 混淆）

- coreguard 卡片标题前缀：`[coreguard·业务]`
- crashguard.hourly_alerter 卡片：`[crashguard·issue]`
- crashguard 日报：维持原标题（区别明显）

## 7. 调度（3 个 cron）

| job | cron | 用途 | kill switch |
|---|---|---|---|
| `coreguard_hourly_watch` | `5 * * * *` | 主路：拉所有 alert_enabled 指标 + SHoW 对比 + 触发判定 | `coreguard_enabled` |
| `coreguard_crashfree_fast` | `*/10 * * * *` | Crash-free 10min 快路（迁自 crashguard） | `coreguard_crashfree_fast_enabled` |
| `coreguard_lifecycle_tick` | `*/15 * * * *` | 升级判定 + resolved 自检 | `coreguard_enabled` |

均通过 `coreguard_job_heartbeat` 写心跳，前端 / API 探针可见。

**多实例兜底**：复用 `scheduler_enabled` 同模式（只让一台机器跑 cron）+ DB UNIQUE 约束兜底。

## 8. 阈值与防抖算法

### 8.1 单点判定（`services/threshold.py`）

```python
def check_breach(snap: MetricSnapshot, metric_cfg: MetricConfig) -> bool:
    if snap.baseline_value is None:
        return False                       # 无基线不告警
    if snap.sessions_count < cfg.min_baseline_sessions:
        return False                       # 低基数 skip

    if metric_cfg.value_type == "percent_pp":
        delta_pp = snap.value - snap.baseline_value
        thresh = metric_cfg.threshold.pp
        if metric_cfg.direction == "down_is_bad":
            return delta_pp <= -thresh
        if metric_cfg.direction == "up_is_bad":
            return delta_pp >= thresh

    elif metric_cfg.value_type in ("latency_pct", "count_pct"):
        if snap.baseline_value <= 0:
            return False
        delta_pct = (snap.value - snap.baseline_value) / snap.baseline_value
        thresh = metric_cfg.threshold.pct
        if metric_cfg.direction == "down_is_bad":
            return delta_pct <= -thresh
        if metric_cfg.direction == "up_is_bad":
            return delta_pct >= thresh
```

### 8.2 防抖（连续 N 点）

```python
def should_emit_alert(metric_key: str, current_window: datetime, N: int = 2) -> bool:
    recent = db.query(MetricSnapshot)\
        .filter(MetricSnapshot.metric_key == metric_key,
                MetricSnapshot.window_start <= current_window)\
        .order_by(MetricSnapshot.window_start.desc())\
        .limit(N)\
        .all()
    return len(recent) == N and all(s.breached for s in recent)
```

### 8.3 SHoW 基线（`services/show_baseline.py`）

```python
def show_baseline_window(now_hour: datetime) -> tuple[datetime, datetime]:
    # 上周同 weekday 同 hour（UTC）
    start = now_hour - timedelta(days=7)
    end = start + timedelta(hours=1)
    return start, end

def fetch_baseline(metric_query, now_hour) -> Optional[float]:
    s, e = show_baseline_window(now_hour)
    val = datadog_scalar(metric_query, s, e)
    if val is None or math.isnan(val):
        # Fallback: 过去 7 天同小时均值
        return rolling_7d_same_hour_avg(metric_query, now_hour)
    return val
```

## 9. Datadog Scalar Client（`services/datadog_scalar.py`）

```python
async def query_scalar(
    queries: list[dict],          # 直接传 widget definition 的 queries 数组
    formula: str,                 # 直接传 formula
    start_ms: int,
    end_ms: int,
    template_vars: dict = None,   # {"os_name": "*", "version": "*"}
) -> Optional[float]:
    """POST /api/v2/query/scalar"""
    body = {
        "data": {
            "type": "scalar_request",
            "attributes": {
                "formulas": [{"formula": formula}],
                "from": start_ms,
                "to": end_ms,
                "queries": _resolve_template_vars(queries, template_vars),
            }
        }
    }
    resp = await http.post(f"https://api.{site}/api/v2/query/scalar",
                            headers={"DD-API-KEY": ..., "DD-APPLICATION-KEY": ...},
                            json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # 解析单 scalar：data.attributes.columns[0].values[0]
    return ...
```

错误处理：
- HTTP 429 / 5xx：指数退避重试 3 次（500ms / 1s / 2s）
- 持续失败：本轮该 metric 入库 `value=NULL, baseline_source='error'`，告警不触发
- 全部 metric 失败 → 整轮标 `coreguard_job_heartbeat.status='failed'`

## 10. 错误处理与可观测性

- **失败不阻断**：单 metric 失败只跳过该 metric，不影响其他
- **心跳监控**：3 个 cron 都写 `coreguard_job_heartbeat`，前端可见
- **API**：`GET /api/coreguard/jobs/status` 暴露 health 状态（同 crashguard 模式）
- **历史回看**：`GET /api/coreguard/metrics/{key}/history?hours=168` 返回过去 7 天 snapshot
- **告警列表**：`GET /api/coreguard/alerts?status=firing` 列出当前未关闭告警
- **Kill switch**：`coreguard_enabled` / `coreguard_crashfree_fast_enabled` / `coreguard_feishu_enabled`

## 11. crashguard 下线方案（Crash-free 路径整合）

### 步骤

1. **新增 coreguard 模块**（不动 crashguard 任何代码）
2. **数据迁移脚本**（一次性，`scripts/migrate_crashfree_to_coreguard.py`）：
   - 读 `crash_metric_snapshot` 表（10min 窗口的 Crash-free %）
   - 写入 `coreguard_metric_snapshot`，`metric_key='crash_free_sessions'` 或 `'crash_free_users'`，`window_start = window_start (10min 对齐保留)`
   - 标记 `baseline_source='migrated_from_crashguard'`
3. **下线 crashguard.core_metric_alerter**：
   - 删 `backend/app/crashguard/services/core_metric_alerter.py`
   - 删 `crashguard/workers/scheduler.py` 中的 `core_metric` cron 注册
   - 删 `CrashMetricSnapshot` / `CrashMetricAlert` model（迁移后留 30 天再删，给回滚窗口）
4. **更新 crashguard CLAUDE.md**：删除"7 个 cron"表中 `core_metric` 行

### 不动的

- `crashguard.hourly_alerter`：per-issue 维度，正交不重叠
- `crashguard.pipeline` / `analyzer` / `pr_drafter`：全部保留
- `crashguard` 飞书早晚报：保留（业务大盘 vs 单 issue 视角不同）

### 关键决策记录

**Q**: 为什么不把 hourly_alerter 也迁到 coreguard？
**A**: hourly_alerter 强依赖 `CrashIssue` 表（crashguard first-class 数据），且每告警按 `datadog_issue_id` 维度，维度与"整体业务指标" 正交。迁移成本是 ~2-3 天 + 违反隔离合约。**brainstorming 阶段已验证并通过用户拍板**。

## 12. 测试策略

### 单测
- `test_threshold.py` —— pp / pct 判定 + 方向性 + min_baseline 守门
- `test_show_baseline.py` —— SHoW 时间计算 + fallback rolling 均值
- `test_lifecycle.py` —— firing → acked → resolved 状态机 + 升级时机
- `test_dashboard_loader.py` —— 配置校验通过 / widget 顺序变动 / title 不匹配

### 集成测
- `test_e2e_alert_flow.py`：
  - mock Datadog scalar API（先返回正常值 → 切异常值 → 持续异常 N 点 → 切回正常）
  - mock 飞书 send / patch API
  - 验证：snapshot 入库 → 连续 2 点超阈 → emit alert → ack webhook → patch card → 下一窗口正常 → resolved patch

### 防腐
- 扩展 `backend/.importlinter` 新增 `coreguard-isolation` contract
- pre-commit / CI 强制 `lint-imports` 通过

## 13. 部署与配置

### env / .env 新增
```bash
COREGUARD_ENABLED=true
COREGUARD_CRASHFREE_FAST_ENABLED=true
COREGUARD_FEISHU_ENABLED=true
COREGUARD_DATADOG_API_KEY=${CRASHGUARD_DATADOG_API_KEY}      # 复用同一对
COREGUARD_DATADOG_APP_KEY=${CRASHGUARD_DATADOG_APP_KEY}
COREGUARD_DATADOG_SITE=datadoghq.com
COREGUARD_FEISHU_CHAT_ID=${CRASHGUARD_FEISHU_CHAT_ID}        # 复用 crashguard 群
COREGUARD_BACKEND_BASE_URL=...                                # ack webhook 跳转用
```

### config.yaml 新段
```yaml
coreguard:
  enabled: true
  feishu_enabled: true
  scheduler_enabled: true
  datadog:
    site: "datadoghq.com"
  dashboard_id: "4h8-qff-zra"
```

### Docker compose
- 复用 backend 容器，不新增 service
- `TZ=Asia/Shanghai`（已设）
- 挂载点：`/coreguard/metrics.yaml`（或直接打包进镜像，由于该配置变更频率低）

## 14. 安全与权限

- Datadog API key：复用 crashguard 已有的 readonly key
- 飞书 ack webhook：验签（飞书 event v2 sig）防伪造
- ack 操作记录用户 id，便于追溯
- `/api/coreguard/reload-config` 需要 admin role

## 15. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| Datadog widget 顺序变更导致 widget_id 错位 | 中 | 严重（指标错位拉错数据） | 启动时 `dashboard_loader` 校验 title；不匹配 → coreguard 自动降级 + 告警 ops |
| 阈值过松 / 过紧导致假警或漏警 | 高 | 中 | per-metric override + 上线后两周内调优（在 metrics.yaml 标记 `tunable: true`） |
| Datadog API quota 用尽 | 低 | 严重（所有指标拉不到） | 单实例每小时 ~60 次 scalar 调用，Datadog enterprise 配额内；监控 429 触发告警 |
| 飞书卡片刷屏 | 中 | 中 | 6h 去重 + 防抖 2 点；如仍超阈，触发"告警风暴抑制"日志 |
| 节假日 / 凌晨低基数假警 | 高 | 中 | `min_baseline_sessions=200` 守门；周末单独配阈值集（v2） |
| ack webhook 被刷 | 低 | 低 | 飞书 event 签名验签；rate limit 100/min |

## 16. v2 范围（不在 MVP）

1. `/coreguard` 前端页：指标清单 + 24h 趋势图 + alert 历史 + ack 按钮（与飞书一致）
2. 多 dashboard 支持：metrics.yaml 支持挂多个 dashboard_id
3. 自适应基线（anomaly detection）替代固定阈值
4. 工作日 / 周末分别配阈值
5. 告警知识库：每条 alert 关联 runbook 链接

## 17. 出 MVP 的验收标准

- ✅ 11 个 P0 指标小时级告警跑通，飞书卡片可见
- ✅ Ack / 升级 / Resolved 三态闭环验证（用真实数据 + mock 异常注入）
- ✅ 单测覆盖率 ≥ 80%
- ✅ `lint-imports` 通过
- ✅ `crashguard.core_metric_alerter` 安全下线（数据迁完 + cron 摘掉 + 30 天保留回滚窗口）
- ✅ `/api/coreguard/jobs/status` 三个 job 全 `ok`，连续 48h
- ✅ 灰度 1 周内只有真实异常触发告警，假警率 < 10%（人工标注校验）

## 18. 实施前需 ops 二次确认（不阻塞 design 通过）

以下为 brainstorming 阶段已定默认值，实施 sprint 启动前由 ops/owner 二次过目（非 design 留白）：

- **metrics.yaml P1/P2 完整清单**：MVP 实施第一步跑 `scripts/coreguard_inventory.py` 自动生成 → owner review tier/threshold per-metric → commit。design 阶段不必逐个穷举。
- **飞书 chat_id**：默认复用 crashguard 群（`COREGUARD_FEISHU_CHAT_ID=${CRASHGUARD_FEISHU_CHAT_ID}`，env 默认值即指向同群）。如需独立群，部署时改 env 即可。
- **升级路由**：第一次升级 @ `GET /api/oncall/current`，第二次升级 @ `coreguard.escalation_tl_user_id`（config.yaml 中显式指定的固定 user_id，默认即 owner 自己）。两次升级都走 `feishu_cli.send_message` 的 @ 语法。

## 附录 A：自动生成 metrics.yaml 完整清单

```bash
# scripts/coreguard_inventory.py
python3 backend/scripts/coreguard_inventory.py \
  --dashboard-id 4h8-qff-zra \
  --output backend/app/coreguard/metrics.yaml.generated
# 然后人工 diff 调整 tier / threshold per-metric override
```

输出格式：见 §5 示例，自动填充 widget_id / title / formula heuristic value_type。

## 附录 B：60 个 widget 完整 inventory（自 Datadog API 拉取，2026-05-21）

```
# query_value (40 个，主要监控目标)
 #0  Crash-free sessions             unit=%        formula=100 - ((query1 * 100) / query2)
 #1  Crash-free users                unit=%        formula=100 - ((query1 * 100) / query2)
 #2  Android ANR                     unit=%        formula=(sessionsWithANR / sessionCount) * 100
 #3  Hang Rate                       unit=ns/hr    formula=cutoff_max(...)
 #4  Memory Usage                    unit=        formula=memoryUsage
 #5  Refresh Rate                    unit=        formula=refreshRate
 #6  APP单次运行平均FPS              unit=        formula=a
 #7  Cold Startup p90                unit=ms      formula=query1
 #8-11 APP单次使用的卡顿次数 iOS/Android (p90/p75)
 #13 云同步上传成功率V2             unit=%       formula=100 * (q1/q2)
 #14 云下载/更新成功率
 #15 Websocket连接成功率
 #16 音频导入成功率
 #17 音频解码成功率
 #18 设备总解绑成功率
 #19 音频播放成功率
 #20 设备绑定成功率
 #21 强制解绑成功率（按SN去重）
 #22 App设备OTA传输成功率
 #23 App云端下载OTABin包成功率
 #24 Wi-Fi连接成功率（按平台）
 #25 Ask Add Note成功率
 #26 wifi 文件同步成功率
 #27 ble 文件同步成功率
 #28 wifi同步文件速度P75
 #29 分享导出成功率
 #30-34 首页/详情页加载延迟 P75/P90
 #36 API延迟P95(按接口)
 #37 API Call Success Rate
 #39 AI任务-转写成功率
 #40 AI任务-Add Note 成功率
 #43 Highlight页面渲染耗时 P75
 #45 Highlight页面打点成功率

# timeseries (3 个，不做单点阈值告警 → 入库供日报)
 #12 Package Size
 #35 API请求量(按接口)
 #53 Audio Upload Cloud Duration P50/P75/P95

# sunburst / toplist / bar_chart / query_table (12 个，不做阈值告警)
 #38 API失败归因
 #41 App版本分布
 #42 登录成功率（sunburst）→ 用 query scalar 抽出整体成功率监控
 #44 商业化购买成功率（sunburst） →同上
 #46 注册成功率（sunburst） → 同上
 #47 Onboarding Profile 完成率（sunburst）
 #48 Highlight打点失败归因（toplist）
 #49 注册失败原因top10（toplist）
 #50 登录失败原因top10（query_table）
 #51 商业化购买成功率 (sunburst, 重复)
 #52 Audio Upload Cloud Duration
 #54 云同步上传失败归因（toplist）
```
