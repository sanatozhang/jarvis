# Crashguard 告警阈值与触发场景

> 本文档列出所有 crashguard 告警、报表的量级门槛，说明每个阈值的设计意图和实际触发条件。
> 调整阈值在 `config.yaml` 的 `crashguard:` 段覆盖，无需改代码。

## Plaud 当前流量基准（参考值，2026-05）

| 平台 | 日均 sessions | 10min 均值 | 峰值估算 |
|------|-------------|-----------|---------|
| iOS | ~38,000 | ~264 | ~450 |
| Android | ~13,600 | ~94 | ~170 |
| 合计 | ~51,600 | ~358 | ~620 |

---

## 一、早晚报（daily_report）

### 1.1 `surge_min_events = 100`

**场景**：判定某 issue 是否属于"飙升"（今日 events > 上周同期 × 1.5）

**触发条件**：`today_events >= 100` 才参与飙升分类

**设计意图**：events < 100 的 issue 即使涨幅极大，绝对增量也极小，对用户无实质影响。与 `daily_attention_min_events` 对齐，避免"标了 surge 但进不了 attention pool"的无效分类。

---

### 1.2 `daily_attention_min_events = 100`

**场景**：issue 进入早晚报 attention pool（关注列表）的门槛

**触发条件**：`events_today >= 100`（新增 issue 豁免，无论 events 多少都进）

**设计意图**：过滤极低频噪声，保证报表信噪比。

---

### 1.3 `daily_baseline_min_events_for_pct = 500`

**场景**：上周同时段 baseline events 是否参与百分比 surge 计算

**触发条件**：`baseline_events < 500` → 不参与 % 判定，直接跳过 surge 检测

**设计意图**：防"基线 20 events → 今日 26 events = +30% 飙升"的假大。实测案例：baseline=172 → +81% 看似严重，真实增量仅 139 events，设 500 后过滤。

---

### 1.4 `latest_version_min_sessions = 300`

**场景**：早晚报 Crash-free 详表"最新版本"D 段展示门槛

**触发条件**：从 Datadog RUM 版本分布中，取 `sessions >= 300` 且 semver 最大的版本；若全部版本均 < 300，则 D 段留空

**设计意图**：灰度/内测包 session 极少，CF 数据无统计意义。口径：RUM inactive sessions（已结束会话），与 Crash-free 率分母口径一致。

> **注**：原有 `latest_version_min_events`（crash events ≥ 300 筛候选版本）已移除。稳定版因崩溃少、crash events 低，用 crash events 做代理会反向排除优质版本。

---

## 二、hourly_alert（3h 块级告警，主通道 2）

每 3 小时触发一次（cron `5 */3 * * *`），对比上周同 weekday 同 3h 块（SHoW 基线）。

### 2.1 `hourly_alert_min_sessions = 500`

**场景**：单 issue 进入 hourly alert 的最低受影响 sessions 要求

**触发条件**：`sessions_affected_in_window < 500` → 跳过，不告警

**设计意图**：过滤极低频 issue，避免小众场景反复打扰。500 = 与 `core_metric_min_sessions` 对齐。

> **当前影响**：Plaud 流量下 Android 均值仅 94 sessions/10min，3h 窗口约 1,700 sessions，此阈值实际约束的是"单 issue 受影响 sessions"不是平台总量，合理。

---

### 2.2 `hourly_alert_min_events_absolute = 200`

**场景**：单 issue 3h 窗口内 crash events 绝对量底线

**触发条件**：`events_in_window < 200` → 跳过

**设计意图**：防"小基数 issue 在用户量大涨时被反复点名"。即使百分比涨幅过阈值，绝对增量才几十事件对业务无意义。

---

### 2.3 `hourly_alert_min_baseline_events = 50`

**场景**：SHoW 基线（上周同 3h 块）是否参与百分比判定的有效性门槛

**触发条件**：`baseline_events < 50` → 不参与 % 计算，跳过 surge 检测（但"新增 issue" 逻辑不受此限制）

**设计意图**：基线 < 50 时，百分比噪声过大（基线 20 events，+10% = 2 events 绝对增量）。

---

### 2.4 `hourly_alert_growth_threshold_pct = 10%`

**场景**：events 增长率触发阈值

**触发条件**：`(current_events - baseline_events) / baseline_events >= 10%`，且同时满足 rate-AND-check（crash rate 也要涨）

**设计意图**：rate-AND-check 防"用户量增长导致 events 等比例上涨"的误告警——events 涨但 crash rate 持平则不告警。

---

## 三、hourly_alert 通道 1（新版本桶）

专门检测新版本灰度中的崩溃，与主通道并行。

### 3.1 `hourly_alert_new_version_min_events = 30`

**触发条件**：新版本 issue 3h 窗口 events ≥ 30

---

### 3.2 `hourly_alert_new_version_user_rate_pct = 0.5%`

**触发条件**：`events / 主要版本用户数 >= 0.5%`

**设计意图**：新版本用户量极少时，即使有一些崩溃也可能是个位数用户，0.5% 用户占比才有业务意义。

---

## 四、hourly_alert 通道 3（全局新 crash 兜底）

24h 累计维度，补捉缓慢增长但总量已显著的新 crash。

### 4.1 `hourly_alert_new_crash_min_events = 150`

**触发条件**：过去 24h 累计 events ≥ 150

---

### 4.2 `hourly_alert_new_crash_min_sessions = 300`

**触发条件**：过去 24h 受影响 sessions ≥ 300

---

## 五、core_metric（10min 粒度 crash-free 整体监控）

每 10 分钟检查一次（cron `*/10 * * * *`），监控全平台 crash-free 率是否异常下跌。

### 5.1 `core_metric_min_sessions = 500`

**触发条件**：当前 10min 窗口 total_sessions ≥ 500 才参与判定

**设计意图**：覆盖晨间/周末低峰噪声。Plaud 流量下 iOS 均值 264/10min、Android 94/10min，此阈值意味着**仅在 iOS+Android 合并峰值时才会触发**，平时可能几乎不触发。

> **建议关注**：如需让 core_metric 在日常流量中也能触发，可将此值下调至 200（Android+iOS 合并约 358/10min）。

---

### 5.2 `core_metric_min_crashed_sessions = 10`

**触发条件**：当前 10min 窗口 crashed_sessions ≥ 10

**设计意图**：与 min_sessions=500 配合，10/500=2% crash rate，这与正常 crash-free 99.84% 相差 1.84pp，远超 0.3pp 阈值，是真实异常。防止 1-2 个用户偶发崩溃触发全平台告警。

---

### 5.3 `core_metric_change_threshold_pp = 0.3`

**触发条件**：`|当前 crash_free_pct - 前 1h 均值| >= 0.3 pp`

**场景示例**：
- 正常基线 iOS crash-free = 99.84%
- 当前 10min = 98.0%（500 sessions，10 crashed）→ Δ = 1.84pp → **触发**
- 当前 10min = 99.6%（500 sessions，2 crashed）→ Δ = 0.24pp → **不触发**

---

## 六、阈值速查表

| 字段 | 值 | 模块 | 单位 |
|------|-----|------|------|
| `surge_min_events` | 100 | 早晚报 | crash events |
| `daily_attention_min_events` | 100 | 早晚报 | crash events |
| `daily_baseline_min_events_for_pct` | 500 | 早晚报 | crash events |
| `latest_version_min_sessions` | 300 | 早晚报最新版 | RUM sessions |
| `hourly_alert_min_sessions` | 500 | hourly 通道 2 | RUM sessions |
| `hourly_alert_min_events_absolute` | 200 | hourly 通道 2 | crash events |
| `hourly_alert_min_baseline_events` | 50 | hourly 通道 2 | crash events |
| `hourly_alert_growth_threshold_pct` | 10% | hourly 通道 2 | 百分比 |
| `hourly_alert_new_version_min_events` | 30 | hourly 通道 1 | crash events |
| `hourly_alert_new_version_user_rate_pct` | 0.5% | hourly 通道 1 | 用户占比 |
| `hourly_alert_new_crash_min_events` | 150 | hourly 通道 3 | crash events |
| `hourly_alert_new_crash_min_sessions` | 300 | hourly 通道 3 | RUM sessions |
| `core_metric_min_sessions` | 500 | core_metric | RUM sessions |
| `core_metric_min_crashed_sessions` | 10 | core_metric | crashed sessions |
| `core_metric_change_threshold_pp` | 0.3 | core_metric | percentage points |

---

## 七、config.yaml 覆盖示例

```yaml
crashguard:
  # 早晚报
  surge_min_events: 100
  daily_attention_min_events: 100
  daily_baseline_min_events_for_pct: 500
  latest_version_min_sessions: 300

  # hourly_alert
  hourly_alert_min_sessions: 500
  hourly_alert_min_events_absolute: 200
  hourly_alert_min_baseline_events: 50
  hourly_alert_growth_threshold_pct: 10.0

  # core_metric
  core_metric_min_sessions: 500       # 可下调至 200 以覆盖 Plaud 当前流量
  core_metric_min_crashed_sessions: 10
  core_metric_change_threshold_pp: 0.3
```
