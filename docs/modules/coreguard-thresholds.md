# Coreguard 报警阈值清单

> 更新于 2026-05-25。所有阈值均经过 Datadog 历史抖动实证，非拍脑袋。
> 配置源：`backend/app/coreguard/metrics.yaml`。运行逻辑：`backend/app/coreguard/services/metric_watcher.py`。

## 触发流程

```
每小时第 15 分钟 cron 触发 (cron: "15 * * * *")
   ↓
1) 拉取本时段（前 1h）窗口 distinct user_count（cardinality(@usr.id) 单次共享）
   ↓
2) 22 个 alert_enabled 指标 SHoW 对比：本时段 vs 上周同时段
   ↓
3) Gate A — 样本量地板：若 user_count < min_users(300) → 静默写快照
   ↓
4) Gate B — N=2 防抖：P1 单点 breach 不报，须连续 2 小时 breach 才入卡（P0 不防抖立即报）
   ↓
5) 通过 gate 的 alertable 指标 → 飞书私聊（sanato.zhang@plaud.ai）
   ↓
6) 所有结果入 coreguard_metric_snapshots 表，可在 /api/coreguard/snapshots 查询
```

## 判定逻辑（_judge）

| 指标 value_type | 计算公式 | 阈值字段 |
|---|---|---|
| `percent_pp` | `change = current - baseline`（绝对百分点差）| `threshold.pp` |
| `latency_pct` / `count_pct` | `change = (current - baseline) / baseline`（相对百分比变化）| `threshold.pct` |

| direction | 触发条件 |
|---|---|
| `down_is_bad` | `change ≤ -threshold` |
| `up_is_bad` | `change ≥ +threshold` |
| `both` | `abs(change) ≥ threshold` |

## P0 — 立即报警（8 项）

| # | 指标 | value_type | direction | 阈值 | 实证 / 设计依据 |
|---|---|---|---|---|---|
| 1 | Crash-free sessions | percent_pp | down_is_bad | **-1.0pp** | 99.5% SLO 基线下，1.0pp = 故障率 2x 放大。原 0.5pp 太敏感（noise 多） |
| 2 | Crash-free users | percent_pp | down_is_bad | **-1.0pp** | 同上 |
| 3 | Android ANR | percent_pp | up_is_bad | **+1.0pp** | 与其他 P0 颗粒度对齐 |
| 4 | Hang Rate | latency_pct | up_is_bad | **+50%** | 实测过去 48h WoW \|Δ\| 中位数 34.1%、P90=53.3% — 原 20% 阈值会触发 73% 天然抖动。50% 命中 P75 之外才报 |
| 5 | 云同步上传成功率 V2 | percent_pp | down_is_bad | -1.0pp | 业务核心链路 |
| 6 | 音频导入成功率 | percent_pp | down_is_bad | -1.0pp | 业务核心链路 |
| 7 | API Call Success Rate | percent_pp | down_is_bad | -1.0pp | 业务核心链路 |
| 8 | AI 任务-转写成功率 | percent_pp | down_is_bad | -1.0pp | 业务核心链路 |

## P1 — 聚合报警 + N=2 防抖（14 项）

> P1 全部启用 N=2 防抖：单小时 breach 仅记录，连续 2 小时才进飞书卡。

| # | 指标 | value_type | direction | 阈值 | 备注 |
|---|---|---|---|---|---|
| 1 | Memory Usage | count_pct | up_is_bad | +25% | 内存涨幅 |
| 2 | Refresh Rate | percent_pp | down_is_bad | -1.0pp | 刷新率（越高越好） |
| 3 | **APP 单次运行平均 FPS** | count_pct | **down_is_bad** | -25% | ✅ **2026-05-25 修复**：原配 up_is_bad 是 bug，FPS 越高越好 |
| 4 | Cold Startup p90 | latency_pct | up_is_bad | **+20%** | 实测延迟类 WoW P90=22.1%，20% 压住 90% 抖动 |
| 5 | wifi 同步文件速度 P75 | latency_pct | up_is_bad | +20% | 同上 |
| 6 | 首页文件列表加载 P75 | latency_pct | up_is_bad | +20% | 同上 |
| 7 | 首页文件列表加载 P90 | latency_pct | up_is_bad | +20% | 同上 |
| 8 | 详情页首屏加载 P75 | latency_pct | up_is_bad | +20% | 同上 |
| 9 | 详情页 WebView 首屏 P75 | latency_pct | up_is_bad | +20% | 同上 |
| 10 | 文件详情页切换 Tab P75 | latency_pct | up_is_bad | +20% | 同上 |
| 11 | API 延迟 P95 | latency_pct | up_is_bad | +20% | 同上 |
| 12 | Highlight 渲染 P75 | latency_pct | up_is_bad | +20% | 同上 |
| 13 | Highlight 打点成功率 | percent_pp | down_is_bad | -1.0pp |  |
| 14 | Audio Upload Cloud Duration P50/P75/P95 | latency_pct | up_is_bad | +20% |  |

## P2 — 仅写快照 / 日报（21 项，alert_enabled=false）

> P2 不进小时报警链路，留给后续 daily report 摘要消费。阈值同 P1 通用值，仅作日报参考。

| # | 指标 | value_type | direction | 阈值 |
|---|---|---|---|---|
| 1 | iOS 卡顿次数 P90 | latency_pct | up_is_bad | +25% |
| 2 | iOS 卡顿次数 P75 | latency_pct | up_is_bad | +25% |
| 3 | Android 卡顿次数 P90 | latency_pct | up_is_bad | +25% |
| 4 | Android 卡顿次数 P75 | latency_pct | up_is_bad | +25% |
| 5 | Package Size | count_pct | up_is_bad | +25% |
| 6 | 云下载/更新成功率 | percent_pp | down_is_bad | -1.0pp |
| 7 | Websocket 连接成功率 | percent_pp | down_is_bad | -1.0pp |
| 8 | 音频解码成功率 | percent_pp | down_is_bad | -1.0pp |
| 9 | 设备总解绑成功率 | percent_pp | down_is_bad | -1.0pp |
| 10 | 音频播放成功率 | percent_pp | down_is_bad | -1.0pp |
| 11 | 设备绑定成功率 | percent_pp | down_is_bad | -1.0pp |
| 12 | 强制解绑成功率 | percent_pp | down_is_bad | -1.0pp |
| 13 | App 设备 OTA 传输成功率 | percent_pp | down_is_bad | -1.0pp |
| 14 | App 云端下载 OTA Bin 成功率 | percent_pp | down_is_bad | -1.0pp |
| 15 | Wi-Fi 连接成功率 | percent_pp | down_is_bad | -1.0pp |
| 16 | Ask Add Note 成功率 | percent_pp | down_is_bad | -1.0pp |
| 17 | wifi 文件同步成功率 | percent_pp | down_is_bad | -1.0pp |
| 18 | ble 文件同步成功率 | percent_pp | down_is_bad | -1.0pp |
| 19 | 分享导出成功率 | percent_pp | down_is_bad | -1.0pp |
| 20 | **API 请求量（按接口）** | count_pct | **both** | **±50%** | ✅ 2026-05-25 改 both，原 up_is_bad+25% 会被自然增长误报；暴涨/暴跌都有意义 |
| 21 | AI 任务-Add Note 成功率 | percent_pp | down_is_bad | -1.0pp |

## 全局 Gate

| Gate | 配置项 | 默认值 | 行为 |
|---|---|---|---|
| **min_users 样本量地板** | `COREGUARD_MIN_USERS` | **300** | 当前窗口 `cardinality(@usr.id) < 300` → 所有 breach 标 alertable=False，写快照不发卡 |
| **P1 N=2 防抖** | `COREGUARD_P1_CONSECUTIVE_BREACH` | **2** | P1 单次 breach 写快照但不入飞书卡；前一窗口同 key 也 breach 才入卡 |
| **窗口对齐** | `hourly_watch_cron` | `"15 * * * *"` | 每小时第 15 分跑，给 Datadog RUM ingest 留 15min 缓冲 |
| **当前窗口** | hardcoded | `[hour_floor - 1h, hour_floor)` | 即"前一小时"，避开 Datadog ingest 末段抖动 |
| **基线窗口** | hardcoded | `current - 7 天` | SHoW（Same Hour-of-Week）对比，跨工作日/周末差异最稳 |

## 与 crashguard 阈值的对齐颗粒度

| 维度 | crashguard | coreguard | 对齐策略 |
|---|---|---|---|
| 样本量底线 | `core_metric_min_sessions=500` / `latest_version_min_sessions=300` | `min_users=300` | coreguard 用 user 维度（user/session ≈ 1.14），300 users ≈ 343 sessions，与 crashguard "展示档"一致 |
| 触发频率 | `hourly_alert_cron="15 */3 * * *"` | `hourly_watch_cron="15 * * * *"` | 同样的 15min ingest 缓冲；coreguard 频率更高（小时颗粒度） |
| 推送通道 | 群（chat_id）默认 | 个人邮箱默认（`feishu_prefer_email=true`） | coreguard 演示阶段点对点不打扰群 |
| 去重 | `hourly_alert_dedup_hours=12` | （待落 — 设计文档 §6 lifecycle 第 1 项） | 后续 sprint |

## 待办 / 已知颗粒度盲区

1. **alert escalate**：P0 连续 N 小时 breach → 升级 oncall。设计文档 §6 第 3 项，未实现。
2. **resolved 自动通知**：breach 转 healthy 时主动通知，避免工程师反复刷 Datadog。设计文档 §6 第 5 项，未实现。
3. **daily P2 报表**：21 项 P2 + 全部健康度走日报，未实现。
4. **周末 / 工作日基线分桶**：节假日 baseline 漂移可能误判，未拆分。

## 变更日志

| 日期 | 变更 |
|---|---|
| 2026-05-25 | 初版阈值（基于 Datadog dashboard `4h8-qff-zra` 自动生成）|
| 2026-05-25 | P0 成功率 0.5pp → 1.0pp；ANR 0.5pp → 1.0pp；Hang Rate 20% → 50%（实证 WoW P90=53%）|
| 2026-05-25 | P1 延迟类 25% → 20%（实证 WoW P90=22%）|
| 2026-05-25 | FPS direction up_is_bad → down_is_bad（修复历史 bug） |
| 2026-05-25 | API 请求量 改 both + 50%（避免自然增长误报）|
| 2026-05-25 | 新增全局 `min_users=300` 样本量地板（实测 @usr.id 填充率 92.7%）|
| 2026-05-25 | 新增 P1 N=2 防抖；P0 保持单点即报 |
