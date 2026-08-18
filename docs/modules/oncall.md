# Oncall 管理模块

值班排班、当前 oncall 查询、升级工单分配、值班统计。

## 后端

### 代码位置

| 文件 | 职责 |
|------|------|
| `backend/app/api/oncall.py` | API 端点（排班、当前、升级工单、统计） |
| `backend/app/api/users.py` | 用户 + 管理员 CRUD（oncall 候选池） |
| `backend/app/services/escalation_reminder.py` | 升级工单超时未处理提醒（cron 推送 oncall） |
| `backend/app/services/notify.py` | 通知发送总入口（封装 feishu 私聊 + 群） |
| `backend/app/services/feishu_cli.py` | Feishu 群 / 私聊 API 调用（escalation 通知具体实现） |

### API 端点

| Method | Path | 用途 |
|--------|------|------|
| `GET`  | `/api/oncall/current` | 当前值班人（按今天日期匹配 schedule） |
| `GET`  | `/api/oncall/schedule` | 排班表全量 |
| `PUT`  | `/api/oncall/schedule` | 更新排班（admin） |
| `GET`  | `/api/oncall/tickets` | 已转交工程师的升级工单列表 |
| `GET`  | `/api/oncall/stats` | 值班统计（人均工单数、平均处理时长） |
| `GET`  | `/api/oncall/my-workload` | 按邮箱反查最近值周窗，聚合 apollo 升级工单 + 飞书工单（含链接 + 附件），供 skill 拉取 |
| `PUT`  | `/api/oncall/tickets/{issue_id}/resolve` | 标记升级工单已解决 |

### 飞书值班表同步（已下线，2026-08-18）

**状态：默认关闭。** 排班真相源已改为 Jarvis 平台自身（`/oncall` 页面「排班管理」
Tab 编辑的 `oncall_groups` + `oncall_config.start_date`），不再从飞书值班表同步。

**临时恢复**（如需）：设环境变量 `ENABLE_ONCALL_FEISHU_SYNC=true` 后重启 backend。
代码、端点、测试均保留未删除，供需要时一行恢复。

**历史说明（功能已停用）：** 曾每周一 08:00 (Asia/Shanghai) 从飞书「本周值班」表
（`app_token=BmjmbSpxxabP2dsuxbtcUTYAn4g`）拉取「值班人员（Feature）」+「值班人员（Fundamentals）」
两个角色，去重合并邮箱后覆盖写入 Jarvis 的 `oncall_week_assignments` 排班快照表。
行为准则如下（若重启用则仍适用）：

- 飞书某周两个角色都为空 → 跳过，不清空 Jarvis 已有排班。
- 从未配置过 `start_date` → 整体跳过，不做任何写入。
- 检测到差异直接覆盖（不经人工确认）。
- 管理员可调 `POST /api/oncall/sync-from-feishu?username=<admin>` 手动跑一次。

### 排班数据模型

存储在 DB 里，按 (date, user_email) 组合查询当天值班人。`get_current_oncall()` 是 `services/feishu_cli.py` 在建升级群时调用的核心抓手。

### 升级群创建（与工单分析模块联动）

工单分析模块的 escalation 流程会调 `feishu_cli.py::create_followup_group()`：

1. 拉当前 oncall 邮箱列表（`db_mod.get_current_oncall()`）
2. 合并固定成员 + 触发用户 → 全员
3. 飞书 API 建群 + 邀成员
4. **群消息模版统一英文**（已对齐 PR：`backend/app/services/feishu_cli.py:822-851`）
5. 拿不到 group invite 时 fallback 私聊通知

### 提醒机制

`services/escalation_reminder.py` 周期跑（超时未 resolve 的升级工单）→ 飞书私聊 oncall 催办。

## 前端

### 页面入口

- `/oncall` 是唯一入口（`frontend/src/app/oncall/page.tsx`）

### 三个 Tab

| Tab | 内容 | API |
|-----|------|-----|
| 当前值班 | 今天 oncall 是谁 + 联系方式 | `GET /api/oncall/current` |
| 排班管理 | 月历视图，admin 可编辑 | `GET/PUT /api/oncall/schedule` |
| 升级工单 | 已转交工程师未 resolved 的工单列表 | `GET /api/oncall/tickets` + `PUT .../resolve` |

### 约定

- 排班编辑权限通过 `useUserRole()` 拉 `/api/users/me` 判断，非 admin 隐藏编辑入口
- 升级工单卡片点击跳到 `/tracking?detail=<issue_id>`（深链复用工单分析模块的详情抽屉）
- 统计图表用站点统一金调 `#B8922E`，不引第三方图表色板
