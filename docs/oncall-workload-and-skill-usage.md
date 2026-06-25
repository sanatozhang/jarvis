# 值周工单 API + 分析 Skill 使用文档

面向值周（oncall）同学：用一个 API 拉取你本周需要人工处理的工单，再用一个 skill 让 AI 自动分析日志和代码、产出报告。

- 设计与实现细节见 `docs/superpowers/specs/2026-06-25-oncall-workload-api-and-skill-design.md` 与 `docs/superpowers/plans/2026-06-25-oncall-workload-and-feedback-widget.md`。
- 相关模块文档：`docs/modules/oncall.md`、`docs/modules/ticket-analysis.md`。

---

## 一、API：`GET /api/oncall/my-workload`

按 oncall 同学邮箱，反查 ta **最近一次值周**的时间窗，聚合该窗口内**仍需人工处理**的两类工单：apollo 升级工单（转交工程师）+ 飞书工单。只读，不写库。

### 请求

```
GET {API_BASE}/api/oncall/my-workload?email=<你的邮箱>
```

| 环境 | API_BASE |
|------|----------|
| 生产（102） | `http://10.0.52.102:8000` |
| 本地开发 | `http://localhost:8000` |

| 参数 | 说明 |
|------|------|
| `email` | 必填。oncall 同学的邮箱。大小写不敏感，按排班组成员匹配（一组通常 2 人，传任一人的邮箱即可）。 |

curl 示例：

```bash
curl -s "http://10.0.52.102:8000/api/oncall/my-workload?email=sanato.zhang@plaud.ai" | jq
```

### 值周窗口怎么定

- 用排班表（`/api/oncall/schedule` 的 `groups` + `start_date`）反查邮箱所在组。
- 取该组**最近一次值周**那一周 `[week_start, week_end]`（含本周；若本周不是 ta 值周则回溯到上一次）。
- 两类工单都限定在这个窗口内：apollo 按 `escalated_at`、飞书按工单创建时间。

### 响应

```jsonc
{
  "email": "sanato.zhang@plaud.ai",
  "duty_week": {
    "week_num": 19,
    "week_start": "2026-06-22",
    "week_end": "2026-06-28",
    "is_current": true            // 是否就是本周
  },
  "oncall_partners": ["bob@plaud.ai"],   // 同组其他成员
  "apollo_tickets": [
    {
      "record_id": "rec_xxx",
      "description": "用户反馈蓝牙频繁断连",
      "problem_type": "蓝牙连接",
      "root_cause": "...",         // 已有分析结论（可能为空）
      "confidence": "high",
      "zendesk_id": "#378794",
      "zendesk_url": "https://.../378794",   // 绝对地址
      "escalated_at": "2026-06-23T10:00:00Z",
      "escalated_by": "alice@plaud.ai",
      "escalation_status": "in_progress",
      "escalation_share_link": "https://...",  // 飞书升级群分享链接
      "apollo_url": "http://10.0.52.102:3000/tracking?detail=rec_xxx",  // 绝对地址，深链到详情
      "logs_download_url": "/api/local/rec_xxx/download-logs"           // 相对路径
    }
  ],
  "feishu_tickets": [
    {
      "record_id": "rec_yyy",
      "description": "无法连接设备",
      "priority": "H",
      "device_sn": "SN123",
      "firmware": "1.2.3",
      "app_version": "4.5.6",
      "assignee": "张三, 李四",
      "assignee_emails": ["zhangsan@plaud.ai", "lisi@plaud.ai"],
      "feishu_link": "https://feishu.cn/base/...",   // 绝对地址
      "zendesk": "https://...",
      "zendesk_id": "#379000",
      "feishu_status": "in_progress",
      "created_at_ms": 1782172800000,
      "attachments": [
        { "name": "log.plaud", "size": 12345, "download_path": "/api/local/rec_yyy/files/log.plaud" }
      ]
    }
  ],
  "summary": { "apollo_count": 1, "feishu_count": 1, "total": 2, "with_attachments": 2 }
}
```

### 链接 / 附件下载约定

- `feishu_link` / `zendesk_url` / `apollo_url` 是**绝对地址**，直接打开。
- `logs_download_url` 与 `attachments[].download_path` 是**相对路径**，需要拼 `API_BASE` 再下载：

  ```bash
  # apollo 工单日志（单文件直传 / 多文件 zip）
  curl -L "http://10.0.52.102:8000/api/local/rec_xxx/download-logs" -o logs.zip

  # 飞书工单附件（本地无缓存时会按需从飞书拉）
  curl -L "http://10.0.52.102:8000/api/local/rec_yyy/files/log.plaud" -o log.plaud
  ```

- 飞书日志多为加密的 `.plaud`，下载后需解密（见 skill 第 4 步 / `plaud-log-decrypt`）。

### 错误

| 状态码 | 含义 |
|--------|------|
| `404` | 该邮箱不在任何排班组，或未配置排班 |
| `200` + 空数组 | 邮箱有效但该值周窗内没有待处理工单 |

---

## 二、Skill：`oncall-ticket-analysis`

让 AI 按你的 git 邮箱自动拉单、探索日志和代码、逐单产出分析报告。

### 安装位置

`~/Desktop/code/myskill/SKILL.md`（独立于 jarvis 仓库）。在能加载该 skill 的 Claude Code 会话里即可触发。

### 前置

- `git`、`curl` 可用，且当前仓库 `git config user.email` 是你的 oncall 邮箱。
- 默认在 jarvis 仓库目录内触发（AI 需要探索 `backend/app/...` 和 `backend/rules/*.md`）。
- 解密依赖 `plaud-log-decrypt` skill。

### 触发

任一即可：`分析我的值周工单` / `oncall 工单分析` / `/oncall-ticket-analysis`。
默认 `API_BASE=http://10.0.52.102:8000`；要改本地时直接说「用 http://localhost:8000」。

### 它会做什么

1. 读 `git config user.email`（取不到会让你手动给）。
2. 调 `my-workload` API 拉单。
3. **先列清单**（每次必做）：值周窗、同组搭档、工单总数，逐条一行 `[apollo|feishu] <id> — 一句话描述 — 附件:有/无`。
4. **逐单详析**：下载附件 → 解密 `.plaud` → 结合日志 + jarvis 代码 + 规则定位根因 → 按 `result.json` 契约产出报告段（`problem_type` / `root_cause` / `confidence` / `key_evidence` ≤5 条 / `user_reply` / `needs_engineer` / `fix_suggestion`）+ 工单链接。
5. **最终输出** = 清单 + 每单报告。

### 约束

- 只读：只 GET API + 下载附件，不写库、不触发后端分析、不 commit/push。
- 证据优先：每条根因必须有日志行支撑；找不到日志就如实说明，不臆测。

---

## 常见问题

- **API 返回 404？** 确认邮箱在 `/api/oncall/schedule` 的某个组里，且排班已配 `start_date`。
- **skill 拉到 0 个工单？** 可能本周不是你值周，或窗口内确实没有待处理单（看返回的 `duty_week.is_current`）。
- **下载附件 404？** apollo 工单可能没有缓存日志；飞书附件名需与 `attachments[].name` 完全一致。
- **API_BASE 用 3000 还是 8000？** 数据 API 在后端 `8000`；`3000` 是前端页面（`apollo_url`/`feishu_link` 这类绝对地址才指向页面）。
