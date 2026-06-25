# Oncall 值周工单 API + 分析 Skill + 全局反馈入口 设计

**日期**: 2026-06-25
**作者**: sanato
**状态**: 已批准（设计）

## 背景与目标

值周（oncall）同学每周需要手动处理两类工单:
1. **apollo 升级工单** —— 工单分析后转交工程师的升级单（`IssueRecord.escalated_at`）。
2. **飞书工单** —— 直接在飞书多维表里处理的工单（`Issue` 模型，指派给 oncall）。

目标:
1. 暴露一个 API，输入 oncall 同学的邮箱，返回 **ta 最近一次值周窗口内、仍需人工处理** 的两类工单，每条含:工单链接（apollo + 飞书）、问题描述、附件（日志/截图）。
2. 做一个 skill，按当前用户 git 邮箱调用该 API 拉取工单，先列清单（数量 + 附件有无），再让 AI 自由探索日志和代码、按项目工单分析输出格式（`result.json` 契约）逐单产出分析报告。

## Part 1 — 后端 API

### 端点

```
GET /api/oncall/my-workload?email=<email>
```

新增在 `backend/app/api/oncall.py`，单一端点合并两类工单。无需鉴权（与同模块其他读端点一致，内网使用）。

### 值周窗口解析（核心逻辑）

1. 载入 `db.get_oncall_groups()` + `db.get_oncall_config("start_date")`。
2. 把 `email` 转小写，在各组 `members` 里做**成员匹配**，定位所在组 `group_index`。
   - 若 email 不在任何组 → 返回 `404`，body `{"detail": "<email> is not an oncall member"}`。
   - 若无 schedule（groups 为空 / 无 start_date）→ 返回 `404`，提示未配置排班。
3. `current_week_num = (today - start_date).days // 7`（下限 0）。
4. ta 的**最近一次值周** `duty_week_num` = 满足以下条件的最大 `week_num`:
   - `week_num % len(groups) == group_index`
   - `week_num <= current_week_num`
   - 即:本周若是 ta 值周则取本周，否则回溯到 ta 上一次值周。
5. 窗口:
   - `week_start = start_date + timedelta(weeks=duty_week_num)`
   - `week_end = week_start + timedelta(days=6)`
   - `is_current = (duty_week_num == current_week_num)`

### apollo 升级工单

- 数据源:`db.get_escalated_issues(status=None)`，在端点内过滤:
  - `escalation_status != "resolved"`（仍需处理）
  - `escalated_at` 落在 `[week_start, week_end]` 内（按日期，含端点）。
- 每条字段:
  ```jsonc
  {
    "record_id", "description",
    "problem_type", "root_cause", "confidence",
    "zendesk_id", "zendesk_url",            // zendesk_url 由 zendesk_id 拼出（若有）
    "escalated_at", "escalated_by", "escalation_status",
    "escalation_share_link",                // 飞书升级群分享链接
    "apollo_url",                           // 绝对: <FRONTEND_BASE>/tracking?detail=<record_id>
    "logs_download_url"                     // 相对: /api/local/<record_id>/download-logs
  }
  ```
- 附件:apollo 升级单的日志缓存在 workspaces，统一用现成的 `GET /api/local/<id>/download-logs`（单文件直传 / 多文件 zip）。不再单独枚举文件名。

### 飞书工单

- 数据源:`FeishuClient().list_issues_by_status("pending"/"in_progress", assignee_emails=[email])`，合并后在端点内过滤:
  - `created_at_ms` 落在 `[week_start, week_end]` 内（贴合"值周时间"口径；窗口前创建的老 open 单不返回，已与用户确认接受）。
- 每条字段（来自 `Issue.model_dump`）:
  ```jsonc
  {
    "record_id", "description", "priority",
    "device_sn", "firmware", "app_version",
    "assignee", "assignee_emails",
    "feishu_link",                          // 绝对，Issue 自带
    "zendesk", "zendesk_id",
    "feishu_status", "created_at_ms",
    "attachments": [
      { "name", "size", "download_path" }   // 相对: /api/local/<record_id>/files/<name>
    ]
  }
  ```
- 附件来自 `Issue.log_files`（已合并飞书表「日志文件」+「其他附件」，即日志 + 截图）。`serve_issue_file` 端点本地无文件时会按需从飞书下载，故 `download_path` 始终可用。

### 返回结构

```jsonc
{
  "email": "alice@plaud.ai",
  "duty_week": { "week_num": 19, "week_start": "2026-06-22", "week_end": "2026-06-28", "is_current": true },
  "oncall_partners": ["bob@plaud.ai"],     // 同组其他成员
  "apollo_tickets": [ ... ],
  "feishu_tickets": [ ... ],
  "summary": { "apollo_count": 2, "feishu_count": 3, "total": 5, "with_attachments": 4 }
}
```

### 链接策略

- 附件链接用**相对路径**（`download_path` / `logs_download_url`），skill 用它调用 API 时的 base 拼成全 URL，规避内外网 host 差异。
- `feishu_link` / `zendesk_url` / `apollo_url` 用**绝对地址**。`FRONTEND_BASE` 默认 `http://10.0.52.102:3000`，从 settings 读（若无则用该默认）。

### 文档更新

- `docs/modules/oncall.md` 的「API 端点」表新增本端点。
- `backend/CLAUDE.md` 路由总览无需改（已含 `/api/oncall`）。

## Part 2 — 分析 Skill

位置:`~/Desktop/code/myskill/`（一个 `SKILL.md` + 可选辅助脚本）。建议 skill 名 `oncall-ticket-analysis`。

### 触发后流程

1. **取邮箱**:`git config user.email`。取不到则提示用户手动传入。
2. **拉工单**:`GET {API_BASE}/api/oncall/my-workload?email=<email>`。
   - `API_BASE` 默认 `http://10.0.52.102:8000`，允许用户在触发时覆盖。
3. **先列清单**（每次触发必做）:
   - 打印值周窗 `[week_start, week_end]`、同组搭档、`summary.total`。
   - 逐条:`[apollo|feishu] <record_id> — <一句话描述> — 附件:有/无`。
4. **再逐单详析**:对每个工单:
   - 下载附件:飞书用 `download_path`，apollo 用 `logs_download_url`（拼 `API_BASE`）。
   - 加密 `.plaud` 日志 → 调用 `plaud-log-decrypt` skill 解密（或 `backend/app/services/decrypt.py`）。
   - 自由探索 jarvis 代码库（默认 cwd，假定 skill 在 jarvis repo 里触发）+ `backend/rules/*.md` 参考规则。
   - 按 **result.json 契约**产出报告段:
     `problem_type` / `root_cause` / `confidence` / `key_evidence`(≤5 条日志行) / `user_reply` / `needs_engineer` / `fix_suggestion`。
   - 可选:按问题类型路由到现成的 `analyze-*` skill 作为分析模板辅助。
5. **最终输出** = 概览清单 + 每单详细 markdown 报告。

### 设计约束

- skill **只读**调用 API + 现成下载端点，不向生产 DB 写任何测试数据。
- skill 不自动 commit / push / 部署。
- 代码库探索路径默认 cwd；若 cwd 非 jarvis repo，提示用户指定路径。

## Part 3 — 全局反馈悬浮入口

给所有页面加一个右下角悬浮反馈按钮，用户填写反馈 + 自动截图，提交后通过飞书私聊发给管理员（`sanato.zhang@plaud.ai`）。

### 前端

- 新组件 `frontend/src/components/FeedbackWidget.tsx`，挂载在 `src/app/layout.tsx`（全局，所有页面可见）。
- **悬浮按钮**:`position: fixed` 右下角，站点统一金调（`#B8922E`）。
- **点击展开面板**:
  - 多行 `textarea`（反馈内容，必填）。
  - 提示「已自动截取当前屏幕」+ 截图缩略图预览。
  - 提交 / 取消按钮。
- **截图**:用 **html2canvas**（新增依赖）渲染当前页面 `document.body` → PNG dataURL。在打开面板时（或提交时）截取，确保截到的是反馈面板弹出前的页面（实现上:先截图再渲染面板，或截图时隐藏面板）。
- **工单 URL 判定**:仅当当前 URL 含 `?detail=<id>` 查询参数（`/` 与 `/tracking` 的工单详情深链约定）时，`page_url = window.location.href`;其他页面 `page_url = null`（忽略 URL）。
- **提交**:`POST /api/site-feedback`,body `{ message, screenshot(base64 PNG), page_url, user_email }`。`user_email` 取自 `AuthProvider`(若已登录)。
- 成功 / 失败用现有 `Toast.tsx` 提示;i18n 走 `useT()`。

### 后端

- 新 router `backend/app/api/site_feedback.py`,挂载 `prefix="/api/site-feedback"`(与既有 `/api/feedback` 本地工单端点区分,避免语义混淆)。
- 端点 `POST /api/site-feedback`,入参:
  ```jsonc
  { "message": "...",        // 必填
    "page_url": "..." | null,
    "screenshot": "data:image/png;base64,..." | null,
    "user_email": "..." | null }
  ```
- 处理:
  1. 若有 `screenshot` → 解 base64 → 调新增 `feishu_cli.upload_image(png_bytes) -> image_key`(`POST /im/v1/images`,`image_type=message`)。
  2. 组装文本消息:反馈内容 + 提交人(`user_email`)+ 工单 URL(若有)+ 时间戳。
  3. `send_message(email=FEEDBACK_RECIPIENT, text=...)` 发文本;若有 `image_key`,再发一条图片消息 → 需新增 `feishu_cli.send_image_message(email, image_key)`(或给 `send_message` 加 `image_key` 入参,`msg_type="image"`,`content={"image_key": ...}`）。
  4. 返回 `{"status": "sent", "image_sent": bool}`;飞书发送失败返回 `502` 但不阻塞（记录日志）。
- **收件人配置**:`FEEDBACK_RECIPIENT`,默认 `sanato.zhang@plaud.ai`,从 settings/config 读，便于后续改人。

### 飞书能力补充（新增）

现有 `feishu_cli.send_message` 只支持 text/markdown。需新增:
- `upload_image(image_bytes: bytes) -> str`:上传图片到飞书拿 `image_key`。
- 图片消息发送:扩展 `send_message` 支持 `image_key` 或新增 `send_image_message`。

### 文档更新

- `backend/CLAUDE.md` 路由总览新增 `/api/site-feedback`。
- `frontend/CLAUDE.md` 记录全局 `FeedbackWidget` 挂载点 + html2canvas 依赖。

## 非目标（YAGNI）

- 不做鉴权 / 限流（内网）。
- 不改前端 `/oncall` 页面（Part 1/2 只加后端 API + skill）。
- 不持久化 skill 分析结果到 DB（输出留在会话/文件）。
- 不替换现有 `/api/oncall/tickets`、`/api/oncall/feishu-tickets` 端点（新端点是面向 skill 的合并视图，旧端点继续服务前端）。
- 反馈 widget 不落库（只转发飞书）；不做反馈历史列表 / 后台管理页。
- 不支持用户手动框选截图区域（默认全屏 DOM 截图）。

## 测试策略

- 后端:`pytest` 针对值周窗反查逻辑做单测（给定 groups + start_date + email + 模拟 today，断言 `duty_week_num` / 窗口边界）；apollo/飞书过滤用 mock 数据断言窗口内外筛选正确。
- 手动:对真实 oncall 邮箱调用端点，核对返回数量与 `/oncall` 页面一致（只读）。
- skill:用一个真实值周邮箱端到端跑一次，确认清单 + 至少一单完整分析。
- 反馈 widget:本地端到端——在工单详情页与普通页各提交一次，确认飞书私聊收到文本（工单页带 URL、普通页不带）+ 截图图片消息；后端对飞书失败的降级（502 不阻塞）有日志。
```
