# Oncall 页聚合飞书工单 — 设计 spec

日期：2026-06-16
分支：`worktree-oncall_tickets`（基于 main）

## 背景与目标

当前「正在处理的工单」分散两处：

- **网站 oncall 页**「升级工单」tab：经分析流程转交工程师、存入本地 DB（`escalated_at`）的工单。
- **飞书多维表**：客服直接在飞书里 `开始处理` 的工单，未走网站升级流程，本地 DB 可能无记录。

目标：在 oncall 页一处即可查看全部正在处理的工单。

## 需求

1. oncall 页新增独立「飞书工单」tab。
2. 范围：飞书侧 `in_progress`(进行中) + `pending`(待处理)，可分开筛选。
3. 工单按真实来源打标签：`feishu→飞书` / `linear→Linear` / `local→本地表单` / `api→API`。
4. 已完成工单要有醒目标记，一眼可辨。
5. 默认只展示待处理 + 处理中；已完成默认隐藏，可切换查看。

## 方案（A：专用 oncall 端点，只读直取）

后端新增 oncall 专用只读端点，直接复用飞书服务现成方法，不写库、不碰升级流程与分析 pipeline。

### 后端

- `backend/app/api/oncall.py`：新增
  `GET /api/oncall/feishu-tickets?status=open|done|all`
  - `open`（默认）= pending + in_progress
  - `done` = done
  - `all` = 全部
  - 调 `FeishuClient.list_issues_by_status`，返回 `Issue` 列表（含 `feishu_status`）。
- `backend/app/services/feishu_cli.py`：`Issue` 模型 + `parse_record` 增加 `assignee` 字段（读 `问题指派人`，目前只在 `filter_by_assignee` 用了未落字段），供卡片显示「谁在处理」。

### 前端

- `frontend/src/lib/api.ts`：新增 `getOncallFeishuTickets(status?)` wrapper + `FeishuTicket` 类型（含 `record_id/description/priority/assignee/feishu_status/feishu_link/zendesk_id/created_at_ms/source`）。
- `frontend/src/app/oncall/page.tsx`：
  - tab 切换器扩为 **「升级工单 | 飞书工单 | 周报」**。
  - 飞书 tab：平铺全部（不按 oncall 分组，目标是「看全部」），顶部 chip 切「待处理 / 处理中」；卡片显示来源标签、优先级、描述、指派人、创建时间、「去飞书」链接。
  - **共享来源标签**：feishu/linear/local/api → 中文标签，配色用站点 token。在升级工单 tab 的卡片上也渲染来源标签（数据已存在 `EscalatedTicket.source`）。
  - **默认隐藏已完成**：两个工单 tab 默认只显示待处理+处理中；加「显示已完成」开关。已完成卡片打醒目标记（绿色 ✓「已完成」角标 + 卡片降透明度）。
- `frontend/src/lib/i18n.ts`：补新文案中→英 key。

### 不改动

升级流程、分析 pipeline、排班/统计逻辑均不动。

## 来源标签映射

| source 值 | 标签 |
|-----------|------|
| `feishu` | 飞书 |
| `linear` | Linear |
| `local` | 本地表单 |
| `api` | API |
| 空/未知 | 飞书（默认） |

## 验证

- 后端：`GET /api/oncall/feishu-tickets` 返回 in_progress+pending，字段含 assignee/source/feishu_status。
- 前端：飞书 tab 渲染列表，待处理/处理中可切；升级 tab 显示来源标签；两 tab 默认隐藏已完成、开关可显示、已完成有醒目标记。
- `npm run lint` 通过。
