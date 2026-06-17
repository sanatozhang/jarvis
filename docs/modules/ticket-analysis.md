# 工单分析模块

把用户工单（Feishu / 本地表单 / Linear）拉进来 → 下载并解码日志 → 匹配规则 → AI agent 结合日志分析 → 出结构化结果给前端展示。

## 工单来源（按 ID 前缀路由）

| 前缀 | 来源 | 入口 |
|------|------|------|
| (无) | Feishu 多维表 | Feishu API 拉取 |
| `fb_` | 本地表单 | `/feedback` 页面提交 |
| `lin_` | Linear webhook | `/api/linear` + 评论里 `@ai-agent` 触发 |

## 后端

### 分析 Pipeline（核心闭环）

入口：`backend/app/workers/analysis_worker.py::run_analysis_pipeline()`

```
1. Fetch issue
   ├─ Feishu 多维表：services/feishu.py / services/feishu_cli.py
   ├─ 本地（fb_）：api/feedback.py + 本地 DB
   └─ Linear（lin_）：api/linear_webhook.py + services/linear.py

2. Download logs
   ├─ Feishu 附件下载 → workspaces/_cache/{issue_id}/
   └─ Zendesk 关联（如 Zendesk ID 存在）：services/zendesk.py

3. Decrypt
   └─ .plaud 私有格式：services/decrypt.py

4. Match rules
   └─ services/rule_engine.py 匹配 issue 描述 vs backend/rules/*.md（YAML frontmatter）

5. Pre-extract
   └─ 命中规则里的 grep 模式预跑日志（L1 抽取），减少 agent 输入颗粒度

6. Build workspace
   └─ workspaces/{task_id}/  子目录 raw/ logs/ rules/ code/ output/

7. Run agent
   ├─ services/agent_orchestrator.py 按 routing 选 agent
   ├─ agents/claude_code.py（Claude CLI 子进程）/ agents/codex.py（Codex CLI 子进程）
   └─ agent 写 output/result.json

8. Parse result
   └─ agents/base.py::BaseAgent.parse_result()，JSON 失败回落 stdout 文本解析
```

### Agent 抽象

- `agents/base.py::BaseAgent` — ABC：`analyze()` + 静态 `build_prompt()` / `parse_result()`
- `agents/claude_code.py` — 跑 `claude` CLI
- `agents/codex.py` — 跑 `codex` CLI
- `services/agent_orchestrator.py` — 按 `config.yaml::agent.routing[problem_type]` 选 agent，落 fallback 到 `agent.default`

### Agent 输出契约（必填字段）

Agent 必须写 `output/result.json` 包含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `problem_type` / `problem_type_en` | str | 问题分类（中 / 英） |
| `root_cause` / `root_cause_en` | str | 根因（中 / 英） |
| `confidence` | `"high" \| "medium" \| "low"` | 置信度 |
| `key_evidence` | list[str] | 关键日志行（最多 5 条） |
| `user_reply` / `user_reply_en` | str | 客服回复模版 |
| `needs_engineer` | bool | 是否需转工程师 |
| `fix_suggestion` | str | 修复建议 |

### 规则系统（热加载）

- 位置：`backend/rules/` 下 Markdown 文件，YAML frontmatter 描述 keywords / regex / grep patterns
- `services/rule_engine.py` 启动时全量同步进 DB，匹配时直接走 DB
- 热加载：`curl -X POST http://localhost:8000/api/rules/reload`（无需重启）

### API 主要端点

- `POST /api/issues/{id}/analyze` — 触发分析
- `GET  /api/issues` — 工单列表 / 检索
- `GET  /api/tasks/{task_id}` + SSE `/api/tasks/{task_id}/events` — 任务进度
- `GET  /api/feedback` / `POST /api/feedback` — 本地工单
- `POST /api/linear` — Linear webhook 入口
- `GET /POST /PUT /DELETE /api/rules/*` — 规则 CRUD
- `POST /api/rules/reload` — 规则热加载
- `GET  /api/reports/*` — 报表导出

### 工单升级（escalation）

工单转交工程师时通过 `services/feishu_cli.py::create_followup_group()` 建飞书群 + 添加 oncall 成员 + 推送通知。**群消息模版统一使用英文**（避免中英混排），DM 兜底通知同样英化。模版函数：`create_followup_group()` + `notify_oncall()`。

## 前端

### 页面入口

| 路径 | 用途 | 关键交互 |
|------|------|---------|
| `/` (`app/page.tsx`) | 主分析入口：粘 / 选工单 → 触发分析 → 实时结果展示 | SSE 订阅进度，结果包含 root cause / evidence / fix suggestion / 回复模版 |
| `/tracking` | 工单追踪列表 | 支持 `?detail=<issue_id>` 深链开抽屉；状态筛选、来源筛选 |
| `/feedback` | 本地工单提交（`fb_` 前缀） | 表单 → POST /api/feedback |
| `/rules` | 规则 CRUD | 编辑后调 `/api/rules/reload` 热生效 |
| `/reports` | 历史报表 | 按时间 / 类型筛选 |

### ⚠️ 详情面板有两份代码，改一处必同步另一处

工单详情面板（右侧停靠 35% 分栏，点列表切换 / Esc 关闭 / 选中行金色高亮）在 `/`（`app/page.tsx`，
状态 `detailId`+`detailData`）和 `/tracking`（`app/tracking/page.tsx`，状态 `detailItem`）各写了一份、
相互独立。**改面板的展示或交互两处都要改**，否则会出现「一边改了另一边没变」的反复坑。共享的只有
`globals.css` 的 `panel-slide-in` 动画与 `IssueComponents.tsx` 的 `S` 配色。（TODO：抽共享组件消除重复。）

### 关键约定

- API 调用全部走 `src/lib/api.ts`：核心 wrapper 是 `analyzeIssue()`、`subscribeTaskProgress()`（SSE）、`fetchTrackingList()`、`fetchRules()`、`reloadRules()`
- SSE 用 `subscribeTaskProgress(taskId, onEvent)`，回调里更新进度状态；任务完成自动 close
- 工单 ID 显示要带前缀（`fb_xxx` / `lin_xxx` / 空），用户复制粘贴需要 ID 完整
- 状态色：open=红 / investigating=黄 / resolved=绿 / ignored,wontfix=灰
- i18n 全部走 `useT()`，中文 key + `i18n.ts` 英译

### 工单升级（前端触发）

`/tracking` 详情抽屉里有「转交工程师」按钮 → 调后端 escalate 端点 → 飞书自动建群 + 推消息（英文模版，见后端「工单升级」段）。
