# Oncall 值周工单 API + 分析 Skill + 全局反馈入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 暴露一个按值周窗口聚合两类工单的只读 API，配套一个 AI 驱动的工单分析 skill，并给所有页面加一个右下角悬浮反馈入口（截图 + 飞书私聊）。

**Architecture:** 后端在 `app/api/oncall.py` 加一个纯函数 `resolve_duty_week()` + 一个聚合端点 `GET /api/oncall/my-workload`；附件复用既有 `/api/local/*` 下载端点。反馈入口为一个全局 React 组件 + 一个 `POST /api/site-feedback` 端点，经新增的飞书图片上传 helper 私聊管理员。Skill 是 `~/Desktop/code/myskill/SKILL.md`，调上面 API 后由 AI 自由分析。

**Tech Stack:** FastAPI + SQLAlchemy + pytest（后端）、Next.js 15 + React 19 + Tailwind + html2canvas（前端）、Feishu Open API、Markdown skill。

## Global Constraints

- 只读优先：新 API 与 skill 不向生产 DB 写任何数据。
- 不自动 commit/push/部署——本计划每个 Task 末尾的 `git commit` 在本地分支执行，推送/部署由用户另行指令。
- 站点主题金调 `#B8922E`；前端文案走 `useT()`，新文案先在 `src/lib/i18n.ts` 加中文 key → 英文。
- 前端所有后端调用集中在 `src/lib/api.ts`，组件不直接 `fetch`。
- 飞书邮箱匹配统一小写后做**成员匹配**（assignee 列表通常 2 人）。
- 附件链接用相对路径（`/api/local/...`），由调用方拼 base；`feishu_link`/`zendesk_url`/`apollo_url` 用绝对地址。
- 反馈收件人默认 `sanato.zhang@plaud.ai`，经 `settings.feedback_recipient` 可配。
- 后端测试：`cd backend && source .venv/bin/activate && pytest`。

---

### Task 1: 值周窗口解析纯函数 `resolve_duty_week`

**Files:**
- Modify: `backend/app/api/oncall.py`（顶部加函数）
- Test: `backend/tests/test_oncall.py`（追加）

**Interfaces:**
- Produces: `resolve_duty_week(groups: list[dict], start_date_str: str, email: str, today: date) -> Optional[dict]`，返回 `{"group_index": int, "week_num": int, "week_start": date, "week_end": date, "is_current": bool, "partners": list[str]}`，email 不在任何组 / 无排班 / 该人尚未轮到值周时返回 `None`。

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_oncall.py` 末尾追加：

```python
from datetime import date


def test_resolve_duty_week_current_week():
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com"]}, {"members": ["b@x.com"]}]
    # start 2026-06-01, today 2026-06-25 → 24 天 → week 3 → 3%2=1 → b 当周值周
    info = resolve_duty_week(groups, "2026-06-01", "B@x.com", date(2026, 6, 25))
    assert info is not None
    assert info["group_index"] == 1
    assert info["week_num"] == 3
    assert info["is_current"] is True
    assert info["week_start"] == date(2026, 6, 22)
    assert info["week_end"] == date(2026, 6, 28)
    assert info["partners"] == []


def test_resolve_duty_week_most_recent_past():
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com", "c@x.com"]}, {"members": ["b@x.com"]}]
    # a 在 group 0；today week 3 → a 最近值周是 week 2（2026-06-15）
    info = resolve_duty_week(groups, "2026-06-01", "a@x.com", date(2026, 6, 25))
    assert info["group_index"] == 0
    assert info["week_num"] == 2
    assert info["is_current"] is False
    assert info["week_start"] == date(2026, 6, 15)
    assert info["partners"] == ["c@x.com"]


def test_resolve_duty_week_not_member():
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com"]}]
    assert resolve_duty_week(groups, "2026-06-01", "nobody@x.com", date(2026, 6, 25)) is None
    assert resolve_duty_week([], "2026-06-01", "a@x.com", date(2026, 6, 25)) is None
    assert resolve_duty_week(groups, "", "a@x.com", date(2026, 6, 25)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_oncall.py -k resolve_duty_week -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_duty_week'`

- [ ] **Step 3: Write minimal implementation**

在 `backend/app/api/oncall.py` 顶部（`router = APIRouter()` 之后）加：

```python
def resolve_duty_week(
    groups: List[Dict[str, Any]],
    start_date_str: str,
    email: str,
    today: date,
) -> Optional[Dict[str, Any]]:
    """Resolve the most recent duty week for `email`.

    Returns None when there is no schedule, the email is not in any group,
    or the person has not yet had a duty week (start in the future for them).
    """
    if not groups or not start_date_str:
        return None
    email_l = email.strip().lower()
    group_index = None
    for i, g in enumerate(groups):
        members = [m.strip().lower() for m in g.get("members", [])]
        if email_l in members:
            group_index = i
            break
    if group_index is None:
        return None
    try:
        start = date.fromisoformat(start_date_str)
    except ValueError:
        return None

    n = len(groups)
    current_week = max(0, (today - start).days // 7)
    # largest week_num <= current_week with week_num % n == group_index
    duty = current_week - ((current_week - group_index) % n)
    if duty < 0:
        return None
    week_start = start + timedelta(weeks=duty)
    week_end = week_start + timedelta(days=6)
    partners = [
        m for m in groups[group_index].get("members", [])
        if m.strip().lower() != email_l
    ]
    return {
        "group_index": group_index,
        "week_num": duty,
        "week_start": week_start,
        "week_end": week_end,
        "is_current": duty == current_week,
        "partners": partners,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_oncall.py -k resolve_duty_week -v`
Expected: PASS（3 个测试）

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/oncall.py backend/tests/test_oncall.py
git commit -m "feat(oncall): add resolve_duty_week helper for duty-week window"
```

---

### Task 2: `GET /api/oncall/my-workload` 聚合端点

**Files:**
- Modify: `backend/app/api/oncall.py`（新增端点）
- Test: `backend/tests/test_oncall.py`（追加）
- Modify: `docs/modules/oncall.md`（API 端点表加一行）

**Interfaces:**
- Consumes: `resolve_duty_week(...)`（Task 1）；`db.get_oncall_groups()`、`db.get_oncall_config("start_date","")`、`db.get_escalated_issues(status=None)`；`app.services.feishu.FeishuClient.list_issues_by_status(status, limit, assignee_emails)`、`FeishuClient._normalize_zendesk_url(str)`。
- Produces: `GET /api/oncall/my-workload?email=<email>` → JSON `{email, duty_week, oncall_partners, apollo_tickets[], feishu_tickets[], summary}`。

- [ ] **Step 1: Write the failing test**

在 `backend/tests/test_oncall.py` 追加（顶部已 `from tests.conftest import seed_admin, seed_user`，这里再 import 需要的）：

```python
from unittest.mock import patch, AsyncMock


async def test_my_workload_not_member(client):
    # 未配置排班 → 404
    resp = await client.get("/api/oncall/my-workload", params={"email": "x@x.com"})
    assert resp.status_code == 404


async def test_my_workload_aggregates(client, db_session):
    from tests.conftest import seed_issue, seed_task, seed_analysis
    from datetime import datetime
    import app.db.database as db_mod

    # 排班：单组 a@x.com，start 2026-06-22（本周）
    await seed_admin(client, "sanato")
    await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": [{"members": ["a@x.com", "b@x.com"]}],
        "start_date": "2026-06-22",
    })

    # 一个窗口内的升级工单（escalated_at 在本周）
    await seed_issue(db_session, issue_id="esc_in", source="feishu", status="done")
    await seed_task(db_session, task_id="t_in", issue_id="esc_in")
    await seed_analysis(db_session, task_id="t_in", issue_id="esc_in", problem_type="蓝牙")
    async with db_session() as s:
        rec = await s.get(db_mod.IssueRecord, "esc_in")
        rec.escalated_at = datetime(2026, 6, 23, 10, 0, 0)
        rec.escalation_status = "in_progress"
        rec.zendesk_id = "#378794"
        await s.commit()

    # mock 飞书工单（一个窗口内、指派给 a@x.com）
    from app.models.schemas import Issue, LogFile, IssueStatus
    fk_issue = Issue(
        record_id="fk1", description="无法连接", assignee_emails=["a@x.com"],
        feishu_link="https://feishu/fk1", created_at_ms=1750636800000,  # 2026-06-23
        feishu_status=IssueStatus.IN_PROGRESS,
        log_files=[LogFile(name="log.plaud", token="tok", size=123)],
    )

    async def fake_list(status, limit=200, assignee_emails=None):
        return [fk_issue] if status == "in_progress" else []

    with patch("app.services.feishu.FeishuClient.list_issues_by_status", new=AsyncMock(side_effect=fake_list)):
        resp = await client.get("/api/oncall/my-workload", params={"email": "a@x.com"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["duty_week"]["is_current"] is True
    assert data["oncall_partners"] == ["b@x.com"]
    assert data["summary"]["apollo_count"] == 1
    assert data["summary"]["feishu_count"] == 1
    assert data["apollo_tickets"][0]["logs_download_url"] == "/api/local/esc_in/download-logs"
    assert data["apollo_tickets"][0]["zendesk_url"]  # 由 zendesk_id 拼出
    att = data["feishu_tickets"][0]["attachments"][0]
    assert att["download_path"] == "/api/local/fk1/files/log.plaud"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_oncall.py -k my_workload -v`
Expected: FAIL with 404/路由不存在（`my_workload_aggregates` 404 或 KeyError）

- [ ] **Step 3: Write minimal implementation**

在 `backend/app/api/oncall.py` 顶部 import 区补 `from datetime import date, timedelta, datetime, time as dtime`（现有已 import 前三个，追加 `time as dtime`），并新增端点：

```python
@router.get("/my-workload")
async def get_my_workload(email: str = Query(..., description="Oncall member email")):
    """Aggregate the tickets an oncall member must handle in their most recent
    duty week: apollo escalated tickets + Feishu tickets, with links + attachments.
    Read-only.
    """
    from app.config import get_settings
    from app.services.feishu import FeishuClient

    groups = await db.get_oncall_groups()
    start_date_str = await db.get_oncall_config("start_date", "")
    info = resolve_duty_week(groups, start_date_str, email, date.today())
    if info is None:
        raise HTTPException(status_code=404, detail=f"{email} is not an oncall member or no schedule configured")

    week_start = info["week_start"]
    week_end = info["week_end"]
    frontend_base = (get_settings().frontend_base_url or "").rstrip("/")
    email_l = email.strip().lower()

    # --- apollo escalated tickets within window, still open ---
    apollo_tickets = []
    for it in await db.get_escalated_issues(status=None):
        if it.get("escalation_status") == "resolved":
            continue
        esc = it.get("escalated_at") or ""
        if not esc:
            continue
        try:
            esc_d = date.fromisoformat(esc[:10])
        except ValueError:
            continue
        if not (week_start <= esc_d <= week_end):
            continue
        zid = it.get("zendesk_id", "")
        rid = it["record_id"]
        apollo_tickets.append({
            "record_id": rid,
            "description": it.get("description", ""),
            "problem_type": it.get("problem_type", ""),
            "root_cause": it.get("root_cause", ""),
            "confidence": it.get("confidence", ""),
            "zendesk_id": zid,
            "zendesk_url": FeishuClient._normalize_zendesk_url(zid) if zid else "",
            "escalated_at": esc,
            "escalated_by": it.get("escalated_by", ""),
            "escalation_status": it.get("escalation_status", ""),
            "escalation_share_link": it.get("escalation_share_link", ""),
            "apollo_url": f"{frontend_base}/tracking?detail={rid}" if frontend_base else "",
            "logs_download_url": f"/api/local/{rid}/download-logs",
        })

    # --- Feishu tickets assigned to email, created within window, open ---
    start_ms = int(datetime.combine(week_start, dtime.min).timestamp() * 1000)
    end_ms = int(datetime.combine(week_end, dtime.max).timestamp() * 1000)
    client = FeishuClient()
    pending = await client.list_issues_by_status("pending", limit=200, assignee_emails=[email_l])
    in_progress = await client.list_issues_by_status("in_progress", limit=200, assignee_emails=[email_l])

    feishu_tickets = []
    for iss in in_progress + pending:
        if not (start_ms <= iss.created_at_ms <= end_ms):
            continue
        attachments = [
            {"name": f.name, "size": f.size, "download_path": f"/api/local/{iss.record_id}/files/{f.name}"}
            for f in iss.log_files
        ]
        feishu_tickets.append({
            "record_id": iss.record_id,
            "description": iss.description,
            "priority": iss.priority,
            "device_sn": iss.device_sn,
            "firmware": iss.firmware,
            "app_version": iss.app_version,
            "assignee": iss.assignee,
            "assignee_emails": iss.assignee_emails,
            "feishu_link": iss.feishu_link,
            "zendesk": iss.zendesk,
            "zendesk_id": iss.zendesk_id,
            "feishu_status": iss.feishu_status.value if hasattr(iss.feishu_status, "value") else iss.feishu_status,
            "created_at_ms": iss.created_at_ms,
            "attachments": attachments,
        })

    with_attachments = sum(1 for t in feishu_tickets if t["attachments"]) + len(apollo_tickets)
    return {
        "email": email_l,
        "duty_week": {
            "week_num": info["week_num"],
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "is_current": info["is_current"],
        },
        "oncall_partners": info["partners"],
        "apollo_tickets": apollo_tickets,
        "feishu_tickets": feishu_tickets,
        "summary": {
            "apollo_count": len(apollo_tickets),
            "feishu_count": len(feishu_tickets),
            "total": len(apollo_tickets) + len(feishu_tickets),
            "with_attachments": with_attachments,
        },
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_oncall.py -k my_workload -v`
Expected: PASS（2 个测试）。再跑全量 `pytest tests/test_oncall.py -v` 确认未回归。

- [ ] **Step 5: 更新文档**

在 `docs/modules/oncall.md` 的「API 端点」表（`GET /api/oncall/stats` 行下方）加：

```markdown
| `GET`  | `/api/oncall/my-workload` | 按邮箱反查最近值周窗，聚合 apollo 升级工单 + 飞书工单（含链接 + 附件），供 skill 拉取 |
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/oncall.py backend/tests/test_oncall.py docs/modules/oncall.md
git commit -m "feat(oncall): add /my-workload aggregated read endpoint for skill"
```

---

### Task 3: 工单分析 Skill `~/Desktop/code/myskill`

**Files:**
- Create: `~/Desktop/code/myskill/SKILL.md`

**Interfaces:**
- Consumes: `GET {API_BASE}/api/oncall/my-workload?email=<email>`（Task 2）；`/api/local/<id>/download-logs`、`/api/local/<id>/files/<name>`；`plaud-log-decrypt` skill。
- Produces: 无代码接口，是一个可触发的 skill。

- [ ] **Step 1: 创建 skill 目录与 SKILL.md**

```bash
mkdir -p ~/Desktop/code/myskill
```

写 `~/Desktop/code/myskill/SKILL.md`：

````markdown
---
name: oncall-ticket-analysis
description: "拉取当前 git 用户值周需人工处理的工单（apollo 升级单 + 飞书工单）并逐单分析。触发词：'分析我的值周工单'、'oncall 工单分析'、'/oncall-ticket-analysis'。"
metadata:
  requires:
    bins: [git, curl]
---

# oncall-ticket-analysis

按当前 git 用户邮箱拉取 ta 最近值周窗口内需人工处理的工单，先列清单，再让 AI 自由探索日志和代码逐单分析。

## 配置

- `API_BASE`：默认 `http://10.0.52.102:8000`。用户触发时可覆盖（如 "用 http://localhost:8000"）。
- 代码库路径：默认当前工作目录（假定在 jarvis repo 内触发）。若 cwd 非 jarvis repo，提示用户指定。

## 步骤

### 1. 取邮箱
运行 `git config user.email`。取不到则让用户手动提供邮箱。

### 2. 拉工单
`curl -s "${API_BASE}/api/oncall/my-workload?email=<email>"`。
- 404 → 告诉用户该邮箱不是 oncall 成员或未配置排班，停止。
- 解析返回的 `duty_week` / `apollo_tickets` / `feishu_tickets` / `summary`。

### 3. 先列清单（每次触发必做）
输出一段概览：
- 值周窗 `week_start ~ week_end`（is_current 标注是否本周）、同组搭档 `oncall_partners`。
- `summary.total` 个工单，分 apollo / feishu 计数。
- 逐条一行：`[apollo|feishu] <record_id> — <description 一句话> — 附件:有/无`。
  - apollo 附件视为「有」（logs_download_url 始终存在）；feishu 看 `attachments` 是否非空。

### 4. 逐单详细分析
对每个工单：
1. **取附件**（拼 `API_BASE` + 相对路径）：
   - feishu：逐个 `attachments[].download_path`。
   - apollo：`logs_download_url`（单文件直传 / 多文件 zip）。
   - 下到本地临时目录。
2. **解密日志**：`.plaud` 加密文件 → 调用 `plaud-log-decrypt` skill 解密成可读文本。
3. **自由探索**：结合解密日志 + jarvis 代码库（`backend/app/...`）+ `backend/rules/*.md` 参考规则，定位根因。可按问题类型参考对应 `analyze-*` skill（如 analyze-bluetooth / analyze-cloud-sync）作为分析模板。
4. **按 result.json 契约产出报告段**（markdown）：
   - `problem_type` / `problem_type_en`
   - `root_cause` / `root_cause_en`
   - `confidence`：high | medium | low
   - `key_evidence`：≤5 条关键日志行
   - `user_reply` / `user_reply_en`：客服回复模版
   - `needs_engineer`：bool
   - `fix_suggestion`：修复建议
   - 附：工单链接（apollo `apollo_url` / feishu `feishu_link`、zendesk）

### 5. 最终输出
= 第 3 步清单 + 每单第 4 步报告。

## 约束
- 只读：只 GET API + 下载附件，不写任何库、不触发后端分析、不 commit/push。
- 证据优先：每条 root_cause 必须有 key_evidence 日志行支撑；找不到日志就如实说明，不臆测。
````

- [ ] **Step 2: 手动验证（人工）**

Run（替换为真实值周邮箱）：
```bash
curl -s "http://10.0.52.102:8000/api/oncall/my-workload?email=<real-oncall-email>" | head -c 400
```
Expected: 返回 JSON，含 `duty_week` 与 `summary`。然后在装有该 skill 的会话里触发 `/oncall-ticket-analysis`，确认先打印清单、再逐单分析。

- [ ] **Step 3: Commit（skill 在 jarvis repo 外，单独说明）**

`~/Desktop/code/myskill` 不在 jarvis repo 内，无需在本仓库提交。如需版本管理由用户决定是否单独建仓。本步骤无 git 操作。

---

### Task 4: 飞书图片上传 + 图片消息 helper

**Files:**
- Modify: `backend/app/services/feishu_cli.py`（新增两个函数）
- Test: `backend/tests/test_feishu_image.py`（新建）

**Interfaces:**
- Produces:
  - `async def upload_image(image_bytes: bytes) -> str`（返回 `image_key`）
  - `async def send_image_message(image_key: str, chat_id: str = "", email: str = "") -> bool`

- [ ] **Step 1: Write the failing test**

新建 `backend/tests/test_feishu_image.py`：

```python
from unittest.mock import AsyncMock, patch


async def test_send_image_message_uses_email_receiver():
    from app.services import feishu_cli
    with patch.object(feishu_cli, "_feishu_api", new=AsyncMock(return_value={"code": 0, "data": {}})) as m:
        ok = await feishu_cli.send_image_message(image_key="img_xxx", email="sanato.zhang@plaud.ai")
    assert ok is True
    # 校验调用了 im/v1/messages、receive_id_type=email、msg_type=image
    args, kwargs = m.call_args
    assert args[0] == "POST"
    assert args[1] == "/im/v1/messages"
    assert kwargs["params"]["receive_id_type"] == "email"
    assert kwargs["body"]["msg_type"] == "image"
    assert '"image_key": "img_xxx"' in kwargs["body"]["content"] or '"image_key":"img_xxx"' in kwargs["body"]["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_feishu_image.py -v`
Expected: FAIL with `AttributeError: module 'app.services.feishu_cli' has no attribute 'send_image_message'`

- [ ] **Step 3: Write minimal implementation**

在 `backend/app/services/feishu_cli.py` 末尾（模块级，与 `send_message` 同层）加：

```python
async def upload_image(image_bytes: bytes) -> str:
    """Upload an image to Feishu and return its image_key (multipart)."""
    token = await _get_tenant_token()
    url = "https://open.feishu.cn/open-apis/im/v1/images"
    async with httpx.AsyncClient(verify=False, timeout=30) as http:
        resp = await http.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": ("feedback.png", image_bytes, "image/png")},
        )
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu image upload error ({result.get('code')}): {result.get('msg', result)}")
        return result["data"]["image_key"]


async def send_image_message(image_key: str, chat_id: str = "", email: str = "") -> bool:
    """Send an image message to a chat or a user (by email)."""
    if not chat_id and not email:
        raise ValueError("Either chat_id or email required")
    import json as _json
    content = _json.dumps({"image_key": image_key}, ensure_ascii=False)
    try:
        if chat_id:
            await _feishu_api("POST", "/im/v1/messages", params={"receive_id_type": "chat_id"},
                              body={"receive_id": chat_id, "msg_type": "image", "content": content})
        else:
            await _feishu_api("POST", "/im/v1/messages", params={"receive_id_type": "email"},
                              body={"receive_id": email, "msg_type": "image", "content": content})
        return True
    except Exception as e:
        logger.error("Failed to send image message: %s", e)
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_feishu_image.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/feishu_cli.py backend/tests/test_feishu_image.py
git commit -m "feat(feishu): add upload_image + send_image_message helpers"
```

---

### Task 5: `POST /api/site-feedback` 端点 + 收件人配置

**Files:**
- Create: `backend/app/api/site_feedback.py`
- Modify: `backend/app/config.py`（`Settings` 加 `feedback_recipient`）
- Modify: `backend/app/main.py`（挂载 router）
- Modify: `backend/CLAUDE.md`（路由总览加一行）
- Test: `backend/tests/test_site_feedback.py`（新建）

**Interfaces:**
- Consumes: `feishu_cli.upload_image`、`feishu_cli.send_message`、`feishu_cli.send_image_message`（Task 4）；`settings.feedback_recipient`。
- Produces: `POST /api/site-feedback`，body `{message, page_url?, screenshot?, user_email?}` → `{status, image_sent}`。

- [ ] **Step 1: Write the failing test**

新建 `backend/tests/test_site_feedback.py`：

```python
import base64
from unittest.mock import AsyncMock, patch


async def test_site_feedback_text_and_image(client):
    png_b64 = "data:image/png;base64," + base64.b64encode(b"\x89PNGfake").decode()
    with patch("app.services.feishu_cli.send_message", new=AsyncMock(return_value=True)) as send_msg, \
         patch("app.services.feishu_cli.upload_image", new=AsyncMock(return_value="img_1")) as up, \
         patch("app.services.feishu_cli.send_image_message", new=AsyncMock(return_value=True)) as send_img:
        resp = await client.post("/api/site-feedback", json={
            "message": "按钮点不动",
            "page_url": "http://x/tracking?detail=abc",
            "screenshot": png_b64,
            "user_email": "u@plaud.ai",
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    assert body["image_sent"] is True
    # 文本消息含反馈内容与工单 URL
    sent_text = send_msg.call_args.kwargs.get("text", "")
    assert "按钮点不动" in sent_text and "tracking?detail=abc" in sent_text
    up.assert_awaited_once()
    send_img.assert_awaited_once()


async def test_site_feedback_text_only(client):
    with patch("app.services.feishu_cli.send_message", new=AsyncMock(return_value=True)), \
         patch("app.services.feishu_cli.upload_image", new=AsyncMock(return_value="img_1")) as up:
        resp = await client.post("/api/site-feedback", json={"message": "仅文字"})
    assert resp.status_code == 200
    assert resp.json()["image_sent"] is False
    up.assert_not_awaited()


async def test_site_feedback_requires_message(client):
    resp = await client.post("/api/site-feedback", json={"message": "   "})
    assert resp.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_site_feedback.py -v`
Expected: FAIL（404，路由未挂载）

- [ ] **Step 3: 加配置项**

在 `backend/app/config.py` 的 `class Settings` 内，`frontend_base_url` 字段附近加：

```python
    feedback_recipient: str = "sanato.zhang@plaud.ai"   # 反馈 widget 收件人（飞书邮箱）
```

- [ ] **Step 4: 写端点**

新建 `backend/app/api/site_feedback.py`：

```python
"""Global site feedback widget → Feishu DM to admin."""
from __future__ import annotations

import base64
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.config import get_settings
from app.services import feishu_cli

logger = logging.getLogger("jarvis.api.site_feedback")
router = APIRouter()


class SiteFeedbackInput(BaseModel):
    message: str
    page_url: str | None = None
    screenshot: str | None = None   # data:image/png;base64,...
    user_email: str | None = None

    @field_validator("message")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message required")
        return v.strip()


def _decode_screenshot(raw: str) -> bytes | None:
    if not raw:
        return None
    b64 = raw.split(",", 1)[1] if raw.startswith("data:") else raw
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


@router.post("")
async def submit_site_feedback(req: SiteFeedbackInput):
    recipient = get_settings().feedback_recipient
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = ["📝 站点反馈", f"内容：{req.message}"]
    if req.user_email:
        lines.append(f"提交人：{req.user_email}")
    if req.page_url:
        lines.append(f"工单页：{req.page_url}")
    lines.append(f"时间：{ts}")
    text = "\n".join(lines)

    text_ok = await feishu_cli.send_message(email=recipient, text=text)

    image_sent = False
    img_bytes = _decode_screenshot(req.screenshot) if req.screenshot else None
    if img_bytes:
        try:
            image_key = await feishu_cli.upload_image(img_bytes)
            image_sent = await feishu_cli.send_image_message(image_key=image_key, email=recipient)
        except Exception as e:
            logger.warning("Feedback screenshot delivery failed: %s", e)

    if not text_ok:
        raise HTTPException(status_code=502, detail="Failed to deliver feedback to Feishu")
    return {"status": "sent", "image_sent": image_sent}
```

- [ ] **Step 5: 挂载 router**

在 `backend/app/main.py` 的 router import 区（`from app.api.local import router as local_router` 附近）加：

```python
from app.api.site_feedback import router as site_feedback_router
```

在 `app.include_router(local_router, ...)` 附近加：

```python
app.include_router(site_feedback_router, prefix="/api/site-feedback", tags=["SiteFeedback"])
```

并在 `backend/tests/conftest.py` 的 `api_modules_with_local_get_settings` 元组里加入 `"app.api.site_feedback",`（保持与其它 api 模块一致的 settings patch）。

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && pytest tests/test_site_feedback.py -v`
Expected: PASS（3 个测试）

- [ ] **Step 7: 更新文档 + Commit**

在 `backend/CLAUDE.md` 的「API 路由总览」表加：

```markdown
| `/api/site-feedback` | `api/site_feedback.py` | 全局反馈 widget → 飞书私聊管理员 |
```

```bash
git add backend/app/api/site_feedback.py backend/app/config.py backend/app/main.py backend/tests/conftest.py backend/tests/test_site_feedback.py backend/CLAUDE.md
git commit -m "feat(api): add /api/site-feedback endpoint forwarding to Feishu DM"
```

---

### Task 6: 前端全局反馈 Widget

**Files:**
- Create: `frontend/src/components/FeedbackWidget.tsx`
- Modify: `frontend/src/lib/api.ts`（加 `submitSiteFeedback` wrapper）
- Modify: `frontend/src/lib/i18n.ts`（加文案 key）
- Modify: `frontend/src/app/layout.tsx`（挂载组件）
- Modify: `frontend/package.json`（加 `html2canvas` 依赖）

**Interfaces:**
- Consumes: `POST /api/site-feedback`（Task 5）。
- Produces: 全局组件 `<FeedbackWidget />`；`submitSiteFeedback(payload)`。

- [ ] **Step 1: 安装依赖**

Run: `cd frontend && npm install html2canvas`
Expected: `package.json` 的 dependencies 出现 `html2canvas`。

- [ ] **Step 2: api.ts 加 wrapper**

在 `frontend/src/lib/api.ts` 末尾加：

```typescript
export interface SiteFeedbackPayload {
  message: string;
  page_url: string | null;
  screenshot: string | null;
  user_email: string | null;
}

export async function submitSiteFeedback(payload: SiteFeedbackPayload): Promise<{ status: string; image_sent: boolean }> {
  const resp = await fetch(`${API_BASE}/api/site-feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) throw new Error(`feedback failed: ${resp.status}`);
  return resp.json();
}
```

> 注：`API_BASE` 是 `api.ts` 内既有的常量（指向后端 / 经 rewrite）。若文件用的是别的名字（如 `BASE` 或直接相对 `/api`），按本文件既有写法对齐，不要新引入常量。

- [ ] **Step 3: i18n 加文案**

在 `frontend/src/lib/i18n.ts` 的中文→英文映射里加：

```typescript
  "反馈": "Feedback",
  "提交反馈": "Submit feedback",
  "描述你遇到的问题…": "Describe the issue you ran into…",
  "已自动截取当前屏幕": "Current screen captured automatically",
  "提交": "Submit",
  "取消": "Cancel",
  "反馈已发送，谢谢！": "Feedback sent, thank you!",
  "反馈发送失败": "Failed to send feedback",
```

- [ ] **Step 4: 写 FeedbackWidget 组件**

新建 `frontend/src/components/FeedbackWidget.tsx`：

```tsx
"use client";

import { useState, useCallback } from "react";
import html2canvas from "html2canvas";
import { useT } from "@/components/LangProvider";
import { useAuth } from "@/components/AuthProvider";
import { submitSiteFeedback } from "@/lib/api";

const GOLD = "#B8922E";

export default function FeedbackWidget() {
  const t = useT();
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [shot, setShot] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const capture = useCallback(async () => {
    try {
      const canvas = await html2canvas(document.body, { logging: false, useCORS: true });
      setShot(canvas.toDataURL("image/png"));
    } catch {
      setShot(null);
    }
  }, []);

  const openPanel = useCallback(async () => {
    await capture();      // 先截图（面板未渲染），再打开
    setOpen(true);
  }, [capture]);

  const submit = useCallback(async () => {
    if (!message.trim()) return;
    setSending(true);
    // 仅工单详情页（?detail=）附 URL，其它页面忽略
    const hasDetail = new URLSearchParams(window.location.search).has("detail");
    try {
      await submitSiteFeedback({
        message: message.trim(),
        page_url: hasDetail ? window.location.href : null,
        screenshot: shot,
        user_email: user?.email ?? null,
      });
      setToast(t("反馈已发送，谢谢！"));
      setMessage("");
      setOpen(false);
    } catch {
      setToast(t("反馈发送失败"));
    } finally {
      setSending(false);
      setTimeout(() => setToast(null), 3000);
    }
  }, [message, shot, user, t]);

  return (
    <>
      {!open && (
        <button
          onClick={openPanel}
          className="fixed bottom-6 right-6 z-50 rounded-full px-4 py-3 text-white shadow-lg text-sm font-medium"
          style={{ backgroundColor: GOLD }}
        >
          {t("反馈")}
        </button>
      )}

      {open && (
        <div className="fixed bottom-6 right-6 z-50 w-80 rounded-xl bg-white dark:bg-neutral-900 shadow-2xl border border-neutral-200 dark:border-neutral-700 p-4">
          <div className="text-sm font-semibold mb-2" style={{ color: GOLD }}>{t("提交反馈")}</div>
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder={t("描述你遇到的问题…")}
            rows={4}
            className="w-full rounded-md border border-neutral-300 dark:border-neutral-600 bg-transparent p-2 text-sm outline-none"
          />
          {shot && (
            <div className="mt-2 text-xs text-neutral-500">
              ✓ {t("已自动截取当前屏幕")}
              <img src={shot} alt="screenshot" className="mt-1 max-h-24 w-full object-cover rounded border border-neutral-200 dark:border-neutral-700" />
            </div>
          )}
          <div className="mt-3 flex justify-end gap-2">
            <button onClick={() => setOpen(false)} className="text-sm px-3 py-1.5 rounded-md text-neutral-500">{t("取消")}</button>
            <button
              onClick={submit}
              disabled={sending || !message.trim()}
              className="text-sm px-3 py-1.5 rounded-md text-white disabled:opacity-50"
              style={{ backgroundColor: GOLD }}
            >
              {t("提交")}
            </button>
          </div>
        </div>
      )}

      {toast && (
        <div className="fixed bottom-24 right-6 z-50 rounded-md bg-neutral-900 text-white text-sm px-4 py-2 shadow-lg">
          {toast}
        </div>
      )}
    </>
  );
}
```

> 注：`useAuth` 的导出名 / `user.email` 字段请对照 `frontend/src/components/AuthProvider.tsx` 实际签名调整；若 AuthProvider 暴露的是 `useAuth()` 之外的 hook 名或字段名不同，按实际改。若拿不到 email，传 `null` 即可（后端允许）。

- [ ] **Step 5: layout.tsx 挂载**

在 `frontend/src/app/layout.tsx`：import 区加 `import FeedbackWidget from "@/components/FeedbackWidget";`，并把组件放进 `<AuthGate>` 内、`</main>` 之后、`</div>` 之前（全局可见）：

```tsx
              <div className="flex h-screen">
                <Sidebar />
                <main className="flex-1 overflow-y-auto">
                  <PageTracker />
                  {children}
                </main>
                <FeedbackWidget />
              </div>
```

- [ ] **Step 6: 验证构建 + lint**

Run: `cd frontend && npm run lint && npm run build`
Expected: lint 通过、build 成功（无类型错误）。若 `useAuth`/字段名报错，按 Step 4 的注解对齐 AuthProvider 实际 API 后重跑。

- [ ] **Step 7: 手动验证（人工）**

本地起前后端：在普通页点反馈按钮 → 看到截图预览 → 填写提交 → 飞书收到文本（无工单 URL）+ 截图。再在 `/tracking?detail=<id>` 打开详情抽屉 → 提交 → 飞书文本含该 URL。

- [ ] **Step 8: 更新文档 + Commit**

在 `frontend/CLAUDE.md` 的「核心约定」表后补一句：全局 `FeedbackWidget`（右下角悬浮反馈，html2canvas 截图 + `/api/site-feedback`）挂在 `layout.tsx`。

```bash
git add frontend/src/components/FeedbackWidget.tsx frontend/src/lib/api.ts frontend/src/lib/i18n.ts frontend/src/app/layout.tsx frontend/package.json frontend/package-lock.json frontend/CLAUDE.md
git commit -m "feat(frontend): add global feedback widget with screenshot capture"
```

---

## Self-Review

**Spec coverage:**
- Part 1（API 按值周窗聚合两类工单 + 链接 + 附件）→ Task 1（窗口反查）+ Task 2（端点）。✅
- Part 2（skill：git 邮箱 → 拉单 → 先列清单 → AI 驱动 result.json 格式分析）→ Task 3。✅
- Part 3（全局反馈 widget：html2canvas 截图 + 工单 URL 仅详情页 + 飞书图片私聊）→ Task 4（飞书图片 helper）+ Task 5（端点 + 收件人配置）+ Task 6（前端 widget）。✅

**Placeholder scan:** 无 TBD/TODO；所有代码步骤含完整代码。两处「按实际 API 对齐」的注解（api.ts 的 `API_BASE` 常量名、AuthProvider 的 `useAuth`/`email`）是真实存在的不确定点，已显式标注为对齐动作而非占位。

**Type consistency:** `resolve_duty_week` 返回字段（group_index/week_num/week_start/week_end/is_current/partners）在 Task 2 中一致使用；`upload_image`/`send_image_message` 签名在 Task 4 定义、Task 5 调用一致；`submitSiteFeedback`/`SiteFeedbackPayload` 在 Task 6 定义并使用一致；后端 `SiteFeedbackInput` 字段（message/page_url/screenshot/user_email）与前端 payload 一致。

**已知执行期对齐点（非阻塞）：**
1. `frontend/src/lib/api.ts` 的后端 base 常量名 —— 按文件既有写法用，勿新引入。
2. `AuthProvider` 的 hook 名与用户 email 字段 —— 按实际签名调整，拿不到则传 null。
