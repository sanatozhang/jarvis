# Datadog 原生 Monitor 能力边界探索 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用一个 monitors-as-code 代码层 + `datadog-monitor-builder` skill，真正在 Datadog 建出 3 个代表性 Monitor（threshold / change / anomaly 各一），并据实测产出一份 Datadog 原生告警能力边界文档。

**Architecture:** `builder.py`（def→payload 纯函数）+ `client.py`（Monitors API HTTP 薄封装）+ `sync.py`/CLI（幂等 create/update，按回写进 def 的 id 去重）。def yaml 是人写的监控意图（含 monitor query 字符串）。skill 指导按 3 种检测类型写 query，复杂 RUM formula 从 Datadog UI 导出。鉴权复用 coreguard 的 `CRASHGUARD_DATADOG_*` key。

**Tech Stack:** Python 3, httpx（同步 Client + MockTransport 测试）, pyyaml, pytest, argparse。Datadog Monitors API v1。

---

## 文件结构

| 文件 | 职责 | 新建/修改 |
|---|---|---|
| `backend/app/coreguard/monitors/__init__.py` | 包标记 | 新建 |
| `backend/app/coreguard/monitors/client.py` | Monitors API HTTP 客户端（create/get/update/list/mute/delete） | 新建 |
| `backend/app/coreguard/monitors/builder.py` | `build_monitor_payload(def) -> dict` 纯函数 | 新建 |
| `backend/app/coreguard/monitors/sync.py` | 读 def yaml → builder → 幂等 create/update + 回写 id | 新建 |
| `backend/app/coreguard/monitors/defs/hang_rate.threshold.yaml` | 样板② 定义 | 新建 |
| `backend/app/coreguard/monitors/defs/crash_free_sessions.change.yaml` | 样板① 定义 | 新建 |
| `backend/app/coreguard/monitors/defs/api_latency_p95.anomaly.yaml` | 样板③ 定义 | 新建 |
| `scripts/datadog_monitor.py` | CLI：sync / list / mute / dry-run | 新建 |
| `backend/tests/coreguard/monitors/test_builder.py` | builder 单测 | 新建 |
| `backend/tests/coreguard/monitors/test_client.py` | client 单测（MockTransport） | 新建 |
| `backend/tests/coreguard/monitors/test_sync.py` | sync 幂等逻辑单测（fake client） | 新建 |
| `Plaud2/.cursor/skills/datadog-monitor-builder/SKILL.md` | skill 定义 | 新建 |
| `docs/datadog-monitor-boundary-map.md` | 能力边界对照（实测填充） | 新建 |

> 注：skill 文件路径在 `Plaud2` 仓库（`/Users/sanato/Desktop/code/newplaud/Plaud2/.cursor/skills/`），其余在 `jarvis` 仓库。两仓库分别提交。

---

## 真实 widget 查询（已从 dashboard 4h8-qff-zra 只读拉取，2026-06-05）

后续 def yaml 的 query 据此推导，避免占位符。

**widget 0 — Crash-free sessions**（RUM 双 query + formula）：
- query1: `data_source:rum`, search `@type:error @error.is_crash:true -@error.category:ANR env:production @application.name:plaud-flutter @device.type:Mobile`, compute `cardinality(@session.id)`
- query2: `data_source:rum`, search `@session.type:user env:production @application.name:plaud-flutter @device.type:Mobile @type:session`, compute `count`
- formula: `100 - ((query1 * 100) / query2)`

**widget 3 — Hang Rate**（metrics 双 query + formula）：
- appHang: `sum:rum.measure.error.hang.duration{application.id:4d37540b-c5d2-453b-8f02-0b65ebab1eca}`
- sessionsTimeSpent: `sum:rum.measure.session.time_spent{application.id:4d37540b-c5d2-453b-8f02-0b65ebab1eca}.as_count()`
- formula: `cutoff_max((cutoff_min(appHang, 250000000) * 3600000000000) / cutoff_min(sessionsTimeSpent, 30000000), 378784384)`
- dashboard red line: `1.2e9 ns/h`

**widget 36 — API延迟P95**（RUM 单 query）：
- a: `data_source:rum`, search `@type:resource @resource.type:native`, compute `pc95(@resource.duration)`

> dashboard 里 `$os_name` / `$version` 是 template var；monitor 全平台监控时删除这些占位（同 `datadog_scalar.py::_resolve_template_vars` 思路）。

---

## Task 1: Monitors API 客户端 `client.py`

**Files:**
- Create: `backend/app/coreguard/monitors/__init__.py`
- Create: `backend/app/coreguard/monitors/client.py`
- Test: `backend/tests/coreguard/monitors/test_client.py`

- [ ] **Step 1: 建包标记文件**

Create `backend/app/coreguard/monitors/__init__.py`（空文件）和 `backend/tests/coreguard/monitors/__init__.py`（空文件）。

- [ ] **Step 2: 写失败测试**

Create `backend/tests/coreguard/monitors/test_client.py`:

```python
import json
import httpx
import pytest
from app.coreguard.monitors.client import DatadogMonitorClient


def _client_with(handler):
    transport = httpx.MockTransport(handler)
    return DatadogMonitorClient(api_key="k", app_key="a", site="datadoghq.com", transport=transport)


def test_create_posts_payload_and_returns_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 123, "name": seen["body"]["name"]})

    c = _client_with(handler)
    result = c.create({"name": "m1", "type": "metric alert", "query": "x > 1"})

    assert result["id"] == 123
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/v1/monitor")
    assert seen["headers"]["dd-api-key"] == "k"
    assert seen["headers"]["dd-application-key"] == "a"
    assert seen["body"]["name"] == "m1"


def test_update_puts_to_id_url():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": 123})

    c = _client_with(handler)
    c.update(123, {"name": "m1"})
    assert seen["method"] == "PUT"
    assert seen["url"].endswith("/api/v1/monitor/123")


def test_mute_posts_to_mute_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": 123})

    c = _client_with(handler)
    c.mute(123)
    assert seen["url"].endswith("/api/v1/monitor/123/mute")


def test_non_2xx_raises_with_body():
    def handler(request):
        return httpx.Response(400, json={"errors": ["bad query"]})

    c = _client_with(handler)
    with pytest.raises(RuntimeError) as e:
        c.create({"name": "m1"})
    assert "bad query" in str(e.value)
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/coreguard/monitors/test_client.py -v`
Expected: FAIL，`ModuleNotFoundError: app.coreguard.monitors.client`

- [ ] **Step 4: 实现 client.py**

Create `backend/app/coreguard/monitors/client.py`:

```python
"""Datadog Monitors API v1 薄封装（同步 httpx，CLI 用）。

鉴权复用 coreguard 的 CRASHGUARD_DATADOG_* key（见 config.py 回落逻辑）。
失败抛 RuntimeError（含响应体），由调用方处理 —— 与 datadog_scalar 的"宽容返回 None"
不同：建监控是写操作，必须显式失败。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from app.coreguard.config import get_coreguard_settings

DEFAULT_TIMEOUT = 30.0


class DatadogMonitorClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        app_key: Optional[str] = None,
        site: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        s = get_coreguard_settings()
        self.api_key = api_key or s.datadog_api_key
        self.app_key = app_key or s.datadog_app_key
        self.site = site or s.datadog_site
        self._base = f"https://api.{self.site}/api/v1/monitor"
        self._client = httpx.Client(timeout=DEFAULT_TIMEOUT, transport=transport)

    def _headers(self) -> Dict[str, str]:
        return {
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, json_body: Optional[dict] = None) -> Any:
        resp = self._client.request(method, url, headers=self._headers(), json=json_body)
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"Datadog Monitors API {method} {url} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", self._base, payload)

    def update(self, monitor_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"{self._base}/{monitor_id}", payload)

    def get(self, monitor_id: int) -> Dict[str, Any]:
        return self._request("GET", f"{self._base}/{monitor_id}")

    def list(self, monitor_tags: Optional[str] = None) -> List[Dict[str, Any]]:
        url = self._base
        if monitor_tags:
            url = f"{self._base}?monitor_tags={monitor_tags}"
        return self._request("GET", url)

    def mute(self, monitor_id: int) -> Dict[str, Any]:
        return self._request("POST", f"{self._base}/{monitor_id}/mute")

    def delete(self, monitor_id: int) -> Dict[str, Any]:
        return self._request("DELETE", f"{self._base}/{monitor_id}")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/coreguard/monitors/test_client.py -v`
Expected: PASS（4 passed）

- [ ] **Step 6: 提交**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis
git add backend/app/coreguard/monitors/__init__.py backend/app/coreguard/monitors/client.py backend/tests/coreguard/monitors/__init__.py backend/tests/coreguard/monitors/test_client.py
git commit -m "feat: Datadog Monitors API 客户端"
```

---

## Task 2: def→payload 构造器 `builder.py`

**职责**：把一份 def dict 组装成 Datadog monitor API payload。query 字符串直接取自 def（由 skill 指导编写）；builder 只负责拼 name/type/query/message/tags/priority/options，并按检测类型补 options 默认值。纯函数，无网络。

**def schema**（字段）：
- `key` str；`name` str；`type` str（`metric alert`/`query alert`/`rum alert`）；`detection` str（`threshold`/`change`/`anomaly`）
- `query` str（完整 monitor query，含比较运算与阈值）
- `priority` int（1=P0…）；`tags` list[str]；`notify` list[str]（如 `@user@x.com`）
- `message` str（可选，正文；builder 自动把 notify 追加到末尾）
- `muted_on_create` bool；`options` dict（可选覆盖）；`id` int|null（幂等键）

**Files:**
- Create: `backend/app/coreguard/monitors/builder.py`
- Test: `backend/tests/coreguard/monitors/test_builder.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/coreguard/monitors/test_builder.py`:

```python
from app.coreguard.monitors.builder import build_monitor_payload


def _base_def():
    return {
        "key": "hang_rate",
        "name": "[coreguard][P0] Hang Rate threshold",
        "type": "metric alert",
        "detection": "threshold",
        "query": "avg(last_15m):<q> > 1200000000",
        "priority": 1,
        "tags": ["source:coreguard", "tier:p0"],
        "notify": ["@sanato.zhang@plaud.ai"],
        "message": "Hang Rate 超红线",
        "muted_on_create": True,
    }


def test_threshold_payload_core_fields():
    p = build_monitor_payload(_base_def())
    assert p["name"] == "[coreguard][P0] Hang Rate threshold"
    assert p["type"] == "metric alert"
    assert p["query"] == "avg(last_15m):<q> > 1200000000"
    assert p["priority"] == 1
    assert set(["source:coreguard", "tier:p0"]).issubset(set(p["tags"]))
    # notify 句柄自动追加进 message
    assert "@sanato.zhang@plaud.ai" in p["message"]
    assert "Hang Rate 超红线" in p["message"]


def test_threshold_critical_parsed_into_options():
    p = build_monitor_payload(_base_def())
    # query 末尾的 "> 1200000000" 被解析进 options.thresholds.critical
    assert p["options"]["thresholds"]["critical"] == 1200000000.0


def test_muted_on_create_sets_silenced():
    p = build_monitor_payload(_base_def())
    assert p["options"]["silenced"] == {"*": None}


def test_not_muted_has_no_silenced():
    d = _base_def()
    d["muted_on_create"] = False
    p = build_monitor_payload(d)
    assert "silenced" not in p["options"]


def test_evaluation_delay_default_900():
    # 复用 coreguard 经验：RUM 入仓 0-10min 延迟，给 15min 缓冲
    p = build_monitor_payload(_base_def())
    assert p["options"]["evaluation_delay"] == 900


def test_anomaly_sets_threshold_windows_and_critical_1():
    d = _base_def()
    d["detection"] = "anomaly"
    d["type"] = "query alert"
    d["query"] = "avg(last_15m):anomalies(avg:foo{*}, 'agile', 2, seasonality='weekly') >= 1"
    p = build_monitor_payload(d)
    assert p["options"]["thresholds"]["critical"] == 1.0
    assert p["options"]["threshold_windows"] == {"trigger_window": "last_30m", "recovery_window": "last_30m"}


def test_options_override_merges():
    d = _base_def()
    d["options"] = {"renotify_interval": 120}
    p = build_monitor_payload(d)
    assert p["options"]["renotify_interval"] == 120
    assert p["options"]["evaluation_delay"] == 900  # 默认仍在
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/coreguard/monitors/test_builder.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现 builder.py**

Create `backend/app/coreguard/monitors/builder.py`:

```python
"""def(dict) -> Datadog monitor API payload。纯函数，无网络，易单测。

query 字符串由调用方（skill 指导）写好；builder 负责组装 + 按检测类型补 options 默认值。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

# RUM 入仓 0-10min 才稳定，给 15min 缓冲（复用 coreguard hourly_watch 经验）
DEFAULT_EVALUATION_DELAY = 900


def _parse_critical_from_query(query: str) -> Optional[float]:
    """从 query 末尾的比较式解析 critical 阈值，如 '... > 1200000000' -> 1200000000.0。"""
    m = re.search(r"(?:>=|<=|>|<|==)\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*$", query.strip())
    return float(m.group(1)) if m else None


def build_monitor_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    detection = d.get("detection", "threshold")
    query = d["query"]

    # message：正文 + notify 句柄
    message = d.get("message", "") or ""
    notify = d.get("notify", []) or []
    if notify:
        message = (message + "\n\n" + " ".join(notify)).strip()

    # options 默认值
    options: Dict[str, Any] = {
        "notify_no_data": False,
        "evaluation_delay": DEFAULT_EVALUATION_DELAY,
        "include_tags": True,
        "thresholds": {},
    }

    if detection == "anomaly":
        # anomaly：critical 固定为 1（异常点计数），需 threshold_windows
        options["thresholds"]["critical"] = 1.0
        options["threshold_windows"] = {"trigger_window": "last_30m", "recovery_window": "last_30m"}
    else:
        crit = _parse_critical_from_query(query)
        if crit is not None:
            options["thresholds"]["critical"] = crit

    # 调用方覆盖
    options.update(d.get("options", {}) or {})

    if d.get("muted_on_create"):
        options["silenced"] = {"*": None}

    return {
        "name": d["name"],
        "type": d["type"],
        "query": query,
        "message": message,
        "tags": list(d.get("tags", []) or []),
        "priority": d.get("priority"),
        "options": options,
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/coreguard/monitors/test_builder.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis
git add backend/app/coreguard/monitors/builder.py backend/tests/coreguard/monitors/test_builder.py
git commit -m "feat: monitor def->payload 构造器"
```

---

## Task 3: 幂等 sync + CLI

**职责**：读 `defs/*.yaml` → builder → 若 def 无 `id` 则 create 并把返回 id 回写 yaml；若有 `id` 则 update。dry-run 只打印 payload 不调网络。

**Files:**
- Create: `backend/app/coreguard/monitors/sync.py`
- Create: `scripts/datadog_monitor.py`
- Test: `backend/tests/coreguard/monitors/test_sync.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/coreguard/monitors/test_sync.py`:

```python
import yaml
from pathlib import Path
from app.coreguard.monitors.sync import sync_def


class FakeClient:
    def __init__(self):
        self.created = []
        self.updated = []

    def create(self, payload):
        self.created.append(payload)
        return {"id": 999}

    def update(self, monitor_id, payload):
        self.updated.append((monitor_id, payload))
        return {"id": monitor_id}


def _write_def(tmp_path: Path, extra: dict) -> Path:
    d = {
        "key": "hang_rate",
        "name": "[coreguard][P0] Hang Rate",
        "type": "metric alert",
        "detection": "threshold",
        "query": "avg(last_15m):avg:foo{*} > 100",
        "priority": 1,
        "tags": ["source:coreguard"],
        "notify": ["@x@y.com"],
        "muted_on_create": True,
    }
    d.update(extra)
    p = tmp_path / "hang_rate.threshold.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    return p


def test_sync_creates_when_no_id_and_writes_id_back(tmp_path):
    p = _write_def(tmp_path, {})  # 无 id
    client = FakeClient()

    sync_def(client, p, dry_run=False)

    assert len(client.created) == 1
    assert len(client.updated) == 0
    # id 已回写
    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert reloaded["id"] == 999


def test_sync_updates_when_id_present(tmp_path):
    p = _write_def(tmp_path, {"id": 555})
    client = FakeClient()

    sync_def(client, p, dry_run=False)

    assert len(client.created) == 0
    assert len(client.updated) == 1
    assert client.updated[0][0] == 555


def test_dry_run_calls_nothing(tmp_path):
    p = _write_def(tmp_path, {})
    client = FakeClient()

    payload = sync_def(client, p, dry_run=True)

    assert client.created == []
    assert client.updated == []
    assert payload["name"] == "[coreguard][P0] Hang Rate"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/coreguard/monitors/test_sync.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现 sync.py**

Create `backend/app/coreguard/monitors/sync.py`:

```python
"""读 def yaml → builder → 幂等 create/update + 回写 id。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import yaml

from app.coreguard.monitors.builder import build_monitor_payload

logger = logging.getLogger("coreguard.monitors.sync")

DEFS_DIR = Path(__file__).resolve().parent / "defs"


def _load(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_id_back(path: Path, monitor_id: int) -> None:
    d = _load(path)
    d["id"] = monitor_id
    path.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")


def sync_def(client, path: Path, dry_run: bool = False) -> Dict[str, Any]:
    """同步单个 def 文件。返回构造出的 payload。"""
    d = _load(path)
    payload = build_monitor_payload(d)
    if dry_run:
        logger.info("[dry-run] %s\n%s", path.name, payload)
        return payload

    existing_id = d.get("id")
    if existing_id:
        client.update(existing_id, payload)
        logger.info("updated monitor %s (%s)", existing_id, path.name)
    else:
        result = client.create(payload)
        new_id = result["id"]
        _write_id_back(path, new_id)
        logger.info("created monitor %s (%s)", new_id, path.name)
    return payload


def sync_all(client, defs_dir: Path = DEFS_DIR, dry_run: bool = False) -> List[str]:
    done = []
    for p in sorted(defs_dir.glob("*.yaml")):
        sync_def(client, p, dry_run=dry_run)
        done.append(p.name)
    return done
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/coreguard/monitors/test_sync.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 实现 CLI**

Create `scripts/datadog_monitor.py`:

```python
#!/usr/bin/env python3
"""Datadog monitor CLI：sync / list / mute / dry-run。

用法（在 jarvis/backend 下，确保 PYTHONPATH 含 app）:
  python ../scripts/datadog_monitor.py sync            # 同步全部 def（create/update）
  python ../scripts/datadog_monitor.py sync --dry-run  # 只打印 payload
  python ../scripts/datadog_monitor.py list            # 列出 source:coreguard 的 monitor
  python ../scripts/datadog_monitor.py mute --id 123
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.coreguard.monitors.client import DatadogMonitorClient  # noqa: E402
from app.coreguard.monitors.sync import sync_all                 # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--dry-run", action="store_true")

    sub.add_parser("list")

    p_mute = sub.add_parser("mute")
    p_mute.add_argument("--id", type=int, required=True)

    args = parser.parse_args()

    if args.cmd == "sync":
        client = None if args.dry_run else DatadogMonitorClient()
        if args.dry_run:
            # dry-run 不需要真 client，但 sync_def 形参要一个；传个占位
            class _Noop:
                pass
            client = _Noop()
        sync_all(client, dry_run=args.dry_run)
    elif args.cmd == "list":
        for m in DatadogMonitorClient().list(monitor_tags="source:coreguard"):
            print(m.get("id"), m.get("name"), "| overall:", m.get("overall_state"))
    elif args.cmd == "mute":
        DatadogMonitorClient().mute(args.id)
        print("muted", args.id)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 手验 dry-run（无网络）**

先建一个临时 def 验证 CLI 串起来（Task 4 会建正式 def）：

Run: `cd /Users/sanato/Desktop/code/newplaud/jarvis/backend && python ../scripts/datadog_monitor.py sync --dry-run`
Expected: 打印各 def 的 payload（此时 defs/ 可能为空则无输出，不报错）。

- [ ] **Step 7: 提交**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis
git add backend/app/coreguard/monitors/sync.py scripts/datadog_monitor.py backend/tests/coreguard/monitors/test_sync.py
git commit -m "feat: monitor 幂等 sync + CLI"
```

---

## Task 4: 三个样板 def yaml

**Files:**
- Create: `backend/app/coreguard/monitors/defs/hang_rate.threshold.yaml`
- Create: `backend/app/coreguard/monitors/defs/api_latency_p95.anomaly.yaml`
- Create: `backend/app/coreguard/monitors/defs/crash_free_sessions.change.yaml`

> query 字符串据上文「真实 widget 查询」推导。这些是**初始版本**，Task 6 实测后可能要调整（Datadog monitor 编辑器 export 校准）。`id` 留空，首次 sync 时回写。

- [ ] **Step 1: 样板② threshold（Hang Rate，metrics formula 最干净）**

Create `backend/app/coreguard/monitors/defs/hang_rate.threshold.yaml`:

```yaml
key: hang_rate
name: "[coreguard][P0] Hang Rate 超红线 (threshold)"
type: "query alert"
detection: threshold
# 来自 widget 3：appHang / sessionsTimeSpent 的 cutoff formula，红线 1.2e9 ns/h。
# monitor formula 语法：用 a/b 两条子查询 + formula；阈值写在末尾比较式。
query: >-
  sum(last_15m):cutoff_max((cutoff_min(sum:rum.measure.error.hang.duration{application.id:4d37540b-c5d2-453b-8f02-0b65ebab1eca}, 250000000) * 3600000000000) / cutoff_min(sum:rum.measure.session.time_spent{application.id:4d37540b-c5d2-453b-8f02-0b65ebab1eca}.as_count(), 30000000), 378784384) > 1200000000
priority: 1
tags: ["source:coreguard", "tier:p0", "metric:hang_rate"]
notify: ["@sanato.zhang@plaud.ai"]
message: "Hang Rate 超过 dashboard 红线 1.2e9 ns/h，存在严重卡顿。"
muted_on_create: true
id: null
```

- [ ] **Step 2: 样板③ anomaly（API 延迟 P95，RUM 单 query）**

Create `backend/app/coreguard/monitors/defs/api_latency_p95.anomaly.yaml`:

```yaml
key: api_latency_p95
name: "[coreguard][P1] API延迟P95 异常 (anomaly)"
type: "query alert"
detection: anomaly
# 来自 widget 36：RUM pc95(@resource.duration)，@type:resource @resource.type:native。
# anomaly 用 weekly 季节性；agile 算法对突变敏感。direction=above 仅在变慢时告警。
query: >-
  avg(last_15m):anomalies(avg:rum.resource.duration{*}, 'agile', 2, direction='above', interval=60, seasonality='weekly') >= 1
priority: 3
tags: ["source:coreguard", "tier:p1", "metric:api_latency_p95"]
notify: ["@sanato.zhang@plaud.ai"]
message: "API 延迟 P95 偏离周季节性基线（异常变慢）。"
muted_on_create: true
id: null
```

> ⚠️ Task 6 边界点：`rum.resource.duration` 是否存在对应 metric、anomaly 是否支持 RUM —— 实测，不行则改用 metric 版或记录为边界。

- [ ] **Step 3: 样板① change（Crash-free，RUM 双 query formula，头号边界）**

Create `backend/app/coreguard/monitors/defs/crash_free_sessions.change.yaml`:

```yaml
key: crash_free_sessions
name: "[coreguard][P0] Crash-free sessions 周同比下跌 (change)"
type: "rum alert"
detection: change
# 来自 widget 0：100 - (crash_sessions * 100 / total_sessions)。
# change 语义：vs 上周同时刻（last_1w）跌 >= 0.5pp 告警 —— 对标 coreguard SHoW。
# ⚠️ 头号边界：多 query RUM formula 能否进 monitor + 能否对 formula 做 change。
# 初版 query 为占位推导，Task 6 必须用 Datadog UI monitor 编辑器校准后 export 回填。
query: >-
  change(avg(last_15m),last_1w):100 - ((rum_crash_sessions * 100) / rum_total_sessions) < -0.5
priority: 1
tags: ["source:coreguard", "tier:p0", "metric:crash_free_sessions"]
notify: ["@sanato.zhang@plaud.ai"]
message: "Crash-free sessions 较上周同时刻下跌超过 0.5pp。"
muted_on_create: true
id: null
```

- [ ] **Step 4: dry-run 验证三个 def 能被 builder 组装**

Run: `cd /Users/sanato/Desktop/code/newplaud/jarvis/backend && python ../scripts/datadog_monitor.py sync --dry-run`
Expected: 打印 3 个 payload，无异常（crash_free 的 critical 解析为 -0.5，hang_rate 为 1200000000，anomaly 为 1.0）。

- [ ] **Step 5: 提交**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis
git add backend/app/coreguard/monitors/defs/
git commit -m "feat: 三个样板 monitor def（threshold/anomaly/change）"
```

---

## Task 5: `datadog-monitor-builder` skill

**Files:**
- Create: `/Users/sanato/Desktop/code/newplaud/Plaud2/.cursor/skills/datadog-monitor-builder/SKILL.md`

- [ ] **Step 1: 写 SKILL.md**

Create the file with this content:

````markdown
---
name: datadog-monitor-builder
description: 通过 monitors-as-code 在 Datadog 创建/更新核心指标告警 Monitor。当用户说「建 Datadog 监控」「加个告警监控」「create datadog monitor」「新增 Datadog Monitor」「给指标加报警」时触发。支持 threshold / change（周同比）/ anomaly 三种检测类型。
---

# datadog-monitor-builder

在 `jarvis` 仓库用 monitors-as-code 创建 Datadog Monitor。代码层在
`backend/app/coreguard/monitors/`，CLI 是 `scripts/datadog_monitor.py`。

## 前置

- Datadog key 复用 `.env` 的 `CRASHGUARD_DATADOG_API_KEY` / `CRASHGUARD_DATADOG_APP_KEY`。
- 不依赖飞书等外部通道；通知用 Datadog 原生（email 句柄写进 def 的 `notify`）。

## 创建一个新 Monitor 的流程

1. **确认三要素**：监控哪个指标（query 来源）、用哪种检测、阈值多少。
2. **选检测类型**：
   - `threshold`：绝对阈值突破（如成功率 < X、延迟 > Y、Hang Rate 超红线）。最简单。
   - `change`：vs 上周同时刻（`change(avg(last_15m),last_1w)`）变化超阈 —— 对标 coreguard SHoW，适合有日内/周内周期的指标。
   - `anomaly`：`anomalies(<q>, 'agile', 2, seasonality='weekly')` 偏离季节性基线。适合趋势异常、无固定阈值的指标。
3. **拿 query**：
   - 指标若在 dashboard `4h8-qff-zra`：用 `dashboard_loader` 或 Datadog UI 看 widget 的 `requests[0].queries + formula`。
   - **单 metric query / 单 RUM query**：可直接套类型模板写 monitor query。
   - **多 query + formula（尤其 RUM）**：在 Datadog UI 的 monitor 编辑器里用 formulas-and-functions 搭好，点 "Export" / "Edit JSON" 拿到 monitor query 字符串，粘进 def。（这是已知边界，见 `docs/datadog-monitor-boundary-map.md`）
   - 删掉 dashboard 的 `$os_name` / `$version` template var（全平台监控）。
4. **写 def yaml**：在 `backend/app/coreguard/monitors/defs/` 建 `<key>.<detection>.yaml`，字段见下。`id` 留 `null`。
5. **dry-run 看 payload**：
   ```
   cd jarvis/backend && python ../scripts/datadog_monitor.py sync --dry-run
   ```
6. **创建（默认静音）**：`muted_on_create: true` 时建出来即静音，不会立刻发告警。
   ```
   cd jarvis/backend && python ../scripts/datadog_monitor.py sync
   ```
   sync 会把返回的 monitor `id` 回写进 def（幂等：再次 sync 走 update）。
7. **UI 验证**：去 Datadog 看该 monitor 评估状态正常（query 不报错、有数据）。
8. **开通知**：确认无误后取消静音 —— 把 def 的 `muted_on_create` 改 `false` 再 sync，或 UI 里 unmute。

## def yaml 字段

| 字段 | 说明 |
|---|---|
| `key` | 指标短名（文件名前缀） |
| `name` | monitor 名，约定 `[coreguard][P0] <指标> (<类型>)` |
| `type` | `metric alert` / `query alert` / `rum alert` |
| `detection` | `threshold` / `change` / `anomaly` |
| `query` | 完整 monitor query（含末尾比较式与阈值） |
| `priority` | 1=P0, 3=P1, 5=P2 |
| `tags` | 必含 `source:coreguard`、`tier:pX` |
| `notify` | Datadog 通知句柄，如 `@sanato.zhang@plaud.ai` |
| `message` | 告警正文（notify 句柄会自动追加） |
| `muted_on_create` | 建议 `true`，验证后再开 |
| `id` | 留 `null`，sync 回写 |

## 类型模板速查

- threshold（metric）：`avg(last_15m):<metric query> > <阈值>`
- change（周同比）：`change(avg(last_15m),last_1w):<query> < <负向阈值>`
- anomaly（周季节性）：`avg(last_15m):anomalies(<query>, 'agile', 2, direction='above', interval=60, seasonality='weekly') >= 1`

## 反模式

- 不要建出来就 @人（先静音验证），避免误报轰炸。
- 不要硬写 dashboard 的 `$os_name`/`$version` 占位符进 monitor query。
- 多 query RUM formula 别凭空猜 query 字符串 —— 从 UI 编辑器 export。
````

- [ ] **Step 2: 提交（Plaud2 仓库）**

```bash
cd /Users/sanato/Desktop/code/newplaud/Plaud2
git add .cursor/skills/datadog-monitor-builder/SKILL.md
git commit -m "docs: 新增 datadog-monitor-builder skill"
```

---

## Task 6: 实测建 Monitor + 填能力边界文档（核心验收）

> 本 Task 真正调 Datadog API（写操作）。每个 monitor 都 `muted_on_create: true`，零误报风险。

**Files:**
- Create: `docs/datadog-monitor-boundary-map.md`
- Modify: `backend/app/coreguard/monitors/defs/*.yaml`（实测校准 query + 回写 id）

- [ ] **Step 1: 通过 skill/CLI 建 Hang Rate（最可能干净的）**

Run: `cd /Users/sanato/Desktop/code/newplaud/jarvis/backend && python ../scripts/datadog_monitor.py sync`
Expected: `created monitor <id> (hang_rate.threshold.yaml)`，def 里 `id` 被回写。
若 API 报 query 语法 400：去 Datadog UI monitor 编辑器粘 formula、export 校准 query，回填 def 重 sync。

- [ ] **Step 2: UI 验证 Hang Rate**

去 Datadog → Monitors，确认该 monitor 存在、muted、评估有数据不报错。记录结论。

- [ ] **Step 3: 建 API 延迟 anomaly，记录 RUM anomaly 边界**

Run: `python ../scripts/datadog_monitor.py sync`（anomaly def 已在 defs/）
观察：`avg:rum.resource.duration{*}` 是否有数据 / anomaly 是否支持。不行则在 UI 用 RUM query 编辑器搭 anomaly，export 校准。把"RUM 指标能否做 anomaly、要不要先转 metric"写进边界文档。

- [ ] **Step 4: 建 Crash-free change，攻头号边界**

在 Datadog UI monitor 编辑器里：选 RUM、用 formulas-and-functions 搭出 `100 - (q1*100/q2)`，再看能否对该 formula 设「change vs 1 week ago」告警。
- 若能：Export JSON，把 monitor query 回填 `crash_free_sessions.change.yaml`，sync。
- 若不能：记录"多 query RUM formula 不支持 change/不支持 formula 告警"这一边界，并记录可行替代（如对 crash-free % 直接设 threshold `< 99.5`，或拆成 metric 再 change）。

- [ ] **Step 5: 写能力边界文档**

Create `docs/datadog-monitor-boundary-map.md`，据 Step 1-4 实测填充。模板：

```markdown
# Datadog 原生 Monitor 能力边界（实测 2026-06-05）

对照 coreguard 自建逻辑，逐条记录 Datadog 原生 Monitor 能/不能做。

| coreguard 自建能力 | Datadog 原生对应 | 实测结论 | 备注 |
|---|---|---|---|
| SHoW 同周同时对比 | `change(avg(last_15m), last_1w)` | ✅/❌ … | last_1w 是否=上周同时刻 |
| Crash-free % 等多 query RUM formula | rum alert + formula | ✅/❌ … | 是否支持 formula 告警 / change |
| `min_users` 样本量地板 | composite monitor 或 query 内嵌 | ✅/❌ … | 是否优雅 |
| N=2 连续 breach 防抖 | `threshold_windows` / "for last N" | ✅/❌ … | |
| P0/P1/P2 分级 | priority + tags | ✅/❌ … | |
| dedup / renotify / 自动 resolve | 内置通知设置 | ✅/❌ … | |
| 趋势异常 | anomaly (weekly) | ✅/❌ … | 调参难度、误报率 |

## 三个样板 monitor 最终状态

| 样板 | monitor id | query 是否需 UI 校准 | 评估正常? |
|---|---|---|---|
| Hang Rate threshold | | | |
| API延迟 anomaly | | | |
| Crash-free change | | | |

## 结论：原生能替代 coreguard 哪些 / 不能替代哪些
（一段话总结）
```

- [ ] **Step 6: 提交（含 def 校准 + 边界文档）**

```bash
cd /Users/sanato/Desktop/code/newplaud/jarvis
git add backend/app/coreguard/monitors/defs/ docs/datadog-monitor-boundary-map.md
git commit -m "docs: 实测三个样板 monitor + Datadog 能力边界文档"
```

- [ ] **Step 7: 验收自检**

确认 spec §9 验收标准全部满足：
- [ ] ≥1 个样板 monitor 由跑 skill/CLI 创建，Datadog UI 可见且评估正常
- [ ] skill 自洽（喂新指标能照流程建）
- [ ] 触发时收到 Datadog email（可手动临时调阈值制造一次触发验证，验证后改回 + 重新静音）
- [ ] builder/client/sync 单测通过：`cd backend && python -m pytest tests/coreguard/monitors/ -v`
- [ ] 边界文档 §表格据实测填完

---

## 备注：已知风险与边界

1. **monitor query 语法**：dashboard 用 v2 formulas-and-functions，monitor 用 legacy/monitor 语法，复杂 RUM formula 可能必须从 UI export（Task 6 Step 4 处理）。def 里的初版 query 是推导值，以实测为准。
2. **anomaly 对 RUM**：可能需要先有对应 metric；Task 6 Step 3 验证。
3. **min_users 样本地板**：原生大概率要 composite monitor，本次只记录边界不实现。
4. **写操作安全**：所有 monitor `muted_on_create: true`，建出来不发告警；开通知是显式手动步骤。
