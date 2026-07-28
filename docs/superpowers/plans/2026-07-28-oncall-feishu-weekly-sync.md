# Oncall Feishu 每周值班同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每周一 08:00 (Asia/Shanghai) 自动把团队维护的飞书多维表格「本周值班（feature 同事兼顾发版）」里当周及未来周次的值班人员，同步进 Jarvis 内部的 oncall 排班快照表，飞书为唯一权威来源，检测到差异直接覆盖写入。

**Architecture:** 新增 `backend/app/services/oncall_feishu_sync.py` 一个文件，承担"拉取飞书表 → 按角色合并邮箱 → 对齐 Jarvis 排班网格 → 差异检测 → 覆盖写入快照表"的完整链路；复用已有的 `feishu_cli.py::_run_cli`（lark-cli subprocess 封装）做只读 API 调用，复用已有的 `oncall_week_assignments` 表和 `db.resolve_week_group`/`db.upsert_week_assignment` 做落库，不新增数据库表/字段。调度沿用 `escalation_reminder.py` 的"自研 asyncio 常驻循环 + sleep 到下次目标时刻"模式，不引入 APScheduler。新增一个管理员可手动触发的 API 端点，方便上线后立即验证一次，不必等到下周一。

**Tech Stack:** FastAPI + SQLAlchemy(async) + lark-cli（已有的 Feishu Bitable 只读封装）+ Python `zoneinfo`。

## Global Constraints

- 只读取飞书表两个角色字段：`值班人员（Feature）`、`值班人员（Fundamentals）`；`发版责任人（Release Manager)` 字段代表发版责任人，不属于 oncall 值班范畴，本功能不读取它（2026-07-28 与用户确认的映射方案）。
- 飞书表里某一周如果两个角色字段都是空（该周没有排班数据），跳过这一周、不动 Jarvis 现有排班——不能把"飞书没数据"误当成"这周没人值班"去覆盖清空。
- 只处理"本周及以后"的飞书行；本次同步不回溯改写历史周次。
- 检测到差异直接覆盖写入 Jarvis 排班快照（不经人工确认这一步——2026-07-28 用户已明确选择"自动直接覆盖写入"）。
- 落地的定时任务本身默认关闭，由环境变量 `ENABLE_ONCALL_FEISHU_SYNC=true` 显式开启，与仓库里 `ENABLE_ONCALL_NOTIFY`（escalation 提醒）的既有约定一致——首次上线前必须让用户显式评审后手动打开开关，不能部署即生效。
- `oncall_week_assignments.week_start_date` 的口径是"非自然周"（`start_date + timedelta(weeks=N)`，不是自然周一）；写入时必须先算出飞书日期落在 Jarvis 网格的第几周（`week_num`），再用 `start + timedelta(weeks=week_num)` 反推出 Jarvis 自己的 key 去写——绝不能直接拿飞书的周一日期当主键，否则当 `start_date` 本身不是周一时，这行快照会被写在一个 `resolve_week_group` 永远查不到的日期上，同步等于静默失效。
- 没有配置过 oncall `start_date`（系统从未初始化过排班）时，整体跳过同步并记录 warning，不做任何写入——没有网格基准，写入的 key 没有意义。

---

### Task 1: Feishu 值班表的 table_id / view_id 配置

**Files:**
- Modify: `backend/app/config.py:107-123`（`FeishuSettings` 类）
- Test: `backend/tests/test_oncall_feishu_sync.py`（新文件，本任务只加开头这一个测试，后续任务继续往这个文件加）

**Interfaces:**
- Produces: `FeishuSettings.oncall_table_id: str`、`FeishuSettings.oncall_view_id: str`（后续任务通过 `get_settings().feishu.oncall_table_id` / `.oncall_view_id` 读取）

- [ ] **Step 1: 写失败的测试**

```python
# backend/tests/test_oncall_feishu_sync.py
"""Weekly oncall sync FROM Feishu「本周值班」表 INTO Jarvis 排班快照。"""


def test_feishu_settings_oncall_table_defaults():
    from app.config import FeishuSettings
    s = FeishuSettings()
    assert s.oncall_table_id == "tblICR3x8k7nwoNK"
    assert s.oncall_view_id == "vewpgzcUrK"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_oncall_feishu_sync.py::test_feishu_settings_oncall_table_defaults -v`
Expected: FAIL，`AttributeError: 'FeishuSettings' object has no attribute 'oncall_table_id'`

- [ ] **Step 3: 加两个配置字段**

在 `backend/app/config.py` 的 `FeishuSettings` 类里，`view_id: str = "vewu36X0Gx"` 那一行之后加两行（env 前缀沿用类里已有的 `env_prefix: "FEISHU_"`，所以可用 `FEISHU_ONCALL_TABLE_ID` / `FEISHU_ONCALL_VIEW_ID` 覆盖）：

```python
class FeishuSettings(BaseSettings):
    app_id: str = ""
    app_secret: str = ""
    app_token: str = "BmjmbSpxxabP2dsuxbtcUTYAn4g"
    table_id: str = "tblWQRIvZq74MhRT"
    view_id: str = "vewu36X0Gx"
    oncall_table_id: str = "tblICR3x8k7nwoNK"
    oncall_view_id: str = "vewpgzcUrK"
    base_url: str = "https://nicebuild.feishu.cn/base/BmjmbSpxxabP2dsuxbtcUTYAn4g"
    # Separate IM app for group chat / messaging (can be same or different app)
    im_app_id: str = ""
    im_app_secret: str = ""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_oncall_feishu_sync.py::test_feishu_settings_oncall_table_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_oncall_feishu_sync.py
git commit -m "feat(oncall): add Feishu oncall-table config (table_id/view_id)"
```

---

### Task 2: 拉取并解析飞书「本周值班」表

**Files:**
- Create: `backend/app/services/oncall_feishu_sync.py`
- Test: `backend/tests/test_oncall_feishu_sync.py`（追加）

**Interfaces:**
- Consumes: `app.config.get_settings().feishu.{app_token,oncall_table_id,oncall_view_id}`（Task 1）；`app.services.feishu_cli._run_cli(*args, timeout=120, retries=3) -> Dict`（已有）
- Produces: `_emails_from_person_field(value: Any) -> List[str]`；`fetch_feishu_oncall_weeks(min_week_start: date) -> List[Dict[str, Any]]`，每项 `{"week_start": date, "members": List[str]}`，按 `week_start` 升序、已按角色合并去重、已过滤空行——供 Task 3 消费

- [ ] **Step 1: 写失败的测试**

```python
# 追加到 backend/tests/test_oncall_feishu_sync.py
from datetime import date, datetime
from zoneinfo import ZoneInfo


def _ms(y, m, d):
    return int(datetime(y, m, d, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


def test_emails_from_person_field_extracts_dedupes_and_lowercases():
    from app.services.oncall_feishu_sync import _emails_from_person_field
    value = [
        {"email": "Leon@Plaud.ai", "name": "Leon"},
        {"email": "", "name": "no email"},
        {"name": "missing email key"},
    ]
    assert _emails_from_person_field(value) == ["leon@plaud.ai"]


def test_emails_from_person_field_handles_none_and_empty():
    from app.services.oncall_feishu_sync import _emails_from_person_field
    assert _emails_from_person_field(None) == []
    assert _emails_from_person_field([]) == []


async def test_fetch_feishu_oncall_weeks_merges_roles_filters_old_and_empty(monkeypatch):
    from app.services import oncall_feishu_sync

    fake_response = {
        "data": {
            "items": [
                {
                    "fields": {
                        "日期": _ms(2026, 7, 27),
                        "值班人员（Feature）": [{"email": "leon@plaud.ai"}],
                        "值班人员（Fundamentals）": [{"email": "yunze@plaud.ai"}],
                    }
                },
                {
                    # 早于 min_week_start，必须被过滤掉
                    "fields": {
                        "日期": _ms(2026, 7, 13),
                        "值班人员（Feature）": [{"email": "jason.shao@plaud.ai"}],
                        "值班人员（Fundamentals）": [{"email": "victor@plaud.ai"}],
                    }
                },
                {
                    # 两个角色都是空，必须被跳过（不能把空行同步成"清空排班"）
                    "fields": {
                        "日期": _ms(2026, 8, 3),
                        "值班人员（Feature）": None,
                        "值班人员（Fundamentals）": None,
                    }
                },
            ]
        }
    }

    async def fake_run_cli(*args, **kwargs):
        return fake_response

    monkeypatch.setattr(oncall_feishu_sync, "_run_cli", fake_run_cli)

    weeks = await oncall_feishu_sync.fetch_feishu_oncall_weeks(date(2026, 7, 27))

    assert weeks == [
        {"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai", "yunze@plaud.ai"]},
    ]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_oncall_feishu_sync.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.services.oncall_feishu_sync'`

- [ ] **Step 3: 实现**

```python
# backend/app/services/oncall_feishu_sync.py
"""
Weekly oncall schedule sync FROM the team's Feishu Base ("本周值班（feature 同事兼顾发版）")
INTO Jarvis's internal oncall_week_assignments snapshot table.

飞书表是值班排班的权威来源（团队手工维护）；Jarvis 内部的 oncall_groups/
oncall_week_assignments 用于工单升级拉群、统计等场景。本模块每周一 08:00
(Asia/Shanghai) 把 Feishu 表里当周及未来周次的"值班人员（Feature）"+
"值班人员（Fundamentals）"合并写入 Jarvis 的排班快照，出现差异直接覆盖
(不经人工确认 — 2026-07-28 与用户确认过)。

字段结构(person 类型，取自实测 API 响应)：
  值班人员（Feature）: [{"email": "leon@plaud.ai", "name": "...", ...}, ...]
  值班人员（Fundamentals）: [{"email": "...", ...}, ...]
  发版责任人（Release Manager): 同上结构，代表"谁负责这周发版"，不算 oncall
    值班的一部分，本模块不读取这个字段。
  日期: 该值班周的周一 0 点时间戳(ms, Asia/Shanghai)。

历史脏数据：2026-01-19 之前的行是表格草创期的测试/候选池数据（同一字段可能
塞进 3-4 个人、Fundamentals 全部为空），本模块只处理 >= 本周一的行，天然
排除这段历史，不需要专门过滤。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.services.feishu_cli import _run_cli

logger = logging.getLogger("jarvis.oncall_feishu_sync")

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

_FIELD_FEATURE = "值班人员（Feature）"
_FIELD_FUNDAMENTALS = "值班人员（Fundamentals）"
_FIELD_DATE = "日期"


def _emails_from_person_field(value: Any) -> List[str]:
    """把 Feishu person 类型字段解析成邮箱列表(小写、去重、丢弃没有 email 的脏数据)。"""
    if not value:
        return []
    emails: List[str] = []
    for person in value:
        email = (person.get("email") or "").strip().lower()
        if email and email not in emails:
            emails.append(email)
    return emails


async def fetch_feishu_oncall_weeks(min_week_start: date) -> List[Dict[str, Any]]:
    """拉取 Feishu"本周值班"表,解析成 [{week_start, members}], 只保留 >= min_week_start 的行。

    `members` 是 Feature + Fundamentals 两个角色去重合并后的邮箱列表；两个角色都
    没人的行会被跳过——避免拿一行空数据把 Jarvis 已有排班冲掉。
    """
    settings = get_settings().feishu
    url = (
        f"/open-apis/bitable/v1/apps/{settings.app_token}"
        f"/tables/{settings.oncall_table_id}/records"
        f"?view_id={settings.oncall_view_id}&page_size=100"
    )
    result = await _run_cli("api", "GET", url, "--page-all", timeout=300)

    weeks: List[Dict[str, Any]] = []
    for item in result.get("data", {}).get("items", []):
        fields = item.get("fields", {})
        ms = fields.get(_FIELD_DATE)
        if ms is None:
            continue
        week_start = datetime.fromtimestamp(ms / 1000, tz=SHANGHAI_TZ).date()
        if week_start < min_week_start:
            continue
        members = sorted(set(
            _emails_from_person_field(fields.get(_FIELD_FEATURE))
            + _emails_from_person_field(fields.get(_FIELD_FUNDAMENTALS))
        ))
        if not members:
            continue
        weeks.append({"week_start": week_start, "members": members})

    weeks.sort(key=lambda w: w["week_start"])
    return weeks
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_oncall_feishu_sync.py -v`
Expected: PASS（4 个测试全过）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/oncall_feishu_sync.py backend/tests/test_oncall_feishu_sync.py
git commit -m "feat(oncall): fetch and parse Feishu oncall-table weeks"
```

---

### Task 3: 对齐 Jarvis 排班网格 + 差异检测 + 覆盖写入

**Files:**
- Modify: `backend/app/services/oncall_feishu_sync.py`
- Test: `backend/tests/test_oncall_feishu_sync.py`（追加）

**Interfaces:**
- Consumes: `db.get_oncall_config(key, default="") -> str`、`db.get_oncall_groups() -> List[Dict]`、`db.resolve_week_group(week_num, groups, start) -> Dict`（含 `group_index`/`members`/`week_start`/`week_end`）、`db.upsert_week_assignment(week_start, week_end, group_index, members, *, only_if_missing=False)`、`db.get_week_assignment(week_start) -> Optional[Dict]`（均为 `backend/app/db/database.py` 已有函数）；Task 2 的 `fetch_feishu_oncall_weeks` 返回结构
- Produces: `diff_and_sync_oncall(feishu_weeks: List[Dict[str, Any]]) -> Dict[str, Any]`，返回 `{"skipped": bool, "updated": List[dict], "unchanged": List[dict]}`，每个 dict 含 `week_start`(Jarvis 网格 key,ISO 字符串)、`feishu_week_start`(飞书原始日期,ISO 字符串)、`before`、`after`——供 Task 4 消费

- [ ] **Step 1: 写失败的测试**

```python
# 追加到 backend/tests/test_oncall_feishu_sync.py
from datetime import timedelta


async def test_diff_and_sync_overwrites_changed_week(client):
    from app.db import database as db
    from app.services.oncall_feishu_sync import diff_and_sync_oncall

    await db.save_oncall_groups([["a@plaud.ai"], ["b@plaud.ai"]], created_by="test")
    await db.set_oncall_config("start_date", "2026-07-27")  # 周一，week_num 0 = 2026-07-27

    result = await diff_and_sync_oncall([
        {"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai", "yunze@plaud.ai"]},
    ])

    assert result["skipped"] is False
    assert len(result["updated"]) == 1
    assert result["updated"][0]["before"] == ["a@plaud.ai"]
    assert result["updated"][0]["after"] == ["leon@plaud.ai", "yunze@plaud.ai"]

    snap = await db.get_week_assignment(date(2026, 7, 27))
    assert snap["members"] == ["leon@plaud.ai", "yunze@plaud.ai"]
    assert snap["group_index"] == -1


async def test_diff_and_sync_skips_unchanged_week(client):
    from app.db import database as db
    from app.services.oncall_feishu_sync import diff_and_sync_oncall

    await db.save_oncall_groups([["leon@plaud.ai"]], created_by="test")
    await db.set_oncall_config("start_date", "2026-07-27")

    result = await diff_and_sync_oncall([
        {"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai"]},
    ])

    assert result["updated"] == []
    assert len(result["unchanged"]) == 1
    assert await db.get_week_assignment(date(2026, 7, 27)) is None


async def test_diff_and_sync_aligns_to_non_monday_grid(client):
    """start_date 不是周一时(如 2026-02-10 是周二),写入的 key 必须是 Jarvis 自己
    网格算出来的日期,不能直接用 Feishu 的周一日期当 key——否则以后
    resolve_week_group 永远查不到这行,同步等于白做。"""
    from app.db import database as db
    from app.services.oncall_feishu_sync import diff_and_sync_oncall

    await db.save_oncall_groups([["old@plaud.ai"]], created_by="test")
    await db.set_oncall_config("start_date", "2026-02-10")  # 周二

    feishu_week_start = date(2026, 7, 27)
    result = await diff_and_sync_oncall([
        {"week_start": feishu_week_start, "members": ["leon@plaud.ai"]},
    ])

    assert len(result["updated"]) == 1
    start = date(2026, 2, 10)
    week_num = (feishu_week_start - start).days // 7
    expected_key = start + timedelta(weeks=week_num)
    assert expected_key != feishu_week_start  # 前提：网格确实错位，测试才有意义

    assert await db.get_week_assignment(feishu_week_start) is None  # 没写在飞书原始日期上
    snap = await db.get_week_assignment(expected_key)
    assert snap is not None
    assert snap["members"] == ["leon@plaud.ai"]


async def test_diff_and_sync_skipped_when_no_start_date(client):
    from app.services.oncall_feishu_sync import diff_and_sync_oncall

    result = await diff_and_sync_oncall([
        {"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai"]},
    ])
    assert result["skipped"] is True
    assert result["updated"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_oncall_feishu_sync.py -v`
Expected: FAIL，`ImportError: cannot import name 'diff_and_sync_oncall'`

- [ ] **Step 3: 实现**

在 `backend/app/services/oncall_feishu_sync.py` 里，`fetch_feishu_oncall_weeks` 函数后面追加：

```python
from app.db import database as db


async def diff_and_sync_oncall(feishu_weeks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把 Feishu 的值班数据对齐进 Jarvis 排班网格,有差异的周直接覆盖写入快照表。

    没有配置 `start_date`(oncall 从未初始化过)时跳过整个同步并记录 warning——
    没有网格基准,写入的 key 没有意义。
    """
    start_date_str = await db.get_oncall_config("start_date", "")
    if not start_date_str:
        logger.warning("Oncall start_date not configured — skip Feishu sync entirely")
        return {"skipped": True, "reason": "no_start_date", "updated": [], "unchanged": []}

    start = date.fromisoformat(start_date_str)
    groups = await db.get_oncall_groups()

    updated: List[Dict[str, Any]] = []
    unchanged: List[Dict[str, Any]] = []

    for week in feishu_weeks:
        feishu_week_start = week["week_start"]
        feishu_members = week["members"]

        week_num = (feishu_week_start - start).days // 7
        if week_num < 0:
            logger.warning(
                "Feishu week %s predates oncall start_date %s — skip", feishu_week_start, start,
            )
            continue

        jarvis_info = await db.resolve_week_group(week_num, groups, start)
        jarvis_members = sorted(m.strip().lower() for m in jarvis_info["members"])

        entry = {
            "week_start": jarvis_info["week_start"].isoformat(),
            "feishu_week_start": feishu_week_start.isoformat(),
            "before": jarvis_members,
            "after": feishu_members,
        }

        if feishu_members == jarvis_members:
            unchanged.append(entry)
            continue

        await db.upsert_week_assignment(
            jarvis_info["week_start"], jarvis_info["week_end"],
            group_index=-1,  # -1 = 来自 Feishu 同步,不对应任何内部 rotation group
            members=feishu_members,
            only_if_missing=False,
        )
        updated.append(entry)

    return {"skipped": False, "updated": updated, "unchanged": unchanged}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_oncall_feishu_sync.py -v`
Expected: PASS（8 个测试全过）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/oncall_feishu_sync.py backend/tests/test_oncall_feishu_sync.py
git commit -m "feat(oncall): diff Feishu weeks against Jarvis grid and overwrite on change"
```

---

### Task 4: 同步入口 + 每周一 08:00 调度循环

**Files:**
- Modify: `backend/app/services/oncall_feishu_sync.py`
- Modify: `backend/app/main.py:216-237`（lifespan 里 escalation reminder 那一段旁边）
- Test: `backend/tests/test_oncall_feishu_sync.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `fetch_feishu_oncall_weeks`、Task 3 的 `diff_and_sync_oncall`；`db.log_event(event_type, issue_id="", username="", detail=None, duration_ms=0, platform="")`（已有）
- Produces: `sync_oncall_from_feishu(today: Optional[date] = None) -> Dict[str, Any]`（可注入 `today` 便于测试，缺省用真实当前日期）；`oncall_feishu_sync_loop() -> None`（常驻协程，供 `main.py` 用 `asyncio.create_task` 启动）；`_seconds_until_next_monday_8am(now_local: datetime) -> float`

- [ ] **Step 1: 写失败的测试**

```python
# 追加到 backend/tests/test_oncall_feishu_sync.py


def test_seconds_until_next_monday_8am_same_day_before_8():
    from app.services.oncall_feishu_sync import _seconds_until_next_monday_8am
    # 2026-07-27 是周一
    now = datetime(2026, 7, 27, 6, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _seconds_until_next_monday_8am(now) == 2 * 3600


def test_seconds_until_next_monday_8am_same_day_after_8_goes_to_next_week():
    from app.services.oncall_feishu_sync import _seconds_until_next_monday_8am
    now = datetime(2026, 7, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    expected = (datetime(2026, 8, 3, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")) - now).total_seconds()
    assert _seconds_until_next_monday_8am(now) == expected


def test_seconds_until_next_monday_8am_midweek():
    from app.services.oncall_feishu_sync import _seconds_until_next_monday_8am
    # 2026-07-29 是周三
    now = datetime(2026, 7, 29, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    expected = (datetime(2026, 8, 3, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")) - now).total_seconds()
    assert _seconds_until_next_monday_8am(now) == expected


async def test_sync_oncall_from_feishu_end_to_end(client, monkeypatch):
    from app.db import database as db
    from app.services import oncall_feishu_sync

    await db.save_oncall_groups([["old@plaud.ai"]], created_by="test")
    await db.set_oncall_config("start_date", "2026-07-27")

    async def fake_fetch(min_week_start):
        assert min_week_start == date(2026, 7, 27)
        return [{"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai", "yunze@plaud.ai"]}]

    monkeypatch.setattr(oncall_feishu_sync, "fetch_feishu_oncall_weeks", fake_fetch)

    result = await oncall_feishu_sync.sync_oncall_from_feishu(today=date(2026, 7, 27))

    assert result["skipped"] is False
    assert len(result["updated"]) == 1
    snap = await db.get_week_assignment(date(2026, 7, 27))
    assert snap["members"] == ["leon@plaud.ai", "yunze@plaud.ai"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_oncall_feishu_sync.py -v`
Expected: FAIL，`ImportError: cannot import name '_seconds_until_next_monday_8am'`

- [ ] **Step 3: 实现**

在 `backend/app/services/oncall_feishu_sync.py` 顶部 import 区加 `import asyncio`，在文件末尾追加：

```python
SYNC_HOUR_LOCAL = 8


async def sync_oncall_from_feishu(today: Optional[date] = None) -> Dict[str, Any]:
    """入口:拉取 Feishu + 对比 + 覆盖写入,并记录审计事件。供 loop 和手动触发端点复用。

    `today` 可注入(测试用),缺省取 Asia/Shanghai 的真实当前日期。
    """
    resolved_today = today or datetime.now(SHANGHAI_TZ).date()
    this_monday = resolved_today - timedelta(days=resolved_today.weekday())

    feishu_weeks = await fetch_feishu_oncall_weeks(this_monday)
    result = await diff_and_sync_oncall(feishu_weeks)

    await db.log_event(
        "oncall_feishu_sync",
        detail={
            "updated_weeks": [u["week_start"] for u in result["updated"]],
            "unchanged_weeks": len(result["unchanged"]),
            "skipped": result["skipped"],
        },
    )
    if result["updated"]:
        logger.info(
            "Oncall Feishu sync: updated %d week(s): %s",
            len(result["updated"]), [u["week_start"] for u in result["updated"]],
        )
    else:
        logger.info("Oncall Feishu sync: no changes (%d week(s) checked)", len(result["unchanged"]))
    return result


def _seconds_until_next_monday_8am(now_local: datetime) -> float:
    target = now_local.replace(hour=SYNC_HOUR_LOCAL, minute=0, second=0, microsecond=0)
    days_ahead = (0 - now_local.weekday()) % 7  # Monday == 0
    if days_ahead == 0 and now_local >= target:
        days_ahead = 7
    target = target + timedelta(days=days_ahead)
    return (target - now_local).total_seconds()


async def oncall_feishu_sync_loop():
    """Background loop: fire every Monday at 08:00 Asia/Shanghai."""
    while True:
        try:
            now_local = datetime.now(SHANGHAI_TZ)
            wait_s = _seconds_until_next_monday_8am(now_local)
            logger.info("Next oncall Feishu sync in %.1f hours (Monday 08:00 SH)", wait_s / 3600)
            await asyncio.sleep(wait_s)
            await sync_oncall_from_feishu()
            await asyncio.sleep(60)  # 避免本次跑得飞快导致同一分钟内重复触发
        except asyncio.CancelledError:
            logger.info("Oncall Feishu sync loop cancelled")
            return
        except Exception as e:
            logger.error("Oncall Feishu sync loop error (retry in 1h): %s", e, exc_info=True)
            await asyncio.sleep(3600)
```

在 `backend/app/main.py`，紧跟着现有的 escalation reminder 那一段（第 216-224 行，`if os.environ.get("ENABLE_ONCALL_NOTIFY", ...)`），加一段结构完全对称的注册代码：

```python
    # Weekly oncall sync FROM Feishu「本周值班」表 (每周一 08:00 Asia/Shanghai)
    # 默认关闭,评审后手动 ENABLE_ONCALL_FEISHU_SYNC=true 打开 (与 escalation
    # reminder 的既有约定一致,不能部署即生效)。
    oncall_feishu_sync_task = None
    if os.environ.get("ENABLE_ONCALL_FEISHU_SYNC", "false").lower() == "true":
        from app.services.oncall_feishu_sync import oncall_feishu_sync_loop
        oncall_feishu_sync_task = asyncio.create_task(oncall_feishu_sync_loop())
        logger.info("Oncall Feishu sync loop started (ENABLE_ONCALL_FEISHU_SYNC=true)")
    else:
        logger.info("Oncall Feishu sync disabled (set ENABLE_ONCALL_FEISHU_SYNC=true to enable)")
```

并在 `yield` 之后的清理段（第 228-229 行 `if reminder_task is not None: reminder_task.cancel()` 后面）加：

```python
    if oncall_feishu_sync_task is not None:
        oncall_feishu_sync_task.cancel()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_oncall_feishu_sync.py -v`
Expected: PASS（12 个测试全过）

- [ ] **Step 5: 跑一次完整的 backend 测试套件,确认没有把 main.py 改坏**

Run: `cd backend && pytest tests/ -v -x -k "not crashguard"`
Expected: PASS（`main.py` 的改动只是新增一段 gated-by-default 的代码，不应影响任何既有测试）

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/oncall_feishu_sync.py backend/app/main.py backend/tests/test_oncall_feishu_sync.py
git commit -m "feat(oncall): weekly Monday 08:00 Feishu sync loop (gated by ENABLE_ONCALL_FEISHU_SYNC)"
```

---

### Task 5: 管理员手动触发端点

**Files:**
- Modify: `backend/app/api/oncall.py`（`update_schedule` 端点，第 198-220 行附近之后）
- Test: `backend/tests/test_oncall.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 `app.services.oncall_feishu_sync.sync_oncall_from_feishu()`；`db.get_user(username) -> Optional[Dict]`（已有，`user["role"]` 判断 admin）
- Produces: `POST /api/oncall/sync-from-feishu?username=<admin>` → 200 时返回 `sync_oncall_from_feishu()` 的原始结果 dict；非 admin → 403

- [ ] **Step 1: 写失败的测试**

```python
# 追加到 backend/tests/test_oncall.py
async def test_sync_from_feishu_requires_admin(client):
    await seed_user(client, "regular")
    resp = await client.post("/api/oncall/sync-from-feishu", params={"username": "regular"})
    assert resp.status_code == 403


async def test_sync_from_feishu_admin_triggers_sync(client, monkeypatch):
    from app.services import oncall_feishu_sync

    async def fake_sync(today=None):
        return {"skipped": False, "updated": [], "unchanged": []}

    monkeypatch.setattr(oncall_feishu_sync, "sync_oncall_from_feishu", fake_sync)
    await seed_admin(client, "sanato")
    resp = await client.post("/api/oncall/sync-from-feishu", params={"username": "sanato"})
    assert resp.status_code == 200
    assert resp.json() == {"skipped": False, "updated": [], "unchanged": []}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && pytest tests/test_oncall.py -k sync_from_feishu -v`
Expected: FAIL，404（路由不存在）

- [ ] **Step 3: 实现**

在 `backend/app/api/oncall.py` 里，`update_schedule` 函数结尾（第 220 行）之后加：

```python
@router.post("/sync-from-feishu")
async def sync_from_feishu(username: str = Query(..., description="Admin username")):
    """Manually trigger the weekly Feishu -> Jarvis oncall sync (admin only).

    对应每周一 08:00 自动跑的同一逻辑(`services/oncall_feishu_sync.py`)，用于
    上线后手动验证一次，不必等到下一个周一。
    """
    user = await db.get_user(username)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can trigger oncall sync")

    from app.services.oncall_feishu_sync import sync_oncall_from_feishu
    return await sync_oncall_from_feishu()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && pytest tests/test_oncall.py -v`
Expected: PASS（全部通过，含新增的两个）

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/oncall.py backend/tests/test_oncall.py
git commit -m "feat(oncall): add admin endpoint to manually trigger Feishu sync"
```

---

### Task 6: 模块文档更新

**Files:**
- Modify: `docs/modules/oncall.md`

**Interfaces:**
- Consumes: 无（纯文档）
- Produces: 无

- [ ] **Step 1: 在 `docs/modules/oncall.md` 的「后端」章节里，`### API 端点` 表格之后加一节**

```markdown
### 飞书值班表同步（2026-07-28）

团队在飞书维护权威的值班排班表「本周值班（feature 同事兼顾发版）」
（`app_token=BmjmbSpxxabP2dsuxbtcUTYAn4g`，与工单表同一个 Base，`table_id`
见 `FeishuSettings.oncall_table_id`）。`services/oncall_feishu_sync.py` 每周一
08:00 (Asia/Shanghai) 拉取该表当周及未来周次的「值班人员（Feature）」+
「值班人员（Fundamentals）」两个角色，去重合并邮箱后覆盖写入 Jarvis 的
`oncall_week_assignments` 排班快照表（不动 `发版责任人` 字段——那是谁负责
发版，不算 oncall 值班）。

- 飞书某周两个角色都为空 → 跳过，不清空 Jarvis 已有排班。
- 从未配置过 `start_date` → 整体跳过，不做任何写入。
- 检测到差异直接覆盖（不经人工确认这一步）——但整个定时循环默认关闭，需要
  `ENABLE_ONCALL_FEISHU_SYNC=true` 显式开启（同 `ENABLE_ONCALL_NOTIFY` 的约定）。
- 管理员可调 `POST /api/oncall/sync-from-feishu?username=<admin>` 手动跑一次，
  不必等到下周一，便于上线后立即验证。
```

- [ ] **Step 2: Commit**

```bash
git add docs/modules/oncall.md
git commit -m "docs(oncall): document Feishu weekly sync mechanism"
```
