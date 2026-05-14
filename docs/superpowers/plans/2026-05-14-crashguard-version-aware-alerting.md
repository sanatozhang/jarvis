# Crashguard 按版本切片告警 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hourly alerter 拆三通道（新版本桶/大盘桶/新 crash 兜底），Datadog 调用走 TTL 缓存，新版本灰度阶段用 user_rate 主信号 + events 地板兜底，全局新 crash 走 24h 累计窗口。

**Architecture:**
- 复用 `version_util.parse_semver` + `datadog_client.top_user_version_by_platform`，不重造轮子
- 在现有 `run_hourly_alert_tick` 中插入"版本分类"步骤，按桶分发触发逻辑
- 新增 `datadog_cache.py` 进程内 TTL 缓存（单实例 Docker，无需 Redis）
- 影子模式（`shadow_mode=true`）24h 校阈值后再真发卡片

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2.x async / pytest-asyncio / Pydantic Settings

**Spec:** `docs/superpowers/specs/2026-05-14-crashguard-version-aware-alerting-design.md`

---

## File Structure

| 路径 | 操作 | 职责 |
|------|------|------|
| `backend/app/crashguard/services/version_classifier.py` | **新建** | `classify_version()` — 输入 issue version + top_user_version dict，返回 `"new" / "main" / "legacy"` |
| `backend/app/crashguard/services/datadog_cache.py` | **新建** | 通用 TTL 缓存（进程内 dict，无外部依赖） |
| `backend/app/crashguard/services/hourly_alerter.py` | 改造 | 主 `run_hourly_alert_tick`：拉 top_user_version → 按 issue 分桶 → 三通道独立触发 → 合卡 dedup |
| `backend/app/crashguard/services/feishu_card.py` | 改造 | `build_hourly_alert_card` 增加 channel tag + first_seen_version 行 |
| `backend/app/crashguard/config.py` | 加字段 | `hourly_alert_new_version_*`（4 字段）+ `hourly_alert_new_crash_*`（4 字段） |
| `config.yaml` | 加默认值 | `crashguard.hourly_alert.new_version` + `crashguard.hourly_alert.new_crash` |
| `backend/tests/crashguard/test_version_classifier.py` | **新建** | 9 个 case：new/main/legacy/build号/边界/缺失 |
| `backend/tests/crashguard/test_datadog_cache.py` | **新建** | 4 个 case：miss→fetch、hit、TTL 过期、并发 |
| `backend/tests/crashguard/test_hourly_alerter.py` | 加用例 | 8 个新测试：通道 1 触发/不触发、通道 3 触发/不触发、合卡优先级、shadow_mode 等 |

---

## Task 1: version_classifier 模块（薄包装，复用 version_util）

**Files:**
- Create: `backend/app/crashguard/services/version_classifier.py`
- Test: `backend/tests/crashguard/test_version_classifier.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/crashguard/test_version_classifier.py
import pytest
from app.crashguard.services.version_classifier import classify_version


TOP = {
    "android": {"version": "3.19.0-634", "users": 12345},
    "ios": {"version": "3.18.0-712", "users": 9876},
}


def test_classify_version_new_when_greater():
    # 3.20.0 > 3.19.0 (主版本) → new
    assert classify_version("3.20.0-700", "android", TOP) == "new"


def test_classify_version_main_when_equal_ignoring_build():
    # 3.19.0-700 vs top 3.19.0-634，忽略 build → main
    assert classify_version("3.19.0-700", "android", TOP) == "main"


def test_classify_version_legacy_when_less():
    assert classify_version("3.16.0-500", "android", TOP) == "legacy"


def test_classify_version_unknown_platform_returns_legacy():
    # 平台不在 top dict → 归 legacy（走大盘桶兜底）
    assert classify_version("3.20.0", "windows", TOP) == "legacy"


def test_classify_version_empty_version_returns_legacy():
    assert classify_version("", "android", TOP) == "legacy"


def test_classify_version_unparseable_returns_legacy():
    assert classify_version("abc-xyz", "android", TOP) == "legacy"


def test_classify_version_empty_top_returns_legacy():
    assert classify_version("3.20.0", "android", {}) == "legacy"


def test_classify_version_top_missing_version_field():
    assert classify_version("3.20.0", "android", {"android": {}}) == "legacy"


def test_classify_version_minor_bump_is_new():
    # 3.19.1 > 3.19.0 → new（patch bump 也算新）
    assert classify_version("3.19.1-650", "android", TOP) == "new"
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd backend && pytest tests/crashguard/test_version_classifier.py -v
```

Expected: 9 FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现模块**

```python
# backend/app/crashguard/services/version_classifier.py
"""
版本分类器：把 issue 的 version 字段映射到三个桶之一。

底层逻辑：复用 version_util.parse_semver 做 semver 比较，忽略 -build 号。
"""
from __future__ import annotations
from typing import Literal

from app.crashguard.services.version_util import parse_semver


VersionBucket = Literal["new", "main", "legacy"]


def classify_version(
    issue_version: str,
    platform: str,
    top_versions: dict,
) -> VersionBucket:
    """把 issue 按版本归到三桶之一。

    Args:
        issue_version: issue 的 last_seen_version / app_version
        platform: 平台 key（必须在 top_versions 里有同名 key）
        top_versions: {platform: {"version": str, "users": int}}

    Returns:
        "new" — issue_version semver > top_version
        "main" — issue_version semver == top_version（忽略 build 号）
        "legacy" — issue_version < top_version，或解析失败/数据缺失
    """
    if not issue_version or not platform:
        return "legacy"

    platform_data = top_versions.get(platform)
    if not platform_data:
        return "legacy"

    top_ver = platform_data.get("version") or ""
    if not top_ver:
        return "legacy"

    issue_parsed = parse_semver(issue_version)
    top_parsed = parse_semver(top_ver)
    if issue_parsed is None or top_parsed is None:
        return "legacy"

    # 只比较 (major, minor, patch)，忽略 suffix（build 号）
    issue_key = issue_parsed[:3]
    top_key = top_parsed[:3]

    if issue_key > top_key:
        return "new"
    elif issue_key == top_key:
        return "main"
    return "legacy"
```

- [ ] **Step 4: 跑测试验证通过**

```bash
cd backend && pytest tests/crashguard/test_version_classifier.py -v
```

Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/crashguard/services/version_classifier.py backend/tests/crashguard/test_version_classifier.py
git commit -m "feat(crashguard): version_classifier 模块——按 semver 把 issue 归三桶（new/main/legacy）"
```

---

## Task 2: datadog_cache 模块

**Files:**
- Create: `backend/app/crashguard/services/datadog_cache.py`
- Test: `backend/tests/crashguard/test_datadog_cache.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/crashguard/test_datadog_cache.py
import pytest
import time
from app.crashguard.services.datadog_cache import DatadogCache


@pytest.fixture(autouse=True)
def _clear_cache():
    DatadogCache.clear()
    yield
    DatadogCache.clear()


@pytest.mark.asyncio
async def test_cache_miss_calls_fetch():
    calls = []
    async def fetch():
        calls.append(1)
        return {"data": "v1"}
    result = await DatadogCache.get_or_fetch("k1", ttl_seconds=10, fetch_fn=fetch)
    assert result == {"data": "v1"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cache_hit_skips_fetch():
    calls = []
    async def fetch():
        calls.append(1)
        return {"data": "v1"}
    await DatadogCache.get_or_fetch("k2", ttl_seconds=10, fetch_fn=fetch)
    await DatadogCache.get_or_fetch("k2", ttl_seconds=10, fetch_fn=fetch)
    await DatadogCache.get_or_fetch("k2", ttl_seconds=10, fetch_fn=fetch)
    assert len(calls) == 1   # 只调一次


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr("app.crashguard.services.datadog_cache.time.time",
                        lambda: fake_now[0])
    calls = []
    async def fetch():
        calls.append(1)
        return {"i": len(calls)}
    await DatadogCache.get_or_fetch("k3", ttl_seconds=5, fetch_fn=fetch)
    fake_now[0] = 1006.0   # 跳到 6 秒后，过期
    result = await DatadogCache.get_or_fetch("k3", ttl_seconds=5, fetch_fn=fetch)
    assert result == {"i": 2}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cache_isolated_by_key():
    async def fetch_a():
        return "A"
    async def fetch_b():
        return "B"
    a = await DatadogCache.get_or_fetch("ka", ttl_seconds=10, fetch_fn=fetch_a)
    b = await DatadogCache.get_or_fetch("kb", ttl_seconds=10, fetch_fn=fetch_b)
    assert a == "A"
    assert b == "B"
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd backend && pytest tests/crashguard/test_datadog_cache.py -v
```

Expected: 4 FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现模块**

```python
# backend/app/crashguard/services/datadog_cache.py
"""
进程内 TTL 缓存：降低 Datadog API 调用频率。

底层逻辑：jarvis 单实例 Docker 部署，dict + TTL 足够；不引入 Redis（YAGNI）。
进程重启缓存失效，首次 cron 自动回填，影响窗口 ≤3h。
"""
from __future__ import annotations
import time
from typing import Any, Awaitable, Callable


class DatadogCache:
    _cache: dict = {}
    _expires_at: dict = {}

    @classmethod
    async def get_or_fetch(
        cls,
        key: str,
        ttl_seconds: int,
        fetch_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """命中返回缓存，未命中/过期则调 fetch_fn 并缓存。"""
        now = time.time()
        if key in cls._cache and cls._expires_at.get(key, 0) > now:
            return cls._cache[key]
        data = await fetch_fn()
        cls._cache[key] = data
        cls._expires_at[key] = now + ttl_seconds
        return data

    @classmethod
    def clear(cls) -> None:
        """测试钩子：清空缓存。"""
        cls._cache.clear()
        cls._expires_at.clear()

    @classmethod
    def stats(cls) -> dict:
        """供 audit / 验证脚本用。"""
        return {"keys": list(cls._cache.keys()), "count": len(cls._cache)}
```

- [ ] **Step 4: 跑测试验证通过**

```bash
cd backend && pytest tests/crashguard/test_datadog_cache.py -v
```

Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/crashguard/services/datadog_cache.py backend/tests/crashguard/test_datadog_cache.py
git commit -m "feat(crashguard): datadog_cache 模块——进程内 TTL 缓存降 API 调用频率"
```

---

## Task 3: config 字段 + config.yaml 默认值

**Files:**
- Modify: `backend/app/crashguard/config.py`
- Modify: `config.yaml`
- Test: `backend/tests/crashguard/test_config.py`（如已有则加 case；否则跳过测试步骤）

- [ ] **Step 1: 在 config.py 加 8 个字段**

打开 `backend/app/crashguard/config.py`，找到现有 `hourly_alert_dedup_hours` 字段定义所在的 Settings 类（应该在 hourly_alert 字段附近）。在其后追加：

```python
    # ===== 通道 1：新版本桶（C3）=====
    hourly_alert_new_version_enabled: bool = True
    hourly_alert_new_version_shadow_mode: bool = True       # Phase 0 影子模式，仅写 audit log 不发卡
    hourly_alert_new_version_min_events: int = 30           # events 地板
    hourly_alert_new_version_user_rate_pct: float = 0.005   # 0.5% 用户占比

    # ===== 通道 3：全局新 crash 兜底（D3）=====
    hourly_alert_new_crash_enabled: bool = True
    hourly_alert_new_crash_shadow_mode: bool = True
    hourly_alert_new_crash_window_hours: int = 24           # 累计窗口
    hourly_alert_new_crash_min_events: int = 150            # events 地板
    hourly_alert_new_crash_min_sessions: int = 300          # sessions 地板
```

- [ ] **Step 2: 在 yaml 解析逻辑里映射 8 字段**

在 `config.py` 中找到 yaml 映射函数（通常叫 `_load_from_yaml` 或类似），找到现有 `min_events_absolute` / `dedup_hours` 的映射逻辑。在其后追加：

```python
        # 通道 1 / 3 配置
        new_version = (hourly_alert_cfg or {}).get("new_version") or {}
        if "enabled" in new_version:
            settings.hourly_alert_new_version_enabled = bool(new_version["enabled"])
        if "shadow_mode" in new_version:
            settings.hourly_alert_new_version_shadow_mode = bool(new_version["shadow_mode"])
        if "min_events" in new_version:
            settings.hourly_alert_new_version_min_events = int(new_version["min_events"])
        if "user_rate_pct" in new_version:
            settings.hourly_alert_new_version_user_rate_pct = float(new_version["user_rate_pct"])

        new_crash = (hourly_alert_cfg or {}).get("new_crash") or {}
        if "enabled" in new_crash:
            settings.hourly_alert_new_crash_enabled = bool(new_crash["enabled"])
        if "shadow_mode" in new_crash:
            settings.hourly_alert_new_crash_shadow_mode = bool(new_crash["shadow_mode"])
        if "window_hours" in new_crash:
            settings.hourly_alert_new_crash_window_hours = int(new_crash["window_hours"])
        if "min_events" in new_crash:
            settings.hourly_alert_new_crash_min_events = int(new_crash["min_events"])
        if "min_sessions" in new_crash:
            settings.hourly_alert_new_crash_min_sessions = int(new_crash["min_sessions"])
```

> ⚠️ **如果当前文件结构与上面不一致**：阅读 config.py 实际的 yaml 解析代码，按相同模式补齐。关键是这 8 个 yaml key 必须能写入 settings。

- [ ] **Step 3: 在 config.yaml 加默认值块**

在 `config.yaml` 中找到 `crashguard.hourly_alert` 块（已有 `dedup_hours: 12` 那段），追加：

```yaml
  hourly_alert:
    # ... 现有字段保留 ...

    # 通道 1：新版本桶（按版本切片，user_rate 主信号）
    new_version:
      enabled: true
      shadow_mode: true            # Phase 0 影子模式，验证 24h 后改 false
      min_events: 30
      user_rate_pct: 0.005         # 0.5%

    # 通道 3：全局新 crash 兜底（24h 累计 + events 量级）
    new_crash:
      enabled: true
      shadow_mode: true
      window_hours: 24
      min_events: 150
      min_sessions: 300
```

- [ ] **Step 4: 验证配置加载**

```bash
cd backend && python -c "
from app.crashguard.config import get_crashguard_settings
s = get_crashguard_settings()
print('new_version.enabled:', s.hourly_alert_new_version_enabled)
print('new_version.shadow_mode:', s.hourly_alert_new_version_shadow_mode)
print('new_version.min_events:', s.hourly_alert_new_version_min_events)
print('new_version.user_rate_pct:', s.hourly_alert_new_version_user_rate_pct)
print('new_crash.window_hours:', s.hourly_alert_new_crash_window_hours)
print('new_crash.min_events:', s.hourly_alert_new_crash_min_events)
print('new_crash.min_sessions:', s.hourly_alert_new_crash_min_sessions)
"
```

Expected output:
```
new_version.enabled: True
new_version.shadow_mode: True
new_version.min_events: 30
new_version.user_rate_pct: 0.005
new_crash.window_hours: 24
new_crash.min_events: 150
new_crash.min_sessions: 300
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/crashguard/config.py config.yaml
git commit -m "feat(crashguard): 通道 1/3 配置字段（新版本桶 + 新 crash 兜底）"
```

---

## Task 4: hourly_alerter 接入"版本分桶 + 通道 1（新版本桶）"

**Files:**
- Modify: `backend/app/crashguard/services/hourly_alerter.py:175-423`
- Test: `backend/tests/crashguard/test_hourly_alerter.py`

> **核心改动**：在现有 `run_hourly_alert_tick` 主循环中插入"按 issue version 分桶"步骤；为分到 `new` 桶的 issue 走通道 1 触发逻辑（user_rate + min_events，不走 SHoW）。`main` / `legacy` 走现状逻辑（通道 2）。

- [ ] **Step 1: 写失败测试 — 通道 1 触发**

打开 `backend/tests/crashguard/test_hourly_alerter.py`，在文件末尾追加：

```python
async def test_channel_1_new_version_triggers_when_user_rate_meets():
    """通道 1：events >= 30 AND user_rate >= 0.5% → 触发，标签 'new_version'"""
    monkeypatch_env(monkeypatch={
        "CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED": "true",
        "CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE": "false",
        "CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS": "30",
        "CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT": "0.005",
    })
    # Mock top_user_version: 主版本 3.19.0；issue 在 3.20.0 = new
    mock_top_versions = {"android": {"version": "3.19.0-600", "users": 10000}}
    mock_datadog_issues = [{
        "id": "test_new_ver_1",
        "attributes": {
            "events_count": 50,                # >= 30 ✓
            "sessions_affected": 800,           # >= 500 ✓
            "title": "NewVersionCrash",
            "platform": "android",
            "version": "3.20.0-700",            # > top → new 桶
        },
    }]
    # crash_users = 50 (events) — 简化口径：按 events 当 user 代理
    # 50 / 10000 = 0.005 = 0.5% ✓
    with patch_top_user_version(mock_top_versions), patch_fetch_hourly(mock_datadog_issues):
        result = await run_hourly_alert_tick(force=True)
    assert result["alerted"] is True
    assert result["new_version"] == 1   # 通道 1 命中数
```

> **说明**：`monkeypatch_env` / `patch_top_user_version` / `patch_fetch_hourly` 是该测试文件已有/将要新增的辅助。先看现有 test_hourly_alerter.py 是怎么 mock `_fetch_hourly_events` 的，对照同款。如果暂无 helper，直接在测试函数体内用 `unittest.mock.patch` 替代。

- [ ] **Step 2: 跑测试验证失败**

```bash
cd backend && pytest tests/crashguard/test_hourly_alerter.py::test_channel_1_new_version_triggers_when_user_rate_meets -v
```

Expected: FAIL（KeyError: 'new_version' 或类似——还没实现）

- [ ] **Step 3: 在 hourly_alerter 主流程插入"分桶"步骤**

打开 `backend/app/crashguard/services/hourly_alerter.py`，在 `run_hourly_alert_tick` 内 `_fetch_hourly_events` 后、主循环前插入：

```python
    # === 取 top_user_version_by_platform（缓存 6h）===
    from app.crashguard.services.datadog_cache import DatadogCache
    from app.crashguard.services.datadog_client import DatadogClient
    from app.crashguard.services.version_classifier import classify_version

    async def _fetch_top_ver():
        client = DatadogClient(...)   # ← 按现有构造方式
        return await client.top_user_version_by_platform(window_hours=24)

    try:
        top_versions = await DatadogCache.get_or_fetch(
            key="top_user_version:24",
            ttl_seconds=6 * 3600,
            fetch_fn=_fetch_top_ver,
        )
    except Exception:
        logger.exception("hourly_alerter: top_user_version fetch failed, fallback empty")
        top_versions = {}
```

> ⚠️ **DatadogClient 构造**：阅读现有 `_fetch_hourly_events` 怎么构造的——通常是 `DatadogClient(api_key=..., app_key=...)` 或从 config 取。按现有 pattern 复用，不要发明新构造方式。

- [ ] **Step 4: 在主循环里给每个 issue 分桶 + 通道 1 触发逻辑**

继续修改 `hourly_alerter.py`。在主循环 `for raw in raw_issues:` 内，在现有 "查 issue 元信息" 之后、`if is_new:` 分支之前插入：

```python
            # === 版本分桶 ===
            issue_ver = (attrs.get("version") or
                         (issue_row.last_seen_version if issue_row else "")) or ""
            bucket = classify_version(issue_ver, platform.lower(), top_versions)

            # === 通道 1：新版本桶（C3）===
            if bucket == "new" and s.hourly_alert_new_version_enabled:
                # user_rate 分母：top_user_version[platform]["users"]
                denom = (top_versions.get(platform.lower()) or {}).get("users") or 0
                user_rate = (events_h / denom) if denom > 0 else None
                pass_min_events = events_h >= s.hourly_alert_new_version_min_events
                pass_user_rate = (user_rate is not None
                                  and user_rate >= s.hourly_alert_new_version_user_rate_pct)
                if pass_min_events and pass_user_rate:
                    new_version_items.append({
                        "issue_id": issue_id,
                        "title": title[:100],
                        "platform": platform,
                        "version": issue_ver,
                        "first_seen_version": (issue_row.first_seen_version
                                               if issue_row else "") or "",
                        "events_h": events_h,
                        "sessions_h": sessions_h,
                        "user_rate_pct": round((user_rate or 0) * 100, 3),
                    })
                continue   # 命中通道 1 不再走大盘逻辑

            # === 通道 2（主版本/旧版本）→ 继续现有逻辑 ===
```

并在函数顶部初始化新桶 list（与 `new_items` / `surge_items` 并列）：

```python
    new_version_items: List[Dict[str, Any]] = []
```

- [ ] **Step 5: 在 alert 入库 + payload 里加 channel 字段**

找到现有 `payload = {...}` 那段，改为：

```python
    payload = {
        "new": new_items, "surge": surge_items,
        "new_version": new_version_items,   # ← 新增通道 1
        "threshold_pct": s.hourly_alert_growth_threshold_pct,
        "min_sessions": min_sessions,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    }
```

`if not new_items and not surge_items:` 改为：

```python
    if not new_items and not surge_items and not new_version_items:
```

- [ ] **Step 6: 跑测试验证通过**

```bash
cd backend && pytest tests/crashguard/test_hourly_alerter.py -v
```

Expected: 现有 + 新通道 1 用例全 PASS

- [ ] **Step 7: 加 2 个负向测试 — 通道 1 不触发**

```python
async def test_channel_1_blocked_by_user_rate():
    """user_rate < 0.5% → 不触发通道 1"""
    monkeypatch_env(monkeypatch={
        "CRASHGUARD_HOURLY_ALERT_NEW_VERSION_ENABLED": "true",
        "CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE": "false",
        "CRASHGUARD_HOURLY_ALERT_NEW_VERSION_MIN_EVENTS": "30",
        "CRASHGUARD_HOURLY_ALERT_NEW_VERSION_USER_RATE_PCT": "0.005",
    })
    mock_top_versions = {"android": {"version": "3.19.0-600", "users": 1_000_000}}
    mock_datadog_issues = [{
        "id": "test_low_rate", "attributes": {
            "events_count": 50, "sessions_affected": 800,
            "title": "LowRate", "platform": "android", "version": "3.20.0",
        }
    }]
    # 50 / 1_000_000 = 0.00005 = 0.005% << 0.5%
    with patch_top_user_version(mock_top_versions), patch_fetch_hourly(mock_datadog_issues):
        result = await run_hourly_alert_tick(force=True)
    assert result.get("new_version", 0) == 0


async def test_channel_1_blocked_by_min_events():
    """events < 30 → 不触发"""
    # 同上结构，events_count=20，预期 new_version=0
```

跑测试：`pytest tests/crashguard/test_hourly_alerter.py -v` → 全绿。

- [ ] **Step 8: Commit**

```bash
git add backend/app/crashguard/services/hourly_alerter.py backend/tests/crashguard/test_hourly_alerter.py
git commit -m "feat(crashguard): hourly_alerter 通道 1——新版本桶按 user_rate + events 地板触发"
```

---

## Task 5: hourly_alerter 通道 3（全局新 crash 兜底，24h 累计窗口）

**Files:**
- Modify: `backend/app/crashguard/services/hourly_alerter.py`（同上）
- Test: `backend/tests/crashguard/test_hourly_alerter.py`

> **核心改动**：在主循环外（独立于按 raw_issues 分桶）增加一段"拉 24h 累计数据 + 筛 first_seen 在 30 天内 + events ≥ 150 + sessions ≥ 300"的逻辑。结果写入 `new_crash_items`，和通道 1/2 合卡 dedup。

- [ ] **Step 1: 写失败测试 — 通道 3 触发**

在 `test_hourly_alerter.py` 追加：

```python
async def test_channel_3_new_crash_triggers_when_thresholds_met():
    """24h events >= 150 AND sessions >= 300 AND first_seen <= 30 天 → 触发"""
    monkeypatch_env(monkeypatch={
        "CRASHGUARD_HOURLY_ALERT_NEW_CRASH_ENABLED": "true",
        "CRASHGUARD_HOURLY_ALERT_NEW_CRASH_SHADOW_MODE": "false",
        "CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_EVENTS": "150",
        "CRASHGUARD_HOURLY_ALERT_NEW_CRASH_MIN_SESSIONS": "300",
    })
    # 24h 累计数据 mock
    mock_24h_issues = [{
        "id": "newly_emerged_1",
        "first_seen_days_ago": 10,   # ≤ 30 ✓
        "events_24h": 200,            # ≥ 150 ✓
        "sessions_24h": 400,          # ≥ 300 ✓
        "platform": "ios",
        "version": "3.18.0",
        "title": "NewlyEmergedBug",
    }]
    with patch_24h_window(mock_24h_issues):
        result = await run_hourly_alert_tick(force=True)
    assert result.get("new_crash", 0) == 1


async def test_channel_3_blocked_by_old_first_seen():
    """first_seen > 30 天 → 不触发"""
    mock_24h_issues = [{
        "id": "old_issue", "first_seen_days_ago": 60,
        "events_24h": 500, "sessions_24h": 1000,
        "platform": "ios", "version": "3.10.0",
    }]
    with patch_24h_window(mock_24h_issues):
        result = await run_hourly_alert_tick(force=True)
    assert result.get("new_crash", 0) == 0


async def test_channel_3_blocked_by_low_events():
    mock_24h_issues = [{
        "id": "low_event", "first_seen_days_ago": 5,
        "events_24h": 100,            # < 150
        "sessions_24h": 500,
        "platform": "ios", "version": "3.18.0",
    }]
    with patch_24h_window(mock_24h_issues):
        result = await run_hourly_alert_tick(force=True)
    assert result.get("new_crash", 0) == 0
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd backend && pytest tests/crashguard/test_hourly_alerter.py::test_channel_3_new_crash_triggers_when_thresholds_met -v
```

Expected: FAIL（KeyError: 'new_crash' / patch_24h_window 未定义）

- [ ] **Step 3: 增加 24h 累计数据拉取（带缓存）**

在 `hourly_alerter.py` 中现有 `_fetch_hourly_events` 之后，新增函数：

```python
async def _fetch_24h_issues_for_new_crash() -> List[Dict[str, Any]]:
    """拉 24h 累计 issues 列表，供通道 3 用。

    复用 DatadogClient 的 fatal 查询；窗口设为 24h。
    走 DatadogCache，TTL 6h（每 4 个 3h cron tick 才真拉一次）。
    """
    from app.crashguard.services.datadog_cache import DatadogCache

    async def _do_fetch():
        # 直接复用 DatadogClient.search_issues / 同款 query
        # 参考现有 _fetch_hourly_events 怎么构造，把 window_hours 改 24
        ...

    return await DatadogCache.get_or_fetch(
        key="hourly_alert:new_crash:24h",
        ttl_seconds=6 * 3600,
        fetch_fn=_do_fetch,
    )
```

> ⚠️ **关键点**：实现 `_do_fetch` 时，复用现有 `DatadogClient.search_issues` 的方法签名——不要重写 query。阅读 `datadog_client.py` 找最贴近"24h 内全 issue events 累计"的方法。如果没现成的，可以拿现有 3h 数据的 query template，把 `start_ms / end_ms` 改成 24h。

- [ ] **Step 4: 在 run_hourly_alert_tick 里跑通道 3**

在主 3h 循环结束后、`payload = {...}` 之前追加：

```python
    # === 通道 3：全局新 crash 兜底（24h 累计）===
    new_crash_items: List[Dict[str, Any]] = []
    if s.hourly_alert_new_crash_enabled:
        try:
            raw_24h = await _fetch_24h_issues_for_new_crash()
        except Exception:
            logger.exception("hourly_alerter: new_crash 24h fetch failed")
            raw_24h = []

        new_crash_cutoff = now - timedelta(days=s.hourly_alert_new_window_days)
        async with get_session() as _s:
            for raw in raw_24h:
                iid = raw.get("id") or ""
                if not iid or iid in dedup_set:
                    continue
                # 看 DB 里的 first_seen_at（拉 Datadog 时 attribute 可能没带）
                issue_row = (await _s.execute(
                    select(CrashIssue).where(CrashIssue.datadog_issue_id == iid)
                )).scalars().first()
                first_seen = issue_row.first_seen_at if issue_row else None
                if first_seen is None or first_seen < new_crash_cutoff:
                    continue
                attrs = raw.get("attributes") or {}
                ev24 = int(attrs.get("events_count") or 0)
                ses24 = int(attrs.get("sessions_affected") or 0)
                if ev24 < s.hourly_alert_new_crash_min_events:
                    continue
                if ses24 < s.hourly_alert_new_crash_min_sessions:
                    continue
                new_crash_items.append({
                    "issue_id": iid,
                    "title": (issue_row.title if issue_row else
                              attrs.get("title") or iid)[:100],
                    "platform": (issue_row.platform if issue_row else
                                 attrs.get("platform") or ""),
                    "first_seen_version": (issue_row.first_seen_version
                                           if issue_row else "") or "",
                    "first_seen_at": first_seen.isoformat() if first_seen else None,
                    "events_24h": ev24,
                    "sessions_24h": ses24,
                })
```

- [ ] **Step 5: 在 payload + 命中检查里加入 new_crash_items**

```python
    payload = {
        "new": new_items, "surge": surge_items,
        "new_version": new_version_items,
        "new_crash": new_crash_items,        # ← 新增
        ...
    }
    if not new_items and not surge_items and not new_version_items and not new_crash_items:
        return {...}
```

返回 dict 里加 `"new_crash": len(new_crash_items)`。

- [ ] **Step 6: 跑测试**

```bash
cd backend && pytest tests/crashguard/test_hourly_alerter.py -v
```

Expected: 全绿

- [ ] **Step 7: Commit**

```bash
git add backend/app/crashguard/services/hourly_alerter.py backend/tests/crashguard/test_hourly_alerter.py
git commit -m "feat(crashguard): hourly_alerter 通道 3——全局新 crash 兜底（24h 累计 + events 地板）"
```

---

## Task 6: 三通道合卡 dedup + shadow_mode 实现

**Files:**
- Modify: `backend/app/crashguard/services/hourly_alerter.py`
- Test: `backend/tests/crashguard/test_hourly_alerter.py`

> **核心改动**：同 issue 命中多通道时，按优先级 `[新版本] > [新 crash] > [主版本]` 合卡。shadow_mode=true 时只写 audit log（alert_payload.channel 标 `shadow_*`），不发飞书卡。

- [ ] **Step 1: 写失败测试 — 多通道合卡优先级**

```python
async def test_multi_channel_merge_keeps_new_version_priority():
    """同 issue 同时被通道 1 + 通道 3 命中，最终卡片标 [新版本]"""
    # 构造 issue：版本 3.20.0（new bucket），且 first_seen 5 天内
    # → 同时满足通道 1（user_rate 达标）和通道 3（events_24h ≥ 150）
    ...
    assert result["alerted"] is True
    # 卡片或 payload 里该 issue 只出现一次，channel='new_version'


async def test_shadow_mode_writes_audit_but_skips_feishu():
    """shadow_mode=true 时不发飞书"""
    monkeypatch_env(monkeypatch={
        "CRASHGUARD_HOURLY_ALERT_NEW_VERSION_SHADOW_MODE": "true",
    })
    # 构造可触发通道 1 的数据
    ...
    with patch_feishu_send() as send_mock:
        result = await run_hourly_alert_tick(force=True)
    assert send_mock.call_count == 0    # 没发飞书
    assert result.get("shadow") is True
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd backend && pytest tests/crashguard/test_hourly_alerter.py::test_multi_channel_merge_keeps_new_version_priority tests/crashguard/test_hourly_alerter.py::test_shadow_mode_writes_audit_but_skips_feishu -v
```

Expected: FAIL

- [ ] **Step 3: 实现合卡 dedup**

在 hourly_alerter 中、payload 构造之前插入：

```python
    # === 多通道合卡 dedup：优先级 new_version > new_crash > main(surge)/new(new_items) ===
    seen_ids = set()
    deduped_new_version = []
    deduped_new_crash = []
    deduped_new_items = []
    deduped_surge = []
    for it in new_version_items:
        if it["issue_id"] in seen_ids:
            continue
        seen_ids.add(it["issue_id"])
        deduped_new_version.append(it)
    for it in new_crash_items:
        if it["issue_id"] in seen_ids:
            continue
        seen_ids.add(it["issue_id"])
        deduped_new_crash.append(it)
    for it in new_items:
        if it["issue_id"] in seen_ids:
            continue
        seen_ids.add(it["issue_id"])
        deduped_new_items.append(it)
    for it in surge_items:
        if it["issue_id"] in seen_ids:
            continue
        seen_ids.add(it["issue_id"])
        deduped_surge.append(it)

    new_version_items = deduped_new_version
    new_crash_items = deduped_new_crash
    new_items = deduped_new_items
    surge_items = deduped_surge
```

- [ ] **Step 4: 实现 shadow_mode**

在飞书发送之前判断：

```python
    shadow_mode_active = (
        (s.hourly_alert_new_version_shadow_mode and len(new_version_items) > 0
         and len(new_items) == 0 and len(surge_items) == 0 and len(new_crash_items) == 0)
        or
        (s.hourly_alert_new_crash_shadow_mode and len(new_crash_items) > 0
         and len(new_items) == 0 and len(surge_items) == 0 and len(new_version_items) == 0)
    )

    if shadow_mode_active:
        logger.info("hourly_alerter: shadow_mode active, skip feishu (payload audit-logged)")
        return {
            "ok": True, "alerted": False, "shadow": True,
            "new": len(new_items), "surge": len(surge_items),
            "new_version": len(new_version_items),
            "new_crash": len(new_crash_items),
            "hour_utc": now_hour.isoformat(),
        }
```

> ⚠️ **精细化策略**：上面的逻辑较粗——若有多通道混合命中（如通道 1 影子 + 通道 2 现状真发），按"只要有非影子通道就真发"原则。这里 shadow_mode_active 真值条件是"所有命中通道都处于影子模式"。

- [ ] **Step 5: 跑测试**

```bash
cd backend && pytest tests/crashguard/test_hourly_alerter.py -v
```

Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add backend/app/crashguard/services/hourly_alerter.py backend/tests/crashguard/test_hourly_alerter.py
git commit -m "feat(crashguard): hourly_alerter 三通道合卡 dedup + shadow_mode 影子发布"
```

---

## Task 7: feishu_card 加 channel tag + first_seen_version 行

**Files:**
- Modify: `backend/app/crashguard/services/feishu_card.py:141` 起的 `build_hourly_alert_card` 函数
- Test: `backend/tests/crashguard/test_feishu_card.py`

> **核心改动**：在 `build_hourly_alert_card` 的签名里加 `new_version_items` / `new_crash_items` 两个新参数；卡片渲染 3 段——`[新版本]🔴` `[新 crash]🟠` `[主版本]🟡`，每行追加 first_seen_version。

- [ ] **Step 1: 写失败测试**

打开 `backend/tests/crashguard/test_feishu_card.py`，追加：

```python
def test_build_hourly_alert_card_with_new_version_section():
    """卡片包含 [新版本]🔴 标签段"""
    card = build_hourly_alert_card(
        hour_utc=datetime(2026, 5, 14, 3, 0),
        new_items=[],
        surge_items=[],
        new_version_items=[{
            "issue_id": "x1", "title": "NewVer", "platform": "android",
            "version": "3.20.0", "first_seen_version": "3.20.0",
            "events_h": 50, "sessions_h": 800, "user_rate_pct": 0.65,
        }],
        new_crash_items=[],
        threshold_pct=10, frontend_base_url="http://x",
    )
    rendered = json.dumps(card, ensure_ascii=False)
    assert "新版本" in rendered or "🔴" in rendered
    assert "3.20.0" in rendered
    assert "首次出现版本" in rendered or "first_seen" in rendered.lower()


def test_build_hourly_alert_card_with_new_crash_section():
    card = build_hourly_alert_card(
        hour_utc=datetime(2026, 5, 14, 3, 0),
        new_items=[], surge_items=[],
        new_version_items=[],
        new_crash_items=[{
            "issue_id": "y1", "title": "NewCrash", "platform": "ios",
            "first_seen_version": "3.18.0",
            "first_seen_at": "2026-05-10T00:00:00",
            "events_24h": 200, "sessions_24h": 400,
        }],
        threshold_pct=10, frontend_base_url="http://x",
    )
    rendered = json.dumps(card, ensure_ascii=False)
    assert "新 crash" in rendered or "🟠" in rendered
    assert "200" in rendered
```

- [ ] **Step 2: 跑测试验证失败**

```bash
cd backend && pytest tests/crashguard/test_feishu_card.py -v
```

Expected: FAIL（TypeError: unexpected keyword 'new_version_items'）

- [ ] **Step 3: 改造 build_hourly_alert_card 签名**

打开 `backend/app/crashguard/services/feishu_card.py:141`，找到 `def build_hourly_alert_card(...)`：

- 加两个新参数 `new_version_items: list = None`、`new_crash_items: list = None`
- 在卡片 elements 里，在现有 `new_items` / `surge_items` section 之前插入两段新 section
- 每个 item 行渲染格式：

通道 1（🔴 新版本）：
```
🔴 {title}
版本: {version} | 首次出现: {first_seen_version}
3h events: {events_h} | sessions: {sessions_h} | user_rate: {user_rate_pct}%
```

通道 3（🟠 新 crash）：
```
🟠 {title}
首次出现版本: {first_seen_version} | 首现时间: {first_seen_at}
24h events: {events_24h} | sessions: {sessions_24h}
```

- [ ] **Step 4: 在 hourly_alerter 调用处传新参数**

回到 `hourly_alerter.py`，找到 `build_hourly_alert_card(...)` 调用，加：

```python
    card = build_hourly_alert_card(
        hour_utc=now_hour,
        new_items=new_items[: s.hourly_alert_max_items],
        surge_items=surge_items[: s.hourly_alert_max_items],
        new_version_items=new_version_items[: s.hourly_alert_max_items],  # ← 新
        new_crash_items=new_crash_items[: s.hourly_alert_max_items],      # ← 新
        threshold_pct=s.hourly_alert_growth_threshold_pct,
        frontend_base_url=s.frontend_base_url,
        alert_id=alert_id,
    )
```

- [ ] **Step 5: 跑测试**

```bash
cd backend && pytest tests/crashguard/test_feishu_card.py tests/crashguard/test_hourly_alerter.py -v
```

Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add backend/app/crashguard/services/feishu_card.py backend/app/crashguard/services/hourly_alerter.py backend/tests/crashguard/test_feishu_card.py
git commit -m "feat(crashguard): feishu_card 加 [新版本]/[新crash] 段 + first_seen_version 行"
```

---

## Task 8: 全量测试 + 影子模式部署

**Files:** 无新文件，纯运维

- [ ] **Step 1: 跑全部 crashguard 测试**

```bash
cd backend && pytest tests/crashguard/ -v
```

Expected: 现有 233 + 新增 ~21 = **254 PASS**（数字以实际为准；不能 FAIL/ERROR）

- [ ] **Step 2: 跑 import isolation lint**

```bash
cd backend && lint-imports
```

Expected: 全部白名单内（如果 datadog_cache.py 引入了新对外耦合需要加白名单——本方案不应触发）

- [ ] **Step 3: Push 到 origin**

> [铁律提醒 🟠] 此步需用户授权——若用户已说"开始实施"= 实施代码完成后 push，则可执行。否则等明确指令。

```bash
git push origin main   # 或当前分支
```

- [ ] **Step 4: 部署到 102 服务器**

```bash
sshpass -p 123456 ssh mac@10.0.52.102 "cd ~/jarvis && git pull && docker compose build backend && docker compose up -d backend"
```

- [ ] **Step 5: 验证配置生效**

```bash
sshpass -p 123456 ssh mac@10.0.52.102 "docker compose -f ~/jarvis/docker-compose.yml exec -T backend python -c '
from app.crashguard.config import get_crashguard_settings
s = get_crashguard_settings()
print(\"new_version.shadow_mode:\", s.hourly_alert_new_version_shadow_mode)
print(\"new_version.min_events:\", s.hourly_alert_new_version_min_events)
print(\"new_version.user_rate_pct:\", s.hourly_alert_new_version_user_rate_pct)
print(\"new_crash.shadow_mode:\", s.hourly_alert_new_crash_shadow_mode)
print(\"new_crash.min_events:\", s.hourly_alert_new_crash_min_events)
print(\"new_crash.min_sessions:\", s.hourly_alert_new_crash_min_sessions)
'"
```

Expected:
```
new_version.shadow_mode: True
new_version.min_events: 30
new_version.user_rate_pct: 0.005
new_crash.shadow_mode: True
new_crash.min_events: 150
new_crash.min_sessions: 300
```

- [ ] **Step 6: 等 24h 后看影子告警统计**

```bash
sshpass -p 123456 ssh mac@10.0.52.102 "docker compose -f ~/jarvis/docker-compose.yml exec -T backend python -c '
import asyncio, json
from app.crashguard.db.session import get_session
from app.crashguard.db.models import CrashHourlyAlert
from sqlalchemy import select
from datetime import datetime, timedelta

async def main():
    cutoff = datetime.utcnow() - timedelta(days=1)
    async with get_session() as s:
        rows = (await s.execute(
            select(CrashHourlyAlert).where(CrashHourlyAlert.created_at >= cutoff)
        )).scalars().all()
        for r in rows:
            p = json.loads(r.alert_payload)
            print(\"hour=%s new=%d surge=%d new_version=%d new_crash=%d\" % (
                r.hour_utc.isoformat(),
                len(p.get(\"new\", [])), len(p.get(\"surge\", [])),
                len(p.get(\"new_version\", [])), len(p.get(\"new_crash\", [])),
            ))

asyncio.run(main())
'"
```

人工判断：是否合理（≤ 1.2 次/天）？阈值是否需要调？

- [ ] **Step 7: 真发卡片（24h 后人工确认）**

如果影子模式 24h 体感良好，改 `config.yaml`：

```yaml
new_version:
  shadow_mode: false   # ← 改 false
new_crash:
  shadow_mode: false
```

部署：

```bash
sshpass -p 123456 ssh mac@10.0.52.102 "cd ~/jarvis && git pull && docker compose restart backend"
```

- [ ] **Step 8: 1 周后体感复盘**

观察告警量 vs 真问题命中率，决定是否进 Phase 2 调参（spec Section 8）。

---

## Self-Review 已执行

- ✅ **Spec coverage**：每个 Section 都有对应 Task（架构→T4/T5/T6 / 版本识别→T1 / 缓存→T2 / 实现→T3-T7 / 测试→各 Task / 影子发布→T8）
- ✅ **Placeholder scan**：无 TBD/TODO；DatadogClient 构造方式留了"按现有 pattern 复用"提示——执行者必须先读 hourly_alerter 现状代码
- ✅ **Type consistency**：`new_version_items` / `new_crash_items` 在所有 Task 里命名一致；`classify_version` 返回值 `"new"/"main"/"legacy"` 全程一致

---

## 风险点 / 执行者注意

1. **DatadogClient 构造**：Task 4 Step 3、Task 5 Step 3 都依赖现有 `_fetch_hourly_events` 的构造方式——务必阅读现状代码，不要发明新 API
2. **24h 累计数据 query**：Task 5 Step 3 的 `_do_fetch` 实现可能要新写一段 Datadog query（24h 窗口）；不要 reuse 3h fetch 函数，因为它内部硬编码了 3h 窗口
3. **测试 helper**：现有 test_hourly_alerter.py 已有 mock pattern——按同款扩展，不要发明新 helper 名字
4. **铁律**：git push / 部署到服务器（Step 3/4/7）须确保用户授权，按 `~/.claude/.../memory/feedback_no_auto_pr.md` 执行
