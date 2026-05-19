# Crashguard 口径与指标说明

> 早晚报、Hourly 告警、Core Metric 告警、首页大盘共用的指标口径。  
> 当群里有人问"这个数怎么算的 / 为啥头条说涨我看不到"，把本文链接丢给他。

---

## 一、Crash-free Sessions 率（核心健康度）

### 公式

```
Crash-free 率 = (已结束会话数 − 崩溃会话数) / 已结束会话数
```

### 关键名词

| 名词 | 定义 | Datadog 查询条件 |
|---|---|---|
| **已结束会话（inactive）** | `@session.is_active:false` —— 用户已退出或会话因超时关闭 | `@type:session @session.is_active:false` |
| **崩溃会话** | 该 session 内发生过至少一次 fatal error（native crash / ANR / App Hang） | `@type:session @session.is_active:false @session.error.count:>0`（带 fatal filter） |
| **崩溃事件（events）** | 单次崩溃实例。同一 session 可有多次事件 | 用于 events 维度，不用于会话维度 |

### 为什么用 inactive-only

- 对齐 **Firebase Crashlytics / Datadog 官方 Crash-free Sessions** 定义。
- 活跃会话（`is_active:true`）即"现在还在跑的 session"，崩溃统计未结算，会让分子分母都跳动 → 数值不稳定。
- 全量会话（active + inactive）会让"刚开始的会话"稀释分子，新版本灰度期 crash-free 率被人为推高。
- 选择 inactive-only → 数值稳定 + 跟外部基准对齐。

### "崩溃" 包含哪些

| 类型 | 平台 | 是否计入崩溃会话 |
|---|---|---|
| Native crash（SIGSEGV / SIGABRT / NSException 等） | iOS / Android | ✅ |
| ANR（Application Not Responding） | Android | ✅ |
| App Hang（iOS watchdog 触发） | iOS | ✅ |
| 业务捕获异常（try/catch 内 logger 上报） | 全平台 | ❌（non_fatal，单独列） |
| Network error / 4xx / 5xx | 全平台 | ❌（错误不计崩溃） |

---

## 二、时间窗口与基线（SHoW-Nh）

### 早晚报差异

| 报告 | 时间 | 数据窗口 | 基线窗口 |
|---|---|---|---|
| **早报** | 07:00 SH | 过去 24h | 上周同 weekday 同 24h 段 |
| **晚报** | 17:00 SH | 过去 10h（日内增量） | 上周同 weekday 同 10h 段 |

### SHoW（Same-Hour-of-Week）基线的底层逻辑

- 不用"vs 昨日"——会被**周末效应**污染（周日流量比周三低 30%，看上去"崩溃下降"是假信号）。
- 不用"vs 上小时"——会被**日内时区周期**污染（凌晨 vs 中午 JP/US 流量差 4 倍）。
- 用 SHoW 同时对齐 **weekday + 时区** 双周期 → "今 vs 上周同时刻"差值才是真信号。

### 小基数过滤

| 阈值 | 默认值 | 用途 |
|---|---|---|
| `baseline_min_for_pct` | 50 | 基线 events < 50 时 %  噪声过大，单 issue 不进 surge 列表（500→1000 看着 +100%，绝对量级却微小） |
| `daily_attention_min_events` | 100 | 当日 events < 100 不进 surge/drop 列表（绝对量底线） |
| `daily_surge_threshold` | +10% | 单 issue 超过此 % 才算"突增" |
| `daily_drop_threshold` | -10% | 单 issue 低于此 % 才算"下降" |
| `hourly_alert_dedup_hours` | 12 | 12h 内 hourly 已点过的 surge issue → 早晚报关注点列表跳过 |

---

## 三、为什么"头条 +X% 与单 issue 列表不对得上"

早晚报有**两套独立计算路径**，不可避免会出现"头条大涨但关注点列表为空"的情况。

| 维度 | 头条 `iOS fatal +130% 🔴` | 关注点单 issue 列表 |
|---|---|---|
| 数据源 | Datadog `list_issues_for_window` 实时双窗口 | DB 表 `crash_snapshots` + `realtime_baseline_events` |
| 颗粒度 | 平台 fatal events 总量加总 | 单 issue × SHoW-Nh |
| 阈值过滤 | **无** | ≥+10% AND ≥100 events AND baseline ≥50 |
| 跨告警去重 | **无** | 12h 内 hourly 点过 → 列表跳过 |

### 解决"失语" — 突增主因 Top 3 段

当 `fatal_delta_pct ≥ +30%` 时，关注点段会强制出一行：

```
### 📌 突增主因 Top 3（按事件绝对增量 · 与头条 fatal +% 同源 · 无阈值/无去重）

**🍎 iOS fatal +130%** — 主因：
- **+880 events** (1,558 vs 上周 678 · +130%) · 🔔 hourly 已报 · [AppHang @ ...](URL)
- ...
```

- **数据源**：跟头条同一份 `realtime_today_events` / `realtime_baseline_events` 字典。
- **排序**：按 `today_events - baseline_events` 绝对增量倒序（不是百分比 —— 1 个 issue 涨 +500% 但只多 5 events，跟 1 个 issue 涨 +50% 但多了 800 events，后者价值大）。
- **徽章**：12h 内 hourly 已点过 → 🔔，让运维一眼看到"这个已经报过、但还在涨"。
- **不受阈值与去重过滤**，专治"头条爆 + 正文哑"。

---

## 四、版本切片

### 主要版本 = 用户量最大版本

- 取过去 24h Datadog RUM `cardinality(@session.id)` group by `@application.version`，session 数最高那个。
- session 数代理 user 数（Plaud RUM SDK 未调 `setUser`，`@usr.id` 几乎全空；24h 内同 user 通常 1-3 session，相关性极高）。
- 用于"主要版本 Crash-free 详表"段。

### 最新版本 = 线上当前发布版本

| 来源优先级 | 说明 |
|---|---|
| 1. `config.current_release.{flutter,android,ios}` 手动覆盖 | 运营手动指定 |
| 2. Datadog RUM 版本分布 | `inactive sessions ≥ latest_version_min_sessions`（默认 300）且 semver 最大 |
| 3. 空字符串 fallback | 数据不足时不渲染该段 |

---

## 五、其他报告/告警的口径速查

| 报告 | 触发 | 基线 | 阈值 |
|---|---|---|---|
| **Hourly Alert** | 每 3h 第 5 分钟 | SHoW-3h | 单 issue ≥ +10%，受影响 sessions ≥ 500，events ≥ 200 |
| **Core Metric Alert** | 每 10 分钟 | 前 1h 加权均值 | crash-free 率变化 ≥ 0.3pp，10min 窗口 sessions ≥ 500，crashed sessions ≥ 10 |
| **新版本桶（hourly）** | hourly_alert 子通道 | 新版本独立切片 | events ≥ 30, user_rate ≥ 0.5% |
| **全局新 crash 兜底** | hourly_alert 子通道 | 24h 累计 | events ≥ 150, sessions ≥ 300 |

---

## 六、Top 突增 / 下降单 issue 行的标签

| 标签 | 条件 | 优先级 |
|---|---|---|
| 🆕 | `is_new_in_version=True`（该版本首次出现） | 最高 |
| 📈 | `delta ≥ +10%` AND `events ≥ 100` AND `baseline ≥ 50` | 第二 |
| 🔥N | 该 fatality 桶内 events Top N（N ≤ 5） | 第三 |
| 📉 | `delta ≤ -10%` | 最低 |
| 🔔 | 12h 内 hourly_alert 已点过（不影响渲染，仅徽章） | 标识 |

同一 issue 只占一行，按优先级取最高级标签渲染。

---

## 七、字段口径速查

| 字段 | 来源 | 颗粒度 | 注 |
|---|---|---|---|
| `events_count` | `crash_snapshots.events_count` | 单 issue × 单日 | 24h 内该 issue 发生次数 |
| `sessions_affected` | `crash_snapshots.sessions_affected` | 单 issue × 单日 | 24h 内被该 issue 影响的 distinct session 数 |
| `users_affected` | **全 0**（已知 data hole） | – | Plaud RUM SDK 未调 setUser，`@usr.id` 空，**勿用此字段** |
| `total_sessions_by_plat` | Datadog `count_inactive_sessions_by_platform` | 平台 × 窗口 | 分母，用于算 crash-free |
| `distinct_crash_sessions_by_plat` | Datadog `count_inactive_crash_sessions_by_platform` | 平台 × 窗口 | 分子（distinct，含 ANR + Hang） |
| `top_app_version` | `crash_issues.top_app_version` | 单 issue | 由 `distribution_prewarmer` 在 analyzer 时刷新；格式 `"3.16.0-634 (60%), 3.15.1-631 (30%)"` |
| `crash_free_impact_score` | `compute_impact_score(users, events)` | 单 issue × 单日 | 用于 Top N 排序权重，非展示口径 |

---

## 八、看见这些数字时该怎么解读

| 现象 | 解读 | 该不该慌 |
|---|---|---|
| 头条 `iOS fatal +130%` 红色 | 平台总量翻倍——但要看「突增主因 Top 3」段是哪个 issue 涨 | 看主因徽章：🔔 已 hourly 报过 = 不用慌、已在跟；无徽章 = 关注 |
| Crash-free 率 99.86% 黄色 | 略低于 99.9% 阈值——常见原因是某个高量 ANR 或 AppHang | 看「双窗口对照」：sessions 涨了多少、fatal events 涨了多少。涨 fatal 是问题、涨 sessions 不是 |
| `📉 -36%` 单 issue 标签 | 该 issue 同比下降 —— **不是** 在 surge | 不慌，是修复/灰度回收的好信号 |
| `🆕新版` 但 +0% | 新版本首次出现该 issue，无基线 | 看绝对量级：events < 100 通常不慌，>= 500 优先看 |
| 平台 fatal +X% 但关注点空白 | hourly 全报过 + 单 issue 都未过阈值。看「突增主因 Top 3」段拿真相 | 看徽章定 |

---

## 九、本文档维护

- 改了阈值/口径 → 同步本文档
- 新增告警类型 → 加到「其他报告/告警的口径速查」段
- 删除字段 → 标记 deprecated 并保留 6 个月再彻底删

> 最后更新：2026-05-19（迁出 daily_report 内 inline 说明）
