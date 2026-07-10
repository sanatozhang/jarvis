# Crashguard 4.0 Native 迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 crashguard 崩溃自动化分析 + 自动开 PR 的运营重点迁移到 4.0 native：正式环境 Datadog 过滤、早报/PR汇总代际角标与权重、crashguard 详情页角标、每日仓库同步任务。

**Architecture:** 纯加法性改动为主（新字段、新展示逻辑、新 cron job），不改变现有告警/PR 触发逻辑；唯一的行为改动是 Datadog service filter 收紧到 `env:production`。所有改动局限在 `backend/app/crashguard/` 子模块内（含其允许的 5 个外部耦合点）+ 对应前端组件，不触碰工单分析模块。

**Tech Stack:** FastAPI + SQLAlchemy (async) + Pydantic Settings（后端）；Next.js 15 + React 19 + TypeScript（前端）；pytest + pytest-asyncio。

## Global Constraints

- 设计文档：`docs/superpowers/specs/2026-07-10-crashguard-4x-migration-design.md`（本计划严格对应其中 A/B/C/D/F 五节；E 节是人工验证清单，非代码任务；G 节明确不动）。
- crashguard 隔离合约（`backend/app/crashguard/CLAUDE.md`）：只能用 5 个允许耦合点，禁止 import `app.models`/`app.workers.analysis_worker`/`app.services.rule_engine`/`app.api.{issues,tasks,feedback}`，禁止 SQL join 到非 `crash_*` 表。本计划所有改动均在此约束内。
- 每个任务改完跑：`cd backend && pytest tests/crashguard/ -v`（本任务相关用例）+ `lint-imports`。
- 前端改动跑：`cd frontend && npm run build`（tsc 类型检查随 build 走）。
- 不删除、不重构本计划范围外的现有代码；`daily_report.py`/`feishu_card.py` 等大文件维持现有结构，只做局部插入式修改。

---

### Task 1: Datadog 正式环境过滤（Section A）

**Files:**
- Modify: `backend/app/crashguard/config.py:142`（`datadog_service_filter` 默认值）
- Modify: `config.yaml:184`（crashguard `datadog` 段 `service_filter`，需先读取确认确切行号/缩进）
- Test: `backend/tests/crashguard/test_datadog_client.py`（如无此文件则新建，验证 `_inject_service` 对新 filter 字符串的行为不变——它是纯字符串拼接，行为本身不需要新逻辑，这里测的是"新 filter 值本身语法上被正确拼接进 query"）

**Interfaces:**
- Consumes：无新接口，纯配置值变更。
- Produces：`CrashguardSettings.datadog_service_filter` 新默认值，供 `DatadogClient._inject_service()`（`datadog_client.py:71-86`）透传使用。

- [ ] **Step 1: 读取 config.yaml 确认 datadog service_filter 当前精确内容和缩进**

Run: `grep -n "service_filter" /Users/sanato/Desktop/code/newplaud/jarvis/config.yaml`

记录返回的确切行号和当前值（应该是
`service_filter: "(service:plaud-flutter OR service:plaud_android OR service:plaud_ios)"`
或类似，缩进层级在 crashguard.datadog 段下）。

- [ ] **Step 2: 修改 config.yaml 里的 service_filter 值 + 加注释**

用 Edit 工具把该行的值改成：

```yaml
    service_filter: "(service:plaud-flutter OR (service:plaud_android AND env:production) OR (service:plaud_ios AND env:production))"
    # ⚠️ env:production 是 Datadog RUM 保留 tag（顶层 tags 数组，不在 attributes.attributes 内层）。
    # 2026-07-10 实测：android/ios 的 env 取值只有 production/development 两种；
    # development 是内部测试 App 的噪声（14天窗口下 android fatal issue 100% 来自 development，
    # 拉长到30天 env:production 才有 15 个）。flutter(3.x) 的 env 目前只出现过 production，
    # 不需要同样限制。
    # 设计取舍：白名单精确匹配 env:production，不用黑名单排除 env:development——
    # 白名单失败即排除（以后出现新 env 取值如 staging/qa 会被自动挡在外面，符合"只关注
    # 正式环境"的初衷）；黑名单失败即放行（命名不按 development 套路来的新环境会被误当成
    # 正式环境漏进来）。不要"优化"成黑名单。
    # 核实方法（Datadog API key 已在 .env 的 CRASHGUARD_DATADOG_API_KEY/APP_KEY）：
    #   curl -X POST "https://api.<site>/api/v2/error-tracking/issues/search" \
    #     -H "DD-API-KEY: $CRASHGUARD_DATADOG_API_KEY" \
    #     -H "DD-APPLICATION-KEY: $CRASHGUARD_DATADOG_APP_KEY" \
    #     -d '{"data":{"attributes":{"query":"service:plaud_android @error.is_crash:true env:production","from":<epoch_ms>,"to":<epoch_ms>,"track":"rum"},"type":"search_request"}}'
    # 详见 docs/superpowers/specs/2026-07-10-crashguard-4x-migration-design.md 「关键实测数据」
```

- [ ] **Step 3: 同步修改 config.py 的默认值（env > yaml > defaults 三层一致）**

Edit `backend/app/crashguard/config.py`，把第 142 行：

```python
    datadog_service_filter: str = "(service:plaud-flutter OR service:plaud_android OR service:plaud_ios)"
```

改成：

```python
    datadog_service_filter: str = "(service:plaud-flutter OR (service:plaud_android AND env:production) OR (service:plaud_ios AND env:production))"
```

同时把第 138-141 行的注释（"✅ 2026-06-30 Datadog 实测确认..."那段）后面追加一行：

```python
    # ✅ 2026-07-10 追加：native 有 env:production/development 两种 tag，development 是
    #    内部测试 App 噪声（详见 config.yaml service_filter 旁的核实数据），只放行 production。
```

- [ ] **Step 4: 写一个验证 filter 语法被正确读取的测试**

Create/extend `backend/tests/crashguard/test_datadog_client.py`:

```python
"""Tests for crashguard.services.datadog_client — service filter injection."""
from __future__ import annotations


def test_inject_service_prepends_env_production_filter():
    from app.crashguard.services.datadog_client import DatadogClient

    client = DatadogClient(
        api_key="x", app_key="y",
        service_filter=(
            "(service:plaud-flutter OR (service:plaud_android AND env:production) "
            "OR (service:plaud_ios AND env:production))"
        ),
    )
    injected = client._inject_service("@error.is_crash:true")
    assert "env:production" in injected
    assert injected.startswith("(service:plaud-flutter")
    assert injected.endswith("@error.is_crash:true")


def test_inject_service_empty_filter_is_debug_escape_hatch():
    from app.crashguard.services.datadog_client import DatadogClient

    client = DatadogClient(api_key="x", app_key="y", service_filter="")
    assert client._inject_service("@type:error") == "@type:error"
```

先跑一次确认 `DatadogClient.__init__` 的 `service_filter` 参数名和 `_inject_service` 方法名与本测试一致（已在设计调研阶段读过 `datadog_client.py:51,64,71-86`，参数名是 `service_filter`）。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && pytest tests/crashguard/test_datadog_client.py -v`
Expected: 2 passed（若 `DatadogClient.__init__` 还需要其它必填参数，按报错补全，不改测试意图）

- [ ] **Step 6: Commit**

```bash
git add config.yaml backend/app/crashguard/config.py backend/tests/crashguard/test_datadog_client.py
git commit -m "$(cat <<'EOF'
feat(crashguard): filter native Datadog data to env:production

Native RUM events carry env:production/development tags; development
is internal test-app noise (14d window: 100% of android fatal issues
were from development, 0 from production). Whitelist env:production
explicitly rather than blacklisting development, so unknown future env
values fail closed instead of leaking through.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: 代际 badge 常量共享化（version_util.py）

**Files:**
- Modify: `backend/app/crashguard/services/version_util.py`（新增 `GEN_BADGE` 常量）
- Modify: `backend/app/crashguard/services/daily_report.py:36-58`（改为从 version_util 导入，删除本地定义）
- Test: `backend/tests/crashguard/test_version_util.py`（新增对 `GEN_BADGE` 的用例）

**Interfaces:**
- Consumes：无。
- Produces：`version_util.GEN_BADGE: dict[str, str]`（`{"native": "🆕4.0", "flutter": "🦋3.x"}`），供 Task 3（C）和 `daily_report.py` 共用。

- [ ] **Step 1: 写失败的测试**

在 `backend/tests/crashguard/test_version_util.py` 末尾追加：

```python
def test_gen_badge_has_native_and_flutter_entries():
    from app.crashguard.services.version_util import GEN_BADGE

    assert GEN_BADGE["native"] == "🆕4.0"
    assert GEN_BADGE["flutter"] == "🦋3.x"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/crashguard/test_version_util.py::test_gen_badge_has_native_and_flutter_entries -v`
Expected: FAIL with `ImportError: cannot import name 'GEN_BADGE'`

- [ ] **Step 3: 在 version_util.py 加常量**

Edit `backend/app/crashguard/services/version_util.py`，在 `classify_generation` 函数定义前（第 51 行之前，紧跟 `_NATIVE_MIN_VERSION` 定义之后）插入：

```python
# 代际 badge（行内标注 4.0 native vs 3.x flutter）——daily_report / pr_pending_review_alert 共用。
GEN_BADGE = {"native": "🆕4.0", "flutter": "🦋3.x"}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/crashguard/test_version_util.py -v`
Expected: all passed

- [ ] **Step 5: 改 daily_report.py 改用共享常量**

Edit `backend/app/crashguard/services/daily_report.py`，把第 36-58 行：

```python
from app.crashguard.services.version_util import classify_generation
from app.db.database import get_session

logger = logging.getLogger("crashguard.daily_report")

# 代际 badge（行内标注 4.0 native vs 3.x flutter）
_GEN_BADGE = {"native": "🆕4.0", "flutter": "🦋3.x"}


def _generation_of(issue: CrashIssue) -> str:
    """issue 代际：'native' / 'flutter' / ''（service 为主，version 兜底）。"""
    return classify_generation(
        getattr(issue, "service", "") or "",
        getattr(issue, "last_seen_version", "") or "",
    )


def _gen_badge_str(issue: Optional[CrashIssue]) -> str:
    """行内代际 badge（前置空格）：' 🆕4.0' / ' 🦋3.x' / ''。issue 为空返回 ''。"""
    if issue is None:
        return ""
    b = _GEN_BADGE.get(_generation_of(issue), "")
    return f" {b}" if b else ""
```

改成：

```python
from app.crashguard.services.version_util import GEN_BADGE, classify_generation
from app.db.database import get_session

logger = logging.getLogger("crashguard.daily_report")


def _generation_of(issue: CrashIssue) -> str:
    """issue 代际：'native' / 'flutter' / ''（service 为主，version 兜底）。"""
    return classify_generation(
        getattr(issue, "service", "") or "",
        getattr(issue, "last_seen_version", "") or "",
    )


def _gen_badge_str(issue: Optional[CrashIssue]) -> str:
    """行内代际 badge（前置空格）：' 🆕4.0' / ' 🦋3.x' / ''。issue 为空返回 ''。"""
    if issue is None:
        return ""
    b = GEN_BADGE.get(_generation_of(issue), "")
    return f" {b}" if b else ""
```

（即：删除本地 `_GEN_BADGE` 定义，import 里加 `GEN_BADGE`，`_gen_badge_str` 里 `_GEN_BADGE.get` 改 `GEN_BADGE.get`。）

- [ ] **Step 6: 跑 daily_report 相关测试确认没有破坏既有行为**

Run: `cd backend && pytest tests/crashguard/test_daily_report.py tests/crashguard/test_daily_report_integration.py -v`
Expected: all passed（沿用原有断言，因为 `_gen_badge_str` 行为完全不变，只是常量来源换了）

- [ ] **Step 7: Commit**

```bash
git add backend/app/crashguard/services/version_util.py backend/app/crashguard/services/daily_report.py backend/tests/crashguard/test_version_util.py
git commit -m "$(cat <<'EOF'
refactor(crashguard): share GEN_BADGE constant via version_util

Moves the 🆕4.0/🦋3.x badge mapping out of daily_report.py into
version_util.py so pr_pending_review_alert.py can reuse it without
duplicating the emoji mapping.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: 早报混合列表 4.0 排序权重（Section B）

**Files:**
- Modify: `backend/app/crashguard/services/daily_report.py:1528-1564`（`fatal_news`/`fatal_surges`/`fatal_drops` 三处排序）
- Test: `backend/tests/crashguard/test_daily_report.py`

**Interfaces:**
- Consumes：`version_util.classify_generation`（已导入）、`id_to_issue: dict[str, CrashIssue]`（函数内既有局部变量，key 是 issue_id）。
- Produces：无新接口，只改排序行为。

- [ ] **Step 1: 读取当前三处排序代码确认上下文未变**

Run: `sed -n '1526,1565p' backend/app/crashguard/services/daily_report.py`

确认与设计调研时读到的一致（`sorted(fatal_news, key=lambda x: -x["events"])[:5]` 等三行）。若行号因中间改动漂移，用 grep 定位：
`grep -n 'sorted(fatal_news\|sorted(fatal_surges\|sorted(fatal_drops' backend/app/crashguard/services/daily_report.py`

- [ ] **Step 2: 改三处排序调用（直接内联 lambda，不新建 helper 函数）**

Edit 第 1531 行（新增/`fatal_news`）：

```python
            for item in sorted(fatal_news, key=lambda x: -x["events"])[:5]:
```

改成：

```python
            for item in sorted(
                fatal_news,
                key=lambda x: (
                    0 if classify_generation(
                        getattr(id_to_issue.get(x["issue_id"]), "service", "") or "",
                        getattr(id_to_issue.get(x["issue_id"]), "last_seen_version", "") or "",
                    ) == "native" else 1,
                    -x["events"],
                ),
            )[:5]:
```

Edit 第 1542 行（突增/`fatal_surges`）：

```python
            for item in sorted(fatal_surges, key=lambda x: -(x["delta"] or 0))[:5]:
```

改成：

```python
            for item in sorted(
                fatal_surges,
                key=lambda x: (
                    0 if classify_generation(
                        getattr(id_to_issue.get(x["issue_id"]), "service", "") or "",
                        getattr(id_to_issue.get(x["issue_id"]), "last_seen_version", "") or "",
                    ) == "native" else 1,
                    -(x["delta"] or 0),
                ),
            )[:5]:
```

Edit 第 1555 行（下降/`fatal_drops`）：

```python
            for item in sorted(fatal_drops, key=lambda x: x["delta"] or 0)[:5]:
```

改成：

```python
            for item in sorted(
                fatal_drops,
                key=lambda x: (
                    0 if classify_generation(
                        getattr(id_to_issue.get(x["issue_id"]), "service", "") or "",
                        getattr(id_to_issue.get(x["issue_id"]), "last_seen_version", "") or "",
                    ) == "native" else 1,
                    x["delta"] or 0,
                ),
            )[:5]:
```

（下降是升序——delta 越负越靠前，不加负号；这与原逻辑一致，只是加了代际首位 key。）

- [ ] **Step 3: 写测试验证同 events 下 native 排前面**

在 `backend/tests/crashguard/test_daily_report.py` 里找到测试 `fatal_news`/`fatal_surges`/`fatal_drops` 排序相关的既有测试（如果没有，新增一个）：

```python
def test_fatal_news_sorts_native_before_flutter_at_same_events(monkeypatch):
    """同 events 数值下，4.0 native 条目应该排在 3.x flutter 前面。"""
    from types import SimpleNamespace

    from app.crashguard.services import daily_report as dr_mod

    id_to_issue = {
        "flutter-issue": SimpleNamespace(service="plaud-flutter", last_seen_version="3.20.0"),
        "native-issue": SimpleNamespace(service="plaud_android", last_seen_version="4.0.100"),
    }
    fatal_news = [
        {"issue_id": "flutter-issue", "events": 500, "platform": "ANDROID", "title": "flutter crash"},
        {"issue_id": "native-issue", "events": 500, "platform": "ANDROID", "title": "native crash"},
    ]
    ordered = sorted(
        fatal_news,
        key=lambda x: (
            0 if dr_mod.classify_generation(
                getattr(id_to_issue.get(x["issue_id"]), "service", "") or "",
                getattr(id_to_issue.get(x["issue_id"]), "last_seen_version", "") or "",
            ) == "native" else 1,
            -x["events"],
        ),
    )
    assert ordered[0]["issue_id"] == "native-issue"
    assert ordered[1]["issue_id"] == "flutter-issue"
```

（这个测试直接验证排序逻辑本身而不是整个 `compose_report` 管线，避免为了测一行排序去 mock 一整套 Datadog/DB 依赖——如果仓库里已有覆盖 `compose_report` 端到端的集成测试并且方便加断言，也可以在 `test_daily_report_integration.py` 里加等价断言，两者选一个跑得通的即可。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/crashguard/test_daily_report.py -v -k native_before_flutter`
Expected: PASS

- [ ] **Step 5: 跑全量 daily_report 测试确认没有回归**

Run: `cd backend && pytest tests/crashguard/test_daily_report.py tests/crashguard/test_daily_report_integration.py -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/crashguard/services/daily_report.py backend/tests/crashguard/test_daily_report.py
git commit -m "$(cat <<'EOF'
feat(crashguard): sort 4.0 native items before 3.x in daily report

Attention lists (新增/突增/下降) now break ties in favor of native
entries at the same urgency tier, without touching alert thresholds
or trigger logic.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: PR 待审核汇总加代际角标 + 置顶（Section C）

**Files:**
- Modify: `backend/app/crashguard/services/pr_pending_review_alert.py`
- Test: `backend/tests/crashguard/test_pr_pending_review_alert.py`

**Interfaces:**
- Consumes：`version_util.GEN_BADGE`、`version_util.classify_generation`（Task 2 产出）；`CrashPullRequest.datadog_issue_id`（已有字段，用于反查 `CrashIssue.service`/`last_seen_version`）。
- Produces：`build_pending_review_card` 的 `prs`/`approved_prs`/`yesterday_*_prs` 各 dict 里新增 `"generation": "native" | "flutter" | ""` 键；卡片渲染时 4.0 置顶。

`CrashPullRequest` 表本身不存 `service`/`version`（只有 `repo`/`datadog_issue_id`），需要反查 `CrashIssue` 才能分类代际——这是本任务与设计文档相比新增的一个实现细节（`CrashPullRequest.repo` 存的是 `repo_router` 的 `logical_name`，不是 `service` tag，不能直接传给 `classify_generation`）。

- [ ] **Step 1: 写失败的测试——`_row_to_dict` 应该带 generation 字段**

在 `backend/tests/crashguard/test_pr_pending_review_alert.py` 里新增：

```python
@pytest.mark.asyncio
async def test_run_pending_review_alert_tags_generation(monkeypatch, patched_session):
    """待审核 PR 列表里每条应该带 generation 字段（反查 CrashIssue.service 分类）。"""
    from app.crashguard.models import CrashIssue, CrashPullRequest
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert

    _make_settings(monkeypatch)
    monkeypatch.setattr(
        "app.crashguard.services.pr_pending_review_alert.feishu_cli.send_interactive_card",
        AsyncMock(return_value=True),
    )

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="native-1", platform="ANDROID", service="plaud_android",
            last_seen_version="4.0.100", title="native crash", stack_fingerprint="fp1",
        ))
        session.add(CrashIssue(
            datadog_issue_id="flutter-1", platform="ANDROID", service="plaud-flutter",
            last_seen_version="3.20.0", title="flutter crash", stack_fingerprint="fp2",
        ))
        session.add(CrashPullRequest(
            analysis_id=1, datadog_issue_id="native-1", repo="plaud-native-android",
            pr_url="https://github.com/x/y/pull/1", pr_number=1, pr_status="draft",
        ))
        session.add(CrashPullRequest(
            analysis_id=2, datadog_issue_id="flutter-1", repo="plaud-android",
            pr_url="https://github.com/x/y/pull/2", pr_number=2, pr_status="draft",
        ))
        await session.commit()

    result = await run_pending_review_alert()
    assert result["sent"] is True
    assert result["pending_count"] == 2
```

（`CrashIssue` 具体必填列名以 `backend/app/crashguard/models.py` 的 `CrashIssue` 类定义为准，若 `platform`/`stack_fingerprint` 等字段名或必填性不同，按模型实际定义调整字段——不要凭空加字段。写这一步前先跑
`grep -n "class CrashIssue" -A 30 backend/app/crashguard/models.py` 核对。）

- [ ] **Step 2: 跑测试确认当前不报 generation 相关错（先确认测试本身能跑通到 assert，为下一步的断言做准备）**

Run: `cd backend && pytest tests/crashguard/test_pr_pending_review_alert.py::test_run_pending_review_alert_tags_generation -v`
Expected: PASS（这一步先不断言 `generation` 字段，只确认测试数据搭建正确）

- [ ] **Step 3: 加断言，改成验证 generation 字段真实存在**

在 `run_pending_review_alert()` 内部，找到 `_row_to_dict` 调用点（`prs = [_row_to_dict(r) for r in rows]` 等 4 处），改造前先加一个新测试直接测 `_row_to_dict` 或者新的 helper。给 `pr_pending_review_alert.py` 加一个 `_generation_lookup` 辅助函数和 issue 反查逻辑（见 Step 4），然后把这个测试改成：

```python
    from app.crashguard.services.pr_pending_review_alert import _collect_yesterday_breakdown
    async with patched_session() as session:
        stmt = __import__("sqlalchemy").select(CrashPullRequest)
        rows = (await session.execute(stmt)).scalars().all()
        gen_map = await _build_generation_lookup(session, [r.datadog_issue_id for r in rows])
    assert gen_map["native-1"] == "native"
    assert gen_map["flutter-1"] == "flutter"
```

（把这段追加在上面 Step 1 写的测试函数末尾，同一个测试函数里既验证 `run_pending_review_alert` 整体跑通，也验证反查结果正确，不用拆两个测试。）

- [ ] **Step 4: 跑测试确认失败（`_build_generation_lookup` 还不存在）**

Run: `cd backend && pytest tests/crashguard/test_pr_pending_review_alert.py::test_run_pending_review_alert_tags_generation -v`
Expected: FAIL with `ImportError: cannot import name '_build_generation_lookup'`

- [ ] **Step 5: 实现 `_build_generation_lookup` + 接入 `_row_to_dict` + 卡片排序**

Edit `backend/app/crashguard/services/pr_pending_review_alert.py`。

在文件顶部 import 区（第 17-24 行）加：

```python
from app.crashguard.services.version_util import GEN_BADGE, classify_generation
```

在 `_age_days` 函数之后（第 63 行之后）加一个新函数：

```python
async def _build_generation_lookup(session, issue_ids: List[str]) -> Dict[str, str]:
    """批量反查 CrashIssue.service/last_seen_version，分类每个 issue_id 的代际。

    CrashPullRequest 本身不存 service/version（只有 repo_router 的 logical_name），
    要判代际必须反查 CrashIssue。空 issue_ids 直接返回空 dict（避免空 IN() 查询）。
    """
    from app.crashguard.models import CrashIssue
    from sqlalchemy import select

    ids = [i for i in set(issue_ids) if i]
    if not ids:
        return {}
    stmt = select(
        CrashIssue.datadog_issue_id, CrashIssue.service, CrashIssue.last_seen_version,
    ).where(CrashIssue.datadog_issue_id.in_(ids))
    rows = (await session.execute(stmt)).all()
    return {
        iid: classify_generation(svc or "", ver or "")
        for iid, svc, ver in rows
    }
```

改 `_row_to_dict`（第 376-396 行），加一个 `generation` 参数：

```python
    def _row_to_dict(r, generation: str = "") -> Dict:
        # 优先用 GitHub 实际 reviewer（pr_sync 回写的 gh_reviewers），它覆盖手动/自动/
        # 兜底加的所有 reviewer；为空再退回 app blame 流程写的 reviewer_emails。
        revs = []
        try:
            revs = json.loads(getattr(r, "gh_reviewers", None) or "[]")
        except (json.JSONDecodeError, TypeError):
            revs = []
        if not revs:
            try:
                revs = json.loads(r.reviewer_emails or "[]")
            except (json.JSONDecodeError, TypeError):
                revs = []
        return {
            "pr_url": r.pr_url or "",
            "pr_number": r.pr_number,
            "repo": r.repo or "unknown",
            "pr_status": r.pr_status or "",
            "reviewer_emails": revs,
            "age_days": _age_days(r.created_at) if r.created_at else 0,
            "generation": generation,
        }
```

在 `run_pending_review_alert()` 里，`async with get_session() as session:` 块内（第 341-361 行）拿到 `rows`/`approved_rows`/`breakdown` 之后、`session` 还没关闭前，加一次批量反查：

```python
        # 反查代际（4.0 native / 3.x flutter），供卡片角标 + 排序用
        all_issue_ids = (
            [r.datadog_issue_id for r in rows]
            + [r.datadog_issue_id for r in approved_rows]
            + [r.datadog_issue_id for r in breakdown["merged"]]
            + [r.datadog_issue_id for r in breakdown["closed"]]
            + [r.datadog_issue_id for r in breakdown["created"]]
        )
        gen_map = await _build_generation_lookup(session, all_issue_ids)
```

（这段加在原有 `stats = {...}` 赋值之后、`async with` 块结束之前。）

把 `session` 块之后的 `_row_to_dict` 调用（第 398-402 行）都传入 generation：

```python
    prs = [_row_to_dict(r, gen_map.get(r.datadog_issue_id, "")) for r in rows]
    approved_prs = [_row_to_dict(r, gen_map.get(r.datadog_issue_id, "")) for r in approved_rows]
    yesterday_merged_prs = [_row_to_dict(r, gen_map.get(r.datadog_issue_id, "")) for r in breakdown["merged"]]
    yesterday_closed_prs = [_row_to_dict(r, gen_map.get(r.datadog_issue_id, "")) for r in breakdown["closed"]]
    yesterday_created_prs = [_row_to_dict(r, gen_map.get(r.datadog_issue_id, "")) for r in breakdown["created"]]
```

- [ ] **Step 6: 跑测试确认 Step 3 的断言通过**

Run: `cd backend && pytest tests/crashguard/test_pr_pending_review_alert.py::test_run_pending_review_alert_tags_generation -v`
Expected: PASS

- [ ] **Step 7: 卡片渲染加角标 + 置顶排序**

Edit `build_pending_review_card` 里的 `_render_pr_section` 内部函数（第 140-173 行）。改 repo 分组内的排序（第 164 行）：

```python
            repo_prs = sorted(by_repo_local[r], key=lambda x: -x.get("age_days", 0))
```

改成：

```python
            repo_prs = sorted(
                by_repo_local[r],
                key=lambda x: (
                    0 if x.get("generation") == "native" else 1,
                    -x.get("age_days", 0),
                ),
            )
```

改 PR 行文案渲染（第 166-169 行），加角标：

```python
            for p in repo_prs:
                lines.append(
                    f"{emoji} [#{p.get('pr_number')}]({p.get('pr_url')}) · {suffix_fn(p)}"
                )
```

改成：

```python
            for p in repo_prs:
                gb = GEN_BADGE.get(p.get("generation", ""), "")
                gb_str = f" {gb}" if gb else ""
                lines.append(
                    f"{emoji} [#{p.get('pr_number')}]({p.get('pr_url')}){gb_str} · {suffix_fn(p)}"
                )
```

同样改「当前积压」清单渲染（第 220-234 行）的排序（第 221 行）：

```python
        repo_prs = sorted(by_repo[repo], key=lambda x: -x.get("age_days", 0))
```

改成：

```python
        repo_prs = sorted(
            by_repo[repo],
            key=lambda x: (
                0 if x.get("generation") == "native" else 1,
                -x.get("age_days", 0),
            ),
        )
```

以及行文案（第 230-234 行）加角标：

```python
            status_emoji = "📝" if p.get("pr_status") == "draft" else "🔵"
            gb = GEN_BADGE.get(p.get("generation", ""), "")
            gb_str = f" {gb}" if gb else ""
            lines.append(
                f"{status_emoji} [#{p.get('pr_number')}]({p.get('pr_url')}){gb_str} "
                f"· {age_str} · reviewer: {rev_short}"
            )
```

- [ ] **Step 8: 写测试验证卡片渲染里 native 排前面 + 角标出现**

在 `test_pr_pending_review_alert.py` 里新增：

```python
def test_build_pending_review_card_sorts_native_first_and_shows_badge():
    from app.crashguard.services.pr_pending_review_alert import build_pending_review_card

    prs = [
        {"pr_url": "u1", "pr_number": 1, "repo": "same-repo", "pr_status": "draft",
         "reviewer_emails": [], "age_days": 1, "generation": "flutter"},
        {"pr_url": "u2", "pr_number": 2, "repo": "same-repo", "pr_status": "draft",
         "reviewer_emails": [], "age_days": 0, "generation": "native"},
    ]
    card = build_pending_review_card(prs, stats={})
    # 找到「当前积压」清单区块，确认 native (#2) 的行在 flutter (#1) 之前
    all_text = "\n".join(
        el.get("text", {}).get("content", "")
        for el in card["elements"] if el.get("tag") == "div"
    )
    idx_native = all_text.find("#2")
    idx_flutter = all_text.find("#1")
    assert idx_native != -1 and idx_flutter != -1
    assert idx_native < idx_flutter
    assert "🆕4.0" in all_text
```

- [ ] **Step 9: 跑全部相关测试确认通过**

Run: `cd backend && pytest tests/crashguard/test_pr_pending_review_alert.py -v`
Expected: all passed

- [ ] **Step 10: Commit**

```bash
git add backend/app/crashguard/services/pr_pending_review_alert.py backend/tests/crashguard/test_pr_pending_review_alert.py
git commit -m "$(cat <<'EOF'
feat(crashguard): tag and sort 4.0 native PRs in 10am pending review card

CrashPullRequest doesn't store service/version, so generation is
resolved by looking up the linked CrashIssue.service. Native PRs get
a 🆕4.0 badge and sort ahead of 3.x within each repo group.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: crashguard issue API 加 generation 字段（Section D 后端）

**Files:**
- Modify: `backend/app/crashguard/api/crash.py:1186-1215`（issue 详情序列化返回体）
- Modify: `frontend/src/lib/api.ts:1142-1166`（`CrashIssueDetail` 类型加字段）
- Test: `backend/tests/crashguard/test_crash_api.py`（若无此文件名，用
  `grep -rl "def read_issue\|/issues/{issue_id}" backend/tests/crashguard/` 找到实际覆盖该端点的测试文件）

**Interfaces:**
- Consumes：`version_util.classify_generation(service, version)`。
- Produces：GET `/api/crash/issues/{issue_id}` 响应体新增 `"generation": "native" | "flutter" | ""` 字段，供前端 Task 6 使用。

- [ ] **Step 1: 找到覆盖该端点的现有测试文件**

Run: `grep -rl "issues/{" backend/tests/crashguard/ backend/app/crashguard/api/crash.py 2>/dev/null | grep test`

若找不到专门测试文件，就在 `backend/tests/crashguard/test_crash_api.py` 新建（先确认这个文件不存在再新建，避免覆盖已有内容）。

- [ ] **Step 2: 写失败的测试**

（若新建文件，先看一个现有 API 测试文件的 fixture 风格，比如 `test_pr_pending_review_alert.py` 的 `patched_session`，复用同样的 `db_engine` fixture 模式。）

```python
"""Tests for crashguard.api.crash — issue detail generation field."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)
    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


@pytest.mark.asyncio
async def test_issue_detail_includes_generation_field(patched_session):
    from app.crashguard.api.crash import get_issue_detail
    from app.crashguard.models import CrashIssue

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="native-2", platform="ANDROID", service="plaud_android",
            last_seen_version="4.0.100", title="native crash", stack_fingerprint="fpx",
        ))
        await session.commit()

    detail = await get_issue_detail("native-2")
    assert detail["generation"] == "native"
```

（函数名 `get_issue_detail` 是本任务前置调研阶段读代码时看到的返回体所在函数——写这一步前先跑
`grep -n "def.*issue_detail\|@router.get(\"/issues/{" backend/app/crashguard/api/crash.py`
确认真实函数名和签名，若不同按实际改。）

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && pytest tests/crashguard/test_crash_api.py::test_issue_detail_includes_generation_field -v`
Expected: FAIL with `KeyError: 'generation'`

- [ ] **Step 4: 实现**

Edit `backend/app/crashguard/api/crash.py`。在文件顶部 import 区加：

```python
from app.crashguard.services.version_util import classify_generation
```

在返回体构造处（第 1186-1215 行），第 1193 行 `"service": issue.service or "",` 之后加一行：

```python
        "service": issue.service or "",
        "generation": classify_generation(issue.service or "", issue.last_seen_version or ""),
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && pytest tests/crashguard/test_crash_api.py -v`
Expected: PASS

- [ ] **Step 6: 前端类型加字段**

Edit `frontend/src/lib/api.ts`，在 `CrashIssueDetail` 接口（第 1142-1166 行）里 `service: string;` 之后加：

```typescript
  service: string;
  generation?: "native" | "flutter" | "";
```

- [ ] **Step 7: 跑后端全量 crashguard 测试确认无回归**

Run: `cd backend && pytest tests/crashguard/ -v`
Expected: all passed

- [ ] **Step 8: 跑前端类型检查确认无报错**

Run: `cd frontend && npm run build`
Expected: build succeeds, no TS errors referencing `generation`

- [ ] **Step 9: Commit**

```bash
git add backend/app/crashguard/api/crash.py frontend/src/lib/api.ts backend/tests/crashguard/test_crash_api.py
git commit -m "$(cat <<'EOF'
feat(crashguard): expose generation field on issue detail API

Reuses version_util.classify_generation so the frontend can render a
native/flutter badge without duplicating the classification logic in
TypeScript.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: crashguard 详情页前端角标（Section D 前端）

**Files:**
- Modify: `frontend/src/app/crashguard/page.tsx`（`DetailDrawer` 组件，约第 2156-2158 行）
- Test: 手动验证（前端无既有单测基础设施覆盖到组件渲染层，跟随 `frontend/CLAUDE.md` 约定用 `npm run build` + 手动跑 `npm run dev` 检查）

**Interfaces:**
- Consumes：`CrashIssueDetail.generation`（Task 5 产出）。
- Produces：无新接口，纯 UI 渲染。

- [ ] **Step 1: 在 DetailDrawer 组件文件顶部（`page.tsx` import 区）确认没有可直接复用的 badge 组件**

Run: `grep -n "^import\|^function.*Badge" frontend/src/app/crashguard/page.tsx | head -20`

确认 crashguard page.tsx 里目前没有独立的 generation badge 组件（若调研阶段结论有变，按实际情况调整，不要重复造轮子）。

- [ ] **Step 2: 加一个轻量 badge 组件（视觉对齐 AnalysisResultView.tsx 的 CodeRoutingBadge，但不跨目录复用）**

在 `page.tsx` 里找到 `DetailDrawer` 组件定义之前的位置（其它辅助组件如 `KV`/`Section`/`PieChart` 所在区域），加：

```tsx
function GenerationBadge({ generation }: { generation?: string }) {
  if (generation === "native") {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-lg px-2 py-0.5 text-[10px] font-medium"
        style={{ background: "rgba(34,197,94,0.10)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}
      >
        🆕 4.0
      </span>
    );
  }
  if (generation === "flutter") {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-lg px-2 py-0.5 text-[10px] font-medium"
        style={{ background: "rgba(96,165,250,0.10)", color: "#2563EB", border: "1px solid rgba(96,165,250,0.25)" }}
      >
        🦋 3.x
      </span>
    );
  }
  return null;
}
```

（配色直接复用 `AnalysisResultView.tsx` 里 `CodeRoutingBadge` 的 native=绿/flutter=蓝 配色值，保持两处视觉一致，但组件本身不跨目录 import——crashguard 前端目录独立。）

- [ ] **Step 3: 在「基础信息」区渲染 badge**

Edit 第 2156-2158 行：

```tsx
                <KV k={t("平台")} v={platformLabel(detail.platform)} />
                <KV k={t("服务")} v={detail.service || "—"} />
                <KV k={t("版本范围")} v={versionRange(detail.first_seen_version, detail.last_seen_version)} />
```

改成：

```tsx
                <KV k={t("平台")} v={platformLabel(detail.platform)} />
                <KV
                  k={t("服务")}
                  v={
                    <span className="inline-flex items-center gap-1.5">
                      {detail.service || "—"}
                      <GenerationBadge generation={detail.generation} />
                    </span>
                  }
                />
                <KV k={t("版本范围")} v={versionRange(detail.first_seen_version, detail.last_seen_version)} />
```

（若 `KV` 组件的 `v` prop 类型定义为 `string`（不接受 `ReactNode`），需要先看 `KV` 组件定义调整类型为 `ReactNode`——运行
`grep -n "function KV" -A 10 frontend/src/app/crashguard/page.tsx`
确认，若类型不匹配就把 `v` 参数类型从 `string` 放宽为 `React.ReactNode`，这是本任务范围内的必要联动修改，不算超出范围的重构。）

- [ ] **Step 4: 跑前端类型检查 + build**

Run: `cd frontend && npm run build`
Expected: build succeeds

- [ ] **Step 5: 手动验证**

Run: `cd frontend && npm run dev`，打开 `http://localhost:3000/crashguard`，点开任意一个 issue 详情，确认「服务」字段旁出现 🆕4.0 或 🦋3.x 角标（取决于该 issue 的 `service` 值）。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/app/crashguard/page.tsx
git commit -m "$(cat <<'EOF'
feat(crashguard): show generation badge on issue detail drawer

Visually matches AnalysisResultView's CodeRoutingBadge color scheme
(native=green, flutter=blue) without cross-directory component reuse,
keeping crashguard's frontend independent per its isolation contract.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: 每日仓库同步任务（Section F）

**Files:**
- Create: `backend/app/crashguard/services/repo_sync.py`
- Modify: `backend/app/crashguard/config.py`（新增 `repo_sync_enabled`/`repo_sync_cron` 设置 + yaml override 解析）
- Modify: `backend/app/crashguard/workers/scheduler.py`（注册新 cron job）
- Modify: `backend/app/crashguard/api/crash.py`（新增手动触发端点）
- Test: `backend/tests/crashguard/test_repo_sync.py`

**Interfaces:**
- Consumes：`app.config.get_repo_routing()`（返回 `dict[str, {"bands": [dict, ...]}]`）；`app.crashguard.services.pr_drafter._acquire_repo_lock/_run_git/_resolve_remote_name/_default_base_ref`（已有函数，直接 import 复用）。
- Produces：`repo_sync.run_repo_sync() -> dict`（`{"total": int, "ok": int, "failed": int, "results": [...]}`），供 scheduler 和手动触发端点调用。

- [ ] **Step 1: 写失败的测试——`_collect_repo_paths` 只覆盖 android/ios 两个 platform，去重**

Create `backend/tests/crashguard/test_repo_sync.py`:

```python
"""Tests for crashguard.services.repo_sync."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def test_collect_repo_paths_covers_android_ios_bands_only(monkeypatch):
    from app.crashguard.services import repo_sync

    fake_routing = {
        "android": {"bands": [
            {"min_version": "0", "family": "flutter", "wrapper": "/tmp/plaud_ai", "sub": "plaud-android"},
            {"min_version": "4.0.0", "family": "native", "wrapper": "/tmp/plaud-native-app", "sub": "plaud-native-android"},
        ]},
        "ios": {"bands": [
            {"min_version": "0", "family": "flutter", "wrapper": "/tmp/plaud_ai", "sub": "plaud-ios"},
            {"min_version": "4.0.0", "family": "native", "wrapper": "/tmp/plaud-native-app", "sub": "plaud-native-ios"},
        ]},
        "web": {"bands": [
            {"min_version": "0", "family": "web", "wrapper": "/tmp/plaud-web", "sub": ""},
        ]},
    }
    monkeypatch.setattr(repo_sync, "get_repo_routing", lambda: fake_routing)

    paths = repo_sync._collect_repo_paths()

    assert "/tmp/plaud_ai/plaud-android" in paths
    assert "/tmp/plaud-native-app/plaud-native-android" in paths
    assert "/tmp/plaud_ai/plaud-ios" in paths
    assert "/tmp/plaud-native-app/plaud-native-ios" in paths
    # web 不在 crashguard 监控范围内，不应该出现
    assert not any("plaud-web" in p for p in paths)
    assert len(paths) == len(set(paths))  # 去重
```

- [ ] **Step 2: 跑测试确认失败（模块还不存在）**

Run: `cd backend && pytest tests/crashguard/test_repo_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.crashguard.services.repo_sync'`

- [ ] **Step 3: 实现 `repo_sync.py`**

Create `backend/app/crashguard/services/repo_sync.py`:

```python
"""每日仓库同步任务 —— 保证 crashguard 自动 PR 的本地 checkout 不会变旧。

只覆盖 crashguard 自己实际监控崩溃、会去开 PR 的 platform（android/ios），不管
是 flutter 世代还是 native 世代的 band 都同步。不覆盖 web/desktop/mcp——那些是
工单处理未来要支持的范围，不是 crashguard 崩溃分析/自动 PR 的范围，见
docs/superpowers/specs/2026-07-10-crashguard-4x-migration-design.md Section F。

正常路径：fetch + checkout 默认分支 + ff-only pull。
失败路径（正常路径任一步报错）：强制 fetch + checkout -f + reset --hard。

复用 pr_drafter.py 已有的 per-repo 锁 + git helper，防止和进行中的 auto-PR
git 操作打架。
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List

from app.config import get_repo_routing
from app.crashguard.services.pr_drafter import (
    _acquire_repo_lock,
    _default_base_ref,
    _resolve_remote_name,
    _run_git,
)

logger = logging.getLogger("crashguard.repo_sync")

# crashguard 自己实际监控崩溃、会去开 PR 的 platform —— 不含 web/desktop/mcp
_MONITORED_PLATFORMS = ("android", "ios")


def _collect_repo_paths() -> List[str]:
    """枚举 crashguard 监控平台下所有 band 的 sub_repo_path，去重（保持首次出现顺序）。"""
    routing = get_repo_routing()
    paths: List[str] = []
    seen = set()
    for platform in _MONITORED_PLATFORMS:
        cfg = routing.get(platform) or {}
        for band in cfg.get("bands") or []:
            wrapper = os.path.expanduser(band.get("wrapper", "") or "")
            if not wrapper:
                continue
            sub = (band.get("sub", "") or "").strip()
            path = os.path.join(wrapper, sub) if sub else wrapper
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
    return paths


def _branch_from_base_ref(base_ref: str, remote: str) -> str:
    """'origin/main' + remote='origin' -> 'main'；解析失败兜底 'main'。"""
    prefix = f"{remote}/"
    if base_ref.startswith(prefix):
        return base_ref[len(prefix):]
    return "main"


async def _sync_one_repo(repo_path: str) -> Dict:
    """同步单仓：正常路径 fetch+checkout+ff-only pull；失败则强制 fetch+reset --hard。"""
    if not os.path.isdir(repo_path):
        return {"repo_path": repo_path, "ok": False, "forced": False, "error": "path not found"}

    lock = await _acquire_repo_lock(repo_path)
    async with lock:
        remote = _resolve_remote_name(repo_path)
        base_ref = _default_base_ref(repo_path)
        branch = _branch_from_base_ref(base_ref, remote)

        rc, _, err = _run_git(["git", "fetch", remote], repo_path, timeout=120)
        if rc == 0:
            rc2, _, err2 = _run_git(["git", "checkout", branch], repo_path, timeout=30)
            if rc2 == 0:
                rc3, _, err3 = _run_git(
                    ["git", "pull", "--ff-only", remote, branch], repo_path, timeout=60,
                )
                if rc3 == 0:
                    return {"repo_path": repo_path, "ok": True, "forced": False, "error": ""}
                err = err3
            else:
                err = err2
        logger.warning("repo_sync: normal path failed for %s (%s), forcing sync", repo_path, err)

        rc, _, ferr = _run_git(["git", "fetch", remote], repo_path, timeout=120)
        if rc != 0:
            return {"repo_path": repo_path, "ok": False, "forced": True, "error": f"forced fetch failed: {ferr}"}
        rc, _, ferr = _run_git(["git", "checkout", "-f", branch], repo_path, timeout=30)
        if rc != 0:
            return {"repo_path": repo_path, "ok": False, "forced": True, "error": f"forced checkout failed: {ferr}"}
        rc, _, ferr = _run_git(
            ["git", "reset", "--hard", f"{remote}/{branch}"], repo_path, timeout=30,
        )
        if rc != 0:
            return {"repo_path": repo_path, "ok": False, "forced": True, "error": f"forced reset failed: {ferr}"}
        return {"repo_path": repo_path, "ok": True, "forced": True, "error": ""}


async def run_repo_sync() -> Dict:
    """主入口：同步所有 crashguard 监控平台的仓库 checkout。"""
    paths = _collect_repo_paths()
    results = [await _sync_one_repo(p) for p in paths]
    for r in results:
        if r["ok"]:
            logger.info("repo_sync: %s ok (forced=%s)", r["repo_path"], r["forced"])
        else:
            logger.warning("repo_sync: %s FAILED: %s", r["repo_path"], r["error"])
    ok_count = sum(1 for r in results if r["ok"])
    return {
        "total": len(results),
        "ok": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/crashguard/test_repo_sync.py -v`
Expected: PASS

- [ ] **Step 5: 加 `_sync_one_repo` 的 mock 测试（正常路径 + 强制路径）**

在 `test_repo_sync.py` 追加：

```python
@pytest.mark.asyncio
async def test_sync_one_repo_normal_path(monkeypatch, tmp_path):
    from app.crashguard.services import repo_sync

    repo_path = str(tmp_path)
    monkeypatch.setattr(repo_sync, "_resolve_remote_name", lambda p: "origin")
    monkeypatch.setattr(repo_sync, "_default_base_ref", lambda p: "origin/main")

    def fake_run_git(cmd, cwd, timeout=60):
        return 0, "", ""

    monkeypatch.setattr(repo_sync, "_run_git", fake_run_git)

    result = await repo_sync._sync_one_repo(repo_path)
    assert result["ok"] is True
    assert result["forced"] is False


@pytest.mark.asyncio
async def test_sync_one_repo_falls_back_to_force_reset(monkeypatch, tmp_path):
    from app.crashguard.services import repo_sync

    repo_path = str(tmp_path)
    monkeypatch.setattr(repo_sync, "_resolve_remote_name", lambda p: "origin")
    monkeypatch.setattr(repo_sync, "_default_base_ref", lambda p: "origin/main")

    call_log = []

    def fake_run_git(cmd, cwd, timeout=60):
        call_log.append(cmd)
        if cmd[:2] == ["git", "pull"]:
            return 1, "", "diverged"
        return 0, "", ""

    monkeypatch.setattr(repo_sync, "_run_git", fake_run_git)

    result = await repo_sync._sync_one_repo(repo_path)
    assert result["ok"] is True
    assert result["forced"] is True
    assert any(cmd[:2] == ["git", "reset"] for cmd in call_log)


@pytest.mark.asyncio
async def test_sync_one_repo_missing_path_returns_error():
    from app.crashguard.services import repo_sync

    result = await repo_sync._sync_one_repo("/definitely/not/a/real/path")
    assert result["ok"] is False
    assert "not found" in result["error"]
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && pytest tests/crashguard/test_repo_sync.py -v`
Expected: all passed

- [ ] **Step 7: 加配置项**

Edit `backend/app/crashguard/config.py`。在 `pipeline_cron: str = "0 */4 * * *"` 之后（第 263 行之后）加：

```python
    # === 每日仓库同步（保证 crashguard auto-PR 的本地 checkout 不变旧）===
    # 上线安全策略：默认 False，先用 POST /api/crash/repo-sync/run-now 在测试机手动验证
    # 行为符合预期，再打开——这是一个会做 git reset --hard、无人值守碰生产仓库的新机制。
    repo_sync_enabled: bool = False
    repo_sync_cron: str = "0 3 * * *"
```

在 `_yaml_overrides()` 函数里，`pipeline_cron` 解析（第 644-645 行）之后加：

```python
    if "repo_sync_enabled" in cfg:
        flat["repo_sync_enabled"] = bool(cfg["repo_sync_enabled"])
    if "repo_sync_cron" in cfg:
        flat["repo_sync_cron"] = str(cfg["repo_sync_cron"])
```

- [ ] **Step 8: 加 scheduler 注册**

Edit `backend/app/crashguard/workers/scheduler.py`。在顶部模块级变量区（第 38 行 `_deep_analyze_auto_last_fired` 之后）加：

```python
_repo_sync_last_fired: str = ""      # 每日仓库同步 tick 进程级幂等
```

在 `_tick_once()` 里 `baseline_backfill` 那个 block（第 360-379 行）之后加一个新 block（照抄同样的模式）：

```python
    # 每日仓库同步（保证 crashguard auto-PR 的本地 checkout 不变旧；默认关，见 config.py 说明）
    global _repo_sync_last_fired
    if getattr(s, "repo_sync_enabled", False):
        rs_cron = getattr(s, "repo_sync_cron", "") or "0 3 * * *"
        if rs_cron and _repo_sync_last_fired != tag and _cron_matches(rs_cron, now):
            _repo_sync_last_fired = tag
            async def _repo_sync_job():
                async with record_heartbeat("repo_sync") as hb:
                    from app.crashguard.services.repo_sync import run_repo_sync
                    res = await run_repo_sync()
                    hb.set_summary(res)
                    hb.set_status_from_result(res)
                    logger.info(
                        "crashguard repo_sync fired: total=%s ok=%s failed=%s",
                        res.get("total"), res.get("ok"), res.get("failed"),
                    )
            _enqueue_job("repo_sync", _repo_sync_job)
```

- [ ] **Step 9: 加手动触发端点**

Edit `backend/app/crashguard/api/crash.py`。在 `@router.post("/warmup")` 端点定义之后（第 93 行附近的函数结束后）加：

```python
@router.post("/repo-sync/run-now")
async def trigger_repo_sync_now() -> Dict[str, Any]:
    """立即触发一次仓库同步（不受 repo_sync_enabled 开关限制——手动触发本来就是显式意图）。

    用于上线前在测试机验证行为，不用等到默认的凌晨 3 点。
    """
    from app.crashguard.services.repo_sync import run_repo_sync

    try:
        return await run_repo_sync()
    except Exception as e:
        logger.exception("manual repo_sync failed")
        raise HTTPException(status_code=500, detail=f"repo_sync failed: {e}")
```

- [ ] **Step 10: 跑全量 crashguard 测试 + lint-imports 确认无回归**

Run: `cd backend && pytest tests/crashguard/ -v && lint-imports`
Expected: all passed, lint-imports 无违规（`repo_sync.py` 只 import 了 `app.config.get_repo_routing` 和同目录的 `pr_drafter`，两者都已经是既有合法耦合，不新增违规）

- [ ] **Step 11: Commit**

```bash
git add backend/app/crashguard/services/repo_sync.py backend/app/crashguard/config.py backend/app/crashguard/workers/scheduler.py backend/app/crashguard/api/crash.py backend/tests/crashguard/test_repo_sync.py
git commit -m "$(cat <<'EOF'
feat(crashguard): add nightly repo-sync job for auto-PR checkouts

New job #8 (default 03:00, disabled by default) keeps the android/ios
repo checkouts crashguard's auto-PR flow depends on from going stale:
fetch+ff-only-pull normally, fetch+checkout-f+reset--hard as a
fallback when that fails. Scoped to the platforms crashguard actually
monitors (android/ios across all repo_routing bands), not web/desktop/
mcp which are for ticket-processing support, not crash auto-PR.

Shares pr_drafter's per-repo lock so it can't race an in-flight PR
git operation. repo_sync_enabled defaults false — verify via the new
POST /api/crash/repo-sync/run-now on the test server before enabling
on prod.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Notes（写计划时已核对）

- **Spec coverage**：A→Task1，共享常量前置重构→Task2，B→Task3，C→Task4，D→Task5+Task6，F→Task7。E 是人工验证清单（非代码，不建任务）；G 明确不动（未建任务）。
- **B 部分已订正**：设计文档原 B.2（面板展开逻辑改动）经代码核实是解决一个不存在的问题（`EXPANDED_KEYWORDS` 只匹配三个固定大段标题，平台明细段落标题从不含这些关键字，本来就默认折叠），已从设计和本计划中移除，只保留排序权重（Task 3）。
- **类型一致性**：`GEN_BADGE`（Task 2 产出）在 Task 4 里被 import 使用，key 一致（`"native"`/`"flutter"`）；`generation` 字段（Task 5 产出）在 Task 6 里作为 `CrashIssueDetail.generation` 使用，值域一致（`"native" | "flutter" | ""`）。
- **CrashPullRequest 反查代际**：设计文档未提及 `CrashPullRequest` 本身没有 service/version 字段，Task 4 里补充了通过 `datadog_issue_id` 反查 `CrashIssue` 的实现细节（`_build_generation_lookup`），这是写计划阶段核对代码后新增的必要实现细节，不是对设计的偏离。
