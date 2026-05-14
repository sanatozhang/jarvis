# Crashguard hourly_alert 技术规格文档

**最后更新**：2026-05-14  
**实现路径**：`backend/app/crashguard/services/hourly_alerter.py`  
**配置路径**：`backend/app/crashguard/config.py` + `config.yaml` 段 `crashguard.hourly_alert`

---

## 1. 整体架构

### 1.1 三通道模型

```
hourly_alerter cron（5 */3 * * *）
         │
         ├─ 拉 Datadog 3h 窗口 issues（_fetch_hourly_events）
         ├─ 拉 top_user_version_by_platform ×2（24h 分桶 + 3h 分母，走缓存）
         ├─ 拉 24h 累计 issues（_fetch_24h_events，走缓存）
         │
         ▼ 对每个 issue 做版本分桶（classify_version）
         │
         ├─[bucket=new]──── 通道 1：新版本桶（灰度回归）
         ├─[bucket=main/legacy]─ 通道 2：大盘 SHoW-3h（现有主路）
         └─────────────────── 通道 3：全局新 crash 兜底（24h 累计，独立循环）
         
         合卡 dedup（优先级：1 > 3 > 2）
         shadow_mode 判定
         写入 crash_hourly_alerts（audit log）
         发飞书卡片（或影子静默）
```

### 1.2 Cron 时间

- **表达式**：`5 */3 * * *`（每 3 小时第 5 分钟，UTC）
- **设计原因**：Datadog ingest 延迟 3-5 分钟，第 5 分钟触发确保数据完整
- **数据时间窗口**：`[now_floor_3h - 3h, now_floor_3h]`（UTC 对齐到 00/03/06/09/12/15/18/21）

---

## 2. 通道 2：大盘 SHoW-3h（基础通道）

### 2.1 触发条件（AND 关系）

```
events_3h ≥ SHoW_baseline × (1 + 10%)     ← events% 上涨 > 10%
AND rate_3h ≥ rate_baseline                 ← crash rate 同步上涨（rate-AND-check）
AND events_3h ≥ 200                         ← events 绝对量底线（min_events_absolute）
AND sessions_3h ≥ 500                       ← 受影响会话底线（min_sessions）
AND SHoW_baseline ≥ 20                      ← 基线量足够（min_baseline_events）
AND issue_id NOT IN dedup_12h               ← 12h 内未告警过
```

### 2.2 SHoW 基线计算口径

| 优先级 | 来源 | 条件 |
|--------|------|------|
| P1 | `crash_hourly_snapshots`：上周同 weekday 同 3h 块（精确到秒±0） | 该行 events_count ≥ min_baseline（20）|
| P2 | rolling 过去 7 天同 hour 块均值 | P1 不满足时 fallback |
| 无基线 | skip（不告警）| P2 也无数据 |

**sessions 基线**：
- 优先用 SHoW snapshot 的 `sessions_count`
- SHoW snapshot 无 sessions_count（历史老数据，5/14 前）→ 取过去 14 天同 issue 所有 sessions_count > 0 的 snapshot 中位数（_fallback_sessions_baseline）
- 仍无 → 强制不告警（rate-AND-check 严格模式，宁缺勿误报）

### 2.3 Rate-AND-check 公式

```python
rate_now = events_3h / sessions_3h                # 当前 3h crash rate
rate_base = SHoW_events / SHoW_sessions           # 同期基线 crash rate
growth_rate = (rate_now - rate_base) / rate_base  # 相对增幅

# 两个条件均满足才告警：
assert events_growth > 10%
assert rate_growth > 0
```

**设计原因**：流量涨幅可能导致 events 绝对量增长（用户变多），rate 不涨不代表问题。单用 events% 易误报，加 rate AND 门显著降低噪声。

---

## 3. 通道 1：新版本桶（灰度回归专用）

### 3.1 版本分桶方式

```python
classify_version(issue_version, platform, top_user_version_24h)
```

| 结果 | 条件 | 走向 |
|------|------|------|
| `"new"` | semver(issue) > semver(top_user_version) | 通道 1 |
| `"main"` | semver(issue) == semver(top_user_version) | 通道 2 |
| `"legacy"` | semver(issue) < semver(top_user_version) 或无法解析 | 通道 2 |

**top_user_version 来源**：`DatadogClient.top_user_version_by_platform(window_hours=24)`，即 RUM session cardinality 24h 分 `(@os.name, @application.version)` group by，取每平台 users 最大的版本。

**版本号比较规则**：
- 格式 `3.16.0-634` → 取 `-` 前的 `3.16.0` 做 semver 比较，build 号忽略
- 实现：`version_util.parse_semver` 返回 `(major, minor, patch, suffix)`，只比较前三元素

### 3.2 触发条件

```
events_3h ≥ new_version_min_events (30)          ← 绝对量地板（灰度专用，低于大盘 200）
AND crash_users_3h / denom ≥ 0.5%               ← 用户占比信号
AND sessions_3h ≥ 500                            ← sessions 底线（同大盘）
AND issue_id NOT IN dedup_12h
```

**crash_users 口径**：events_3h（用 events 代理 users，Plaud RUM SDK 未设 `@usr.id`，`cardinality(@session.id)` 在 3h 颗粒度下成本高，events 是合理代理）

**用户占比分母（denom）校准**：
- P1：`top_user_version_by_platform(window_hours=3).users`——3h 窗口活跃用户，与 events_3h 时间颗粒度对齐
- P2 fallback：`top_user_version_by_platform(window_hours=24).users`——3h 数据缺失时（如新版本刚上线 < 3h）

**为什么不用 24h 分母**：夜间低流量时段 24h 累计 10000 用户 vs 3h 实际活跃 800 用户，用 24h 分母会让 user_rate 低估约 12 倍，高质量报警被吞掉。

**min_events_absolute 绕过**：通道 1 的 events 地板是 30（灰度期别样），全局的 `min_events_absolute=200` 在通道 1 之后才应用（仅对通道 2 生效）。这是一个 5/14 修复的 bug，之前通道 1 完全无效。

### 3.3 卡片标签

```
🔴 [新版本] 灰度异常
{issue_title}
版本: {version} | 首次出现: {first_seen_version}
3h events: {events_h} | sessions: {sessions_h} | user_rate: {user_rate_pct}%
denom_source: 3h | denom_users: {denom}
```

---

## 4. 通道 3：全局新 crash 兜底

### 4.1 设计动机

通道 1 只看新版本，通道 2 看老 issue 突增。**遗漏场景**：
- 老版本突然冒出来一个完全新的 crash（之前没有过）
- 跨版本的底层 crash（如系统 API 变化引起）

通道 3 与版本无关，只看"全网近 30 天是否首次出现 + 量级达标"。

### 4.2 触发条件

```
first_seen_at ≤ now - 30 天                    ← 近 30 天全网首现（new_window_days）
AND events_24h ≥ 150                            ← 24h 累计量达标
AND sessions_24h ≥ 300                          ← 24h 受影响会话达标
AND issue_id NOT IN dedup_12h
```

**first_seen_at 数据源（双路，API 优先）**：

| 优先级 | 来源 | 延迟 |
|--------|------|------|
| P1 | Datadog API 返回 `attributes.first_seen_timestamp`（实时） | ~5min |
| P2 | DB `CrashIssue.first_seen_at`（pipeline 写入） | ≤4h |

P1 优先确保漏报窗口从 ≤4h 缩到 ≤1h。同时 `first_seen_source` 字段记录来源（"api"/"db"），方便 audit。

**数据窗口**：24h（不同于通道 1/2 的 3h），走 DatadogCache TTL 6h。设计原因：新 crash 兜底看"量级"而非"突发"，24h 窗口抗噪能力更强。

### 4.3 卡片标签

```
🟠 [新 crash] 全网首现
{issue_title}
首次出现版本: {first_seen_version} | 首现时间: {first_seen_at}
24h events: {events_24h} | sessions: {sessions_24h}
first_seen_source: api|db
```

---

## 5. 共享机制

### 5.1 多通道合卡 dedup

同一 `issue_id` 在同一次 cron tick 内可能同时命中多个通道（如新版本 + 首现）。合卡优先级：

```
通道 1（新版本）> 通道 3（新 crash）> 通道 2（新增）> 通道 2（上涨）
```

同一 issue 只在最高优先级通道的 list 里保留，其余通道的该 issue 被移除。这样一张卡片里同一 issue 不会重复展示。

### 5.2 跨 tick dedup（dedup_12h）

```
dedup_hours: 12   # 同 issue 12h 内已告警过 → 本 tick 跳过所有通道
```

实现：从 `crash_hourly_alerts.alert_payload` 扫描过去 12h 内所有 alert 的 issue_id 集合，本 tick 内所有通道均跳过这些 ID。

**覆盖范围**：hourly_alerter 生成的所有告警（含通道 1/2/3）、日报 attention 列表（已 12h 内 hourly 告警的 surge 类不再出现）。

### 5.3 Shadow Mode（影子模式）

**目的**：新通道上线后先「试运行」24h，看 audit log 里的命中数是否合理，再真发飞书。

**判定逻辑**：
```
shadow_mode_active = True 当且仅当：
  (1) 至少有一个通道命中
  (2) 通道 2（new_items + surge_items）命中数 = 0
  (3) 所有命中通道都处于 shadow_mode=true
```

即：如果通道 2 有命中（大盘告警），即使通道 1/3 也是影子模式，仍然真发（通道 2 从不影子）。

**shadow_mode 激活时**：
- 写 `crash_hourly_alerts` audit 行（payload 包含 new_version/new_crash 完整数据）
- `result["shadow"]=True, result["alerted"]=False`
- **不发飞书**

**关闭影子模式**：把 `config.yaml` 的 `new_version.shadow_mode: false` + `new_crash.shadow_mode: false` 改完后，重启 backend 或等 uvicorn reload（`--reload` 模式会自动 pick up）。

### 5.4 min_sessions 底线

```
min_sessions: 500    # 过去 3h 受影响 sessions < 500 → 所有通道均跳过
```

注意：通道 3 用 `new_crash_min_sessions: 300`（独立配置），因为 24h 窗口 sessions 总量天然更大。

---

## 6. Datadog 数据接口

### 6.1 主要 API

| 接口 | 作用 | 对应字段 |
|------|------|---------|
| `DatadogClient.list_issues_for_window(start_ms, end_ms, tracks, query)` | 拉 3h/24h 内出现过的 fatal issues | `attributes.events_count`, `sessions_affected`, `first_seen_timestamp`, `version` |
| `DatadogClient.top_user_version_by_platform(window_hours=N)` | 每平台用户量最大的版本 | `{platform: {version, users}}`，users = RUM session cardinality |

### 6.2 DatadogCache（进程内 TTL 缓存）

| 缓存 key | 内容 | TTL | 调用次数/天 |
|---------|------|-----|-----------|
| `top_user_version:24` | 24h 版本用户分布（分桶用） | 6h | 4 次 |
| `top_user_version:3` | 3h 版本用户分布（分母用） | 6h | 4 次 |
| `hourly_alert:new_crash:24h` | 24h 全 issues 列表（通道 3 用）| 6h | 4 次 |

进程重启时缓存失效，下一次 cron 自动回填，影响窗口 ≤3h。

### 6.3 Datadog query

```yaml
# config.yaml
crashguard:
  datadog:
    query_fatal: "@error.type:(crash OR anr OR AppHang)"   # 双路：fatal
    query_nonfatal: "@error.type:(business)"                # 双路：non-fatal
    tracks:
      - errors                  # Error Tracking
```

### 6.4 版本号获取路径

```
issue.attributes.version         → 崩溃发生时的 app 版本
issue.attributes.first_seen_version → issue 全网首次出现的 app 版本
CrashIssue.last_seen_version     → DB 侧最后一次见到的版本（pipeline 维护）
```

top_user_version 来自 Datadog RUM `cardinality(@session.id)` group by `(@os.name, @application.version)`，session 维度代理 user（因为 `@usr.id` 大量为空）。

---

## 7. 配置参数索引

| 字段名（config.py） | yaml 路径 | 默认值 | 说明 |
|---------------------|---------|--------|------|
| `hourly_alert_enabled` | `hourly_alert.enabled` | `true` | kill switch |
| `hourly_alert_cron` | `hourly_alert.cron` | `5 */3 * * *` | cron 表达式 |
| `hourly_alert_growth_threshold_pct` | `hourly_alert.growth_threshold_pct` | `10` | SHoW 上涨阈值（%）|
| `hourly_alert_new_window_days` | `hourly_alert.new_window_days` | `30` | 新增判定窗口（天）|
| `hourly_alert_min_baseline_events` | `hourly_alert.min_baseline_events` | `20` | SHoW 基线最小量 |
| `hourly_alert_min_sessions` | `hourly_alert.min_sessions` | `500` | 会话底线（通道 1/2 共享）|
| `hourly_alert_min_events_absolute` | `hourly_alert.min_events_absolute` | `200` | events 底线（**仅通道 2**）|
| `hourly_alert_dedup_hours` | `hourly_alert.dedup_hours` | `12` | 跨告警去重窗口（h）|
| `hourly_alert_max_items` | `hourly_alert.max_items` | `10` | 卡片最多展示条目 |
| **通道 1** | | | |
| `hourly_alert_new_version_enabled` | `hourly_alert.new_version.enabled` | `true` | 通道 1 开关 |
| `hourly_alert_new_version_shadow_mode` | `hourly_alert.new_version.shadow_mode` | `true` | 影子模式（上线后改 false）|
| `hourly_alert_new_version_min_events` | `hourly_alert.new_version.min_events` | `30` | 通道 1 events 底线 |
| `hourly_alert_new_version_user_rate_pct` | `hourly_alert.new_version.user_rate_pct` | `0.005` | 用户占比阈值（0.5%）|
| **通道 3** | | | |
| `hourly_alert_new_crash_enabled` | `hourly_alert.new_crash.enabled` | `true` | 通道 3 开关 |
| `hourly_alert_new_crash_shadow_mode` | `hourly_alert.new_crash.shadow_mode` | `true` | 影子模式 |
| `hourly_alert_new_crash_window_hours` | `hourly_alert.new_crash.window_hours` | `24` | 24h 累计窗口 |
| `hourly_alert_new_crash_min_events` | `hourly_alert.new_crash.min_events` | `150` | 通道 3 events 底线 |
| `hourly_alert_new_crash_min_sessions` | `hourly_alert.new_crash.min_sessions` | `300` | 通道 3 sessions 底线 |

所有字段支持 `CRASHGUARD_HOURLY_ALERT_*` env var override（优先级高于 yaml）。

---

## 8. 飞书卡片格式

### 8.1 卡片结构

```
[标题] Crashguard 实时告警 · 2026-05-14 15:00 SGT
[正文]
  🔴 [新版本] 灰度异常                       ← 通道 1 节
    1. {title} ....
  
  🟠 [新 crash] 全网首现                     ← 通道 3 节
    1. {title} ....
  
  🆕 新增崩溃                               ← 通道 2 new 节（现有）
    1. {title} ....
  
  ⚠️ 异常上涨                               ← 通道 2 surge 节（现有）
    1. {title} ....
  
  ─────────────────────────────
  [📊 Web 端查看]  [👍 准]  [👎 不准]        ← 反馈按钮
```

### 8.2 反馈按钮 URL

```
👍 准：{frontend_base_url}/api/crash/alert-feedback?alert_id={id}&label=good
👎 不准：{frontend_base_url}/api/crash/alert-feedback?alert_id={id}&label=bad
```

点击后跳转到 jarvis backend（经 Next.js rewrites 代理），记录到 `crash_hourly_alerts.feedback`。

---

## 9. 运维 Dashboard

### 9.1 API

```
GET /api/crash/alert-channels
```

返回示例：

```json
{
  "ok": true,
  "window_hours": 24,
  "channels": [
    {"name": "new",         "count_24h": 0,  "enabled": true, "shadow_mode": false},
    {"name": "surge",       "count_24h": 12, "enabled": true, "shadow_mode": false},
    {"name": "new_version", "count_24h": 0,  "enabled": true, "shadow_mode": true,
     "threshold": {"min_events": 30, "user_rate_pct": 0.005}},
    {"name": "new_crash",   "count_24h": 0,  "enabled": true, "shadow_mode": true,
     "threshold": {"window_hours": 24, "min_events": 150, "min_sessions": 300}}
  ],
  "audit_rows_24h": 5,
  "datadog_cache": {"keys": ["top_user_version:24", "top_user_version:3"], "count": 2},
  "feedback_24h": {"good": 3, "bad": 1, "total_with_feedback": 4, "total_audit_rows": 5}
}
```

### 9.2 前端页面

`/crashguard/jobs` 页顶部 "Alert Channels" tile，30s 自动刷新。每个通道显示命中数 + 状态徽章（ON/OFF/影子）。

---

## 10. DB 表

### 10.1 `crash_hourly_alerts`（每次 cron 产生告警时写一行）

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | PK | |
| `hour_utc` | DATETIME UNIQUE | 3h 块起点（UTC），UNIQUE 防重发 |
| `new_count` | INT | 通道 2 new 命中数 |
| `surge_count` | INT | 通道 2 surge 命中数 |
| `feishu_message_id` | VARCHAR | 飞书消息 ID（影子模式下为空）|
| `alert_payload` | TEXT | 完整 JSON，含 new/surge/new_version/new_crash 四个 list |
| `feedback` | VARCHAR(16) | 用户反馈："good"/"bad"/NULL |
| `feedback_at` | DATETIME | |
| `feedback_by` | VARCHAR(64) | 反馈者标识 |
| `created_at` | DATETIME | |

**alert_payload schema**：

```json
{
  "new": [{issue_id, title, platform, events_h, sessions_h, first_seen}],
  "surge": [{issue_id, title, platform, events_h, sessions_h, baseline, growth_pct, rate_now, rate_base, rate_growth_pct}],
  "new_version": [{issue_id, title, platform, version, first_seen_version, events_h, sessions_h, user_rate_pct, denom_source, denom_users}],
  "new_crash": [{issue_id, title, platform, first_seen_version, first_seen_at, first_seen_source, events_24h, sessions_24h}],
  "threshold_pct": 10.0,
  "min_sessions": 500,
  "window_start": "2026-05-14T09:00:00",
  "window_end": "2026-05-14T12:00:00"
}
```

### 10.2 `crash_hourly_snapshots`（每次 cron 对每个 issue 写快照）

| 列 | 说明 |
|----|------|
| `datadog_issue_id` | issue ID |
| `hour_utc` | 3h 块起点 |
| `events_count` | 本 3h 块 events 数 |
| `sessions_count` | 本 3h 块受影响 sessions 数（rate-AND-check 用）|

UNIQUE(datadog_issue_id, hour_utc)。**所有 issue 都入库（不论是否告警）**，用于下周 SHoW 基线计算。

---

## 11. 告警量预估

| 通道 | 每天预估触发次数 | 设计说明 |
|------|---------------|---------|
| 通道 2 surge | ~0.5 次/天 | 已部署上轮降噪（min_sess=500, events≥200, dedup_12h）|
| 通道 2 new | ~0.2 次/天 | 30d 首现 + events≥200 限制 |
| 通道 1 new_version | ~0.3 次/天（预估）| 灰度发版期间触发，平时静默 |
| 通道 3 new_crash | ~0.4 次/天（预估）| |
| **合计** | **~1.4 次/天** | 较改造前 1.7 次/天下降，且信号质量更高 |

dedup_12h 确保同一 issue 一天内最多告警一次。

---

## 12. 上线流程（新通道 shadow → 真发）

1. 确认影子模式已运行 ≥24h
2. 查 audit log 统计：

```bash
cd backend && python -c "
import asyncio, json
from app.db.database import init_db, get_session
from app.crashguard.models import CrashHourlyAlert
from sqlalchemy import select, desc
from datetime import datetime, timedelta

async def main():
    await init_db()
    cutoff = datetime.utcnow() - timedelta(hours=24)
    async with get_session() as s:
        rows = (await s.execute(select(CrashHourlyAlert)
            .where(CrashHourlyAlert.created_at >= cutoff))).scalars().all()
        for r in rows:
            p = json.loads(r.alert_payload)
            print(f'hour={r.hour_utc} nv={len(p.get(\"new_version\",[]))} nc={len(p.get(\"new_crash\",[]))}')
asyncio.run(main())
"
```

3. 若命中数合理（每天 ≤3 次），修改 config.yaml：

```yaml
crashguard:
  hourly_alert:
    new_version:
      shadow_mode: false   # ← 改
    new_crash:
      shadow_mode: false   # ← 改
```

4. 等 uvicorn 自动 reload（`--reload` 模式）或 `kill -HUP <pid>`。

**回滚方法**：随时将 `shadow_mode: true` 或 `enabled: false` 写回 config.yaml 即可，无 DB schema 变更。

---

## 13. 已知限制与 TODO

| 限制 | 影响 | 待改进 |
|------|------|--------|
| user_rate 分母用 events 代理 users | 一个 user 多次崩溃会放大分子 | 等 Plaud RUM SDK 接入 `setUser(userId)` 后改用 `@usr.id` cardinality |
| DatadogCache 进程内 dict | 多实例部署时各实例 cache 独立 | 多实例上线时可改 Redis（目前单实例 Docker OK）|
| 反馈按钮 URL-based | 用户需在浏览器打开 jarvis，不能直接在飞书内完成 | 后续改 feishu interactive callback |
| 通道 1 user_rate 阈值为固定值 | 不同平台用户量差异大 | 可按平台独立配置阈值 |
| `top_user_version` 用 session 代理 user | 日均 session/user ≈ 1-3，相关性高但非精确 | 同上，等 SDK 接入 |
