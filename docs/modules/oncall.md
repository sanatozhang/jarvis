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

### 每周值周提醒（`services/oncall_weekly_greeting.py`）

**状态：默认关闭**（`ENABLE_ONCALL_WEEKLY_GREETING`，默认 `false`）。每周一 09:00
(Asia/Shanghai) 往固定飞书群「APP Team」（`chat_id = oc_517fdd3067d8dfa90f8d97d4ae6fe5c0`，
配置在 `FeishuSettings.oncall_greeting_chat_id`）发一条中英双语消息，`@` 出本周值班
同学并附 `/oncall` 看板链接——解决"排班表要主动去翻才知道，值班同学常常不知道自己
在值周"的问题。

**时序**：飞书排班同步（上一节）原来跑在每周一 08:00，值周提醒跑在 09:00，比它晚一小时——
历史上这个顺序保证提醒读到的是刚同步过的最新排班。**同步现已下线**，提醒现在直接读
Jarvis 平台自身的排班（`/oncall` 页面「排班管理」写的 `oncall_groups` +
`oncall_config.start_date`），跟同步没有直接依赖了；如果同步以后被重新打开
（设 `ENABLE_ONCALL_FEISHU_SYNC=true`），这个"晚一小时"的时序关系仍然成立，
值周提醒依然会读到当次同步刚写入的排班。

**跳过原因清单**（`send_weekly_greeting()` 返回 `{"skipped": true, "reason": ...}`）：

| `reason` | 触发条件 | 是否私聊通知管理员 |
|---|---|---|
| `no_start_date` | `oncall_config.start_date` 未配置 | 是（非 dry_run 且非 to_email 时） |
| `no_oncall_members` | 当前值班组解析为空（`db.get_current_oncall()` 返回 `[]`） | 是（同上） |
| `already_sent_this_week` | 幂等标记显示本周已发过（见下方 marker） | 否 |
| `no_chat_id` | `oncall_greeting_chat_id` 为空且未传 `to_email` | 否 |
| `send_failed` | 有界重试（默认 3 次，间隔 5 分钟）全部失败 | 是（见下方，不受 dry_run/to_email 的通知逻辑约束——这条路径本身就已经排除了 dry_run 和 to_email） |

`no_start_date` / `no_oncall_members` 是配置断供，排班已是唯一真相源之后"静默 skip"
代价很高——群里一周没消息不会有任何人发现，所以要主动私聊管理员
（收件人 `get_settings().feedback_recipient`）。`send_failed` 同理：重试耗尽后
也会 `logger.error` + 私聊管理员"这周群里没人收到提醒"，因为发送失败和配置断供
对用户的影响是一样的——都是"这周没人被提醒"。

**为什么 `ENABLE_ONCALL_WEEKLY_GREETING` 永久默认 `false`**（与上面
`ENABLE_ONCALL_FEISHU_SYNC` 的"默认关、临时恢复用"模式不同）：这不是一个"评审后
改成 true"的过渡态开关，而是刻意长期保持默认关闭、只在生产服务器的 `.env`
里显式打开。原因是 `.env` 不进 git——笔记本 clone 出来的仓库、或任何独立起
的实例，默认状态下都**不会**往群里发消息，这是对"野实例重复发送"最有效的防线
（比 DB 里的幂等标记强得多，标记只挡得住共享同一个 SQLite 文件的场景）。

**手动触发端点** `POST /api/oncall/weekly-greeting?username=<admin>`（admin only，
403 拒绝非 admin）：

| 参数 | 默认 | 效果 |
|---|---|---|
| `dry_run` | `true` | 只渲染文案、不发消息、不写幂等标记；生产上可随便点，是端点自己的安全闸 |
| `to_email` | `""` | 真发一条，但只发到这个人的私聊做验证，不发群、不写标记 |
| `force` | `false` | 忽略"本周已发过"的幂等守卫 |

端点内部固定传 `max_attempts=1`，避免一次发送失败把 HTTP 请求挂住 5-15 分钟重试。

**幂等标记**：`oncall_config` 表里的 kv 键 `weekly_greeting_last_sent_week`
（值是本周周一的 `YYYY-MM-DD`），只在**真实群发**（非 `dry_run`、非 `to_email`）
成功后才写。它的能力边界（写在代码注释里，不要自我欺骗）：

- 能防住：手动验证端点跟自动 Monday 任务撞车、同一进程/同一 SQLite 文件内的多进程竞争。
- 防不住：① 使用独立 SQLite 文件的"野实例"——各写各的标记，照样会双发，真正的防线是
  上面的开关默认 `false`；② check-then-set 不是原子操作，同毫秒并发有极小概率都通过
  "已发过？"检查；③ 发送成功但写标记前进程被杀——这周会漏发一次（这个取舍是刻意的：
  "宁可少发一次"好过"往全群重复骚扰"）。

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
