# Crashguard 告警按版本切片设计（v1）

**Date**: 2026-05-14
**Owner**: sanato
**Status**: Draft（待审核）

## 1. 背景与动机

### 1.1 当前痛点

Crashguard `hourly_alerter` 模块**仅有一条触发通路**（全平台单桶 SHoW-3h 对比），无法区分以下三类信号：

1. **新版本灰度异常**——新版本刚上线用户少、数据波动大，现状逻辑要么误报（小基数百分比假信号）要么漏报（绝对量未达阈值）
2. **大盘版本恶化**——主力版本上老 crash 突然恶化，现状能覆盖（上一轮 #1+#2+#3 已优化）
3. **全网新 crash 突现**——某 issue 全网近 30 天首次出现且事件量上量，现状的 "new 通道" 无 events 门槛，导致 148 sessions 也触发"AppHang"误报

各版本用户量动态变化（新版本灰度→放量→主版本→老版本沉淀），单一阈值无法适应全生命周期。

### 1.2 目标（顶层设计）

- **新版本灰度阶段**：以 user_rate 为主信号，绝对量为底线 → 即使小样本也能抓"真问题"
- **大盘**：保持现有 SHoW + rate-AND-check 不变（上一轮已降噪到 ~0.5 次/天）
- **全网新 crash**：独立通道 + events 量级地板，兜底"突然冒出来的新 crash"
- **三通道合计 ≤ 1.2 次/天**，信号质量优先于召回率

## 2. 顶层架构

```
                  ┌────────────────────────────────────────────┐
                  │   hourly_alerter cron (5 */3 * * *)        │
                  └────────────────┬───────────────────────────┘
                                   ▼
   ┌──────── 拉数据（3h 窗口 + 24h 累计，均走缓存） ──────────┐
   │                                                          │
   ▼                          ▼                               ▼
[通道 1] 新版本桶          [通道 2] 大盘桶              [通道 3] 全局新 crash 兜底
─────────────────       ─────────────────              ─────────────────
窗口: 3h                 窗口: 3h                       窗口: 24h 累计
版本: > top_user_v       版本: ≤ top_user_v             版本: 不限
触发:                     触发: 现有 SHoW+rate           触发:
  events≥30                events≥SHoW*1.1                first_seen ≤ 30 天
  AND user_rate≥0.5%       AND rate AND check             AND events≥150
  AND sessions≥500         AND events_abs≥200             AND sessions≥300
                          AND sessions≥500
```

三通道**共享**：alert_dedup（12h 跨告警去重）、推送 channel（alert_email DM）、min_sessions=500 配置抓手。

同 issue 多通道命中时**合并为单卡**，标签优先级 `[新版本] > [新 crash] > [主版本]`。

## 3. 版本识别（B2 + B4）

### 3.1 主路径 B2：版本号比较

数据源已就绪：`datadog_client.top_user_version_by_platform(window_hours=24)` 返回每平台 user 量最大的版本 + users 数。

```python
def classify_version(issue_version: str, platform: str, top_versions: dict) -> str:
    """
    Returns: "new" | "main" | "legacy"
    """
    top_ver = top_versions[platform]["version"]
    issue_semver = parse_semver(issue_version)  # 忽略 -build 号
    top_semver = parse_semver(top_ver)
    if issue_semver > top_semver:
        return "new"
    elif issue_semver == top_semver:
        return "main"
    return "legacy"
```

**版本号比较规则**：
- 格式 `3.16.0-634` → 取 `3.16.0` 做比较，忽略 build 号（hotfix 重打包不影响分类）
- 使用 `tuple(map(int, v.split(".")))` 简单实现，无外部依赖

### 3.2 卡片标签 B4：first_seen_version

每张卡片底部加一行：

```
首次出现版本: v3.20.0  ← B4 高亮
```

数据已在 `CrashSnapshot.first_seen_version` 字段中，无需新拉。

### 3.3 边界情况

| 场景 | 处理 |
|------|------|
| issue 没有 version 字段 | 归大盘桶（通道 2），不触发新版本通道 |
| 多版本灰度并存 | 按 `version_distribution[0]`（占比最大版本）分桶 |
| 回滚（top_user_version 跌回） | 下次 cron 自动重算（缓存 TTL 6h 内有滞后但可接受） |
| build 号差异 | 忽略，按 semver 前缀判等 |

## 4. 三通道触发阈值

### 通道 1：新版本桶（C3）

```python
trigger_new_version = (
    events_3h >= cfg.new_version_min_events            # 默认 30
    AND
    crash_users_3h / new_version_total_users >= cfg.new_version_user_rate_pct  # 默认 0.005 (0.5%)
    AND
    sessions_3h >= cfg.hourly_alert_min_sessions       # 复用 500
    AND
    issue_id NOT IN dedup_set_12h
)
```

**分母**：`top_user_version_by_platform` 返回的 `users`（过去 24h 该版本独立用户数）。

### 通道 2：大盘桶（保持现状）

无改动，保留上一轮已部署的：SHoW+10% / rate-AND-check / events≥200 / sessions≥500 / min_baseline≥20 / dedup_12h。

### 通道 3：全局新 crash 兜底（D3）

```python
trigger_new_crash = (
    first_seen_at >= now - timedelta(days=cfg.new_window_days)  # 默认 30
    AND events_24h >= cfg.new_crash_min_events                  # 默认 150
    AND sessions_24h >= cfg.new_crash_min_sessions              # 默认 300
    AND issue_id NOT IN dedup_set_12h
)
```

**为何 24h 累计**：新 crash 兜底优先看"量级"——3h 段抖动易致误报，24h 滚动让 150 events 判定更稳健。

### 多通道合卡逻辑

```python
processed_ids = set()
for channel in [ch1_new_version, ch3_new_crash, ch2_main]:  # 优先级顺序
    for hit in channel:
        if hit.issue_id in processed_ids:
            continue
        processed_ids.add(hit.issue_id)
        emit_alert(hit, channel_tag=channel.tag)
```

## 5. Datadog 缓存抓手

### 5.1 为什么要缓存

- `top_user_version_by_platform` 数据变化粒度为天级（用户分布不会每 3h 大变）
- 现状每 3h cron 调一次 → 8 次/天浪费
- 24h 累计数据同理，多次 cron 重复拉同一窗口

### 5.2 实现（进程内 dict + TTL）

```python
# backend/app/crashguard/services/datadog_cache.py（新文件）

import time
from typing import Any

class DatadogCache:
    """通用 TTL 缓存——单实例 Docker 部署，进程内 dict 足够"""
    _cache: dict = {}
    _expires_at: dict = {}

    @classmethod
    async def get_or_fetch(cls, key: str, ttl_seconds: int, fetch_fn):
        now = time.time()
        if key in cls._cache and cls._expires_at[key] > now:
            return cls._cache[key]
        data = await fetch_fn()
        cls._cache[key] = data
        cls._expires_at[key] = now + ttl_seconds
        return data
```

### 5.3 缓存配置

| 数据 | TTL | 节省 |
|------|-----|------|
| `top_user_version_by_platform(24h)` | 6h | 8 → 4 次/天（-50%）|
| `issue_events_24h_rolling` | 6h | 8 → 4 次/天（-50%）|

**为什么不上 Redis**：jarvis 单实例 Docker 部署，进程内 dict 够用。引入 Redis 是过度工程。进程重启后第一次 cron 自动回填，影响窗口 ≤3h，可接受。

## 6. 实现拆解

### 6.1 文件级改动清单

| 文件 | 操作 | 内容 |
|------|------|------|
| `backend/app/crashguard/services/datadog_cache.py` | **新建** | 通用 TTL 缓存（top_user_version + 24h 累计数据）|
| `backend/app/crashguard/services/version_classifier.py` | **新建** | `classify_version()` + semver 比较（B2+B4 逻辑独立 module）|
| `backend/app/crashguard/services/hourly_alerter.py` | 改造 | 主 `run()` 拆三通道分发：`_dispatch_channel_1/2/3` + 合卡 dedup |
| `backend/app/crashguard/services/feishu_card_builder.py` | 加 tag | `[新版本]🔴 / [新crash]🟠 / [主版本]🟡` + first_seen 行 |
| `backend/app/crashguard/config.py` | 加字段 | 6 个新配置（见下）|
| `config.yaml` | 加默认值 | 同步默认 |
| `backend/tests/crashguard/test_hourly_alerter.py` | 加用例 | 8 个新测试 |

### 6.2 新增配置（全部有默认值，可热改）

```yaml
crashguard:
  hourly_alert:
    new_version:
      enabled: true
      shadow_mode: true              # Phase 0 影子模式，验证完手动改 false
      min_events: 30                 # 通道 1 events 地板
      user_rate_pct: 0.005           # 通道 1 用户占比 0.5%

    new_crash:
      enabled: true
      shadow_mode: true
      window_hours: 24               # 通道 3 累计窗口
      min_events: 150                # 通道 3 events 地板
      min_sessions: 300              # 通道 3 sessions 地板
```

### 6.3 卡片渲染示例

通道 1（新版本桶）：

```
🔴 [新版本] flutter_crash @ MainActivity.onCreate
   版本: v3.20.0 (新, 距主版本 v3.19.0 +1)
   首次出现版本: v3.20.0
   3h events: 87 | users: 23 | rate: 2.3% (>0.5%)
   👉 灰度新增 crash，疑似新版本引入
```

通道 3（新 crash 兜底）：

```
🟠 [新 crash] flutter_crash @ DeepLinkHandler.parse
   版本: 全平台
   首次出现版本: v3.18.0（30 天内首现）
   24h events: 178 | sessions: 412
   👉 近 30 天首次出现，全网兜底告警
```

通道 2（主版本，保持现状）：

```
🟡 [主版本] flutter_crash @ AudioPlayer.dispose
   版本: v3.19.0 (主, 78% 用户)
   首次出现版本: v3.16.0
   3h events: 320 (SHoW +35%) | rate +0.4pp
   👉 主版本上老 crash 恶化
```

## 7. 测试矩阵

新增 8 个 pytest 用例（`test_hourly_alerter.py`）：

| 用例 | 覆盖 |
|------|------|
| `test_channel_1_new_version_triggers_at_threshold` | 通道 1 触发 |
| `test_channel_1_new_version_blocked_by_user_rate` | 通道 1 user_rate 不够时不触发 |
| `test_channel_1_new_version_blocked_by_min_events` | 通道 1 events 不够时不触发 |
| `test_channel_3_new_crash_triggers_at_threshold` | 通道 3 触发 |
| `test_channel_3_new_crash_blocked_by_old_first_seen` | first_seen > 30 天不触发 |
| `test_channel_3_new_crash_blocked_by_events` | events < 150 不触发 |
| `test_multi_channel_merge_keeps_highest_priority_tag` | 合卡优先级 |
| `test_cache_hit_skips_datadog_call` | 缓存命中不调 API |

## 8. 灰度发布 + 回滚抓手

### Phase 0：24h 影子模式

代码上线但**不发卡片**——只写 audit log 到 `crash_hourly_alerts` 表，channel 字段标 `shadow_*`。看新通道一天会触发多少次，校对阈值。

### Phase 1：真发卡片（T+24h）

`shadow_mode: false` 双通道开启。观察 1 周。

### Phase 2 调参（T+1 周）

若误报率超预期：
- `new_version.user_rate_pct` 0.5% → 1%
- `new_crash.min_events` 150 → 200

若漏报：反向收紧（提敏感度）。

### 回滚抓手

```yaml
new_version:
  enabled: false   # ← 一刀切回滚
new_crash:
  enabled: false
```

hourly_alerter 立即回到现状逻辑。无数据库 schema 变更，无 rollback 包袱。

## 9. 验证脚本（部署后）

```bash
# 1. 缓存命中率
docker compose exec backend python -c "
from app.crashguard.services.datadog_cache import DatadogCache
print('Cache keys:', list(DatadogCache._cache.keys()))
"

# 2. 24h 三通道触发统计（影子模式后跑）
docker compose exec backend python -c "
from app.crashguard.db.session import get_session
import asyncio, json
async def main():
    async with get_session() as s:
        rows = await s.execute(
            'SELECT json_extract(alert_payload, \"\$.channel\") as ch, COUNT(*) '
            'FROM crash_hourly_alerts '
            'WHERE created_at >= datetime(\"now\",\"-1 day\") '
            'GROUP BY ch'
        )
        for r in rows: print(r)
asyncio.run(main())
"
```

## 10. 时间线

| T+ | 动作 |
|----|------|
| 0 | 写代码 + 单测（预估 2h）|
| 0.5h | 本机 pytest 全绿 → push → 部署 102 影子模式 |
| 24h | 看 audit log 校阈值 |
| 25h | `shadow_mode=false` 真发卡片 |
| 1 周 | 看告警体感，决定要不要进 Phase 2 调参 |

## 11. 风险与权衡

| 风险 | 缓解 |
|------|------|
| 缓存 TTL 6h 内版本切换（如紧急回滚）滞后 | 影响 ≤6h，且回滚场景本来就罕见——值得换 API 调用减半 |
| 通道 3 24h 累计意味着首次 cron 后 24h 内才有完整数据 | 影子模式期已覆盖；真发卡前必有完整数据 |
| 三通道合卡优先级有歧义（如同时 [新版本] + [新crash]）| 标签按 `[新版本] > [新 crash] > [主版本]` 固化，避免运行时摇摆 |
| 进程重启缓存失效 | 单实例 Docker，重启频率低；首次 cron 自动回填，影响窗口 ≤3h |

## 12. 不在本期范围（YAGNI）

- ❌ Release calendar 表（按发布时间识别新版本）——靠 top_user_version 比较即可
- ❌ Redis/Memcached 缓存——单实例 dict 够用
- ❌ 版本号 RC/Beta 阶段细分——按现行 semver 即可
- ❌ Per-platform 阈值差异化——所有平台共用阈值起步，1 周后看数据再决定

## 13. 设计完成检查

- [x] 顶层架构图（Section 2）
- [x] 版本识别细节（Section 3）
- [x] 三通道触发条件（Section 4）
- [x] 缓存方案（Section 5）
- [x] 实现拆解（Section 6）
- [x] 测试矩阵（Section 7）
- [x] 灰度 + 回滚（Section 8）
- [x] 验证脚本（Section 9）
- [x] 时间线（Section 10）
- [x] 风险表（Section 11）
- [x] YAGNI 边界（Section 12）
