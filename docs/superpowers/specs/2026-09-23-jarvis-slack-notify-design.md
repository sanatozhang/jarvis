# jarvis 通知层迁移 Slack（与飞书并存、按模块可切、预定下线）

> 状态：设计已定，实施中。
> 姊妹项目 Apollo 已完成同类迁移（`Plaud-AI/Apollo` PR #103），本设计**刻意不照搬**
> 它的形态——两边的飞书用法根本不同，见「与 Apollo 的差异」。

## Context

jarvis 的通知 100% 绑死飞书，且**以交互式卡片为主**：`send_interactive_card`
28 处 + `send_message` 21 处，背后是 **12 个 card builder / ~2300 行**飞书卡片
构造代码，分布在三个模块：

| 模块 | builder 数 | 行数 | 卡片 |
|---|---|---|---|
| crashguard | 9 | 1200 | daily / hourly_alert / core_metric_alert / fatal_backlog / job_health / symbol_health / pending_review / reviewer / fallback |
| coreguard | 2 | 413 | summary / demo |
| graygate | 1 | 592 | report |

### 与 Apollo 的差异（为什么不照搬）

| | Apollo | jarvis |
|---|---|---|
| 飞书用法 | 纯文本 + 建群 | 交互式卡片 |
| 会话形态 | 一张工单一个群/thread，**有状态** | fire-and-forget 告警，**无状态** |
| 粘性需求 | 必须有（`escalation_provider` 存在工单上） | **不需要**——没有"后续消息要发回原会话"这回事 |
| 契约层 | `OncallDispatcher` 已经渠道中立 | 无抽象，`notify.py` 是 15 行 re-export shim |

所以本设计**不引入** `escalation_provider` 那套粘性机制。

### 三个关键事实（调研得出，决定了工作量）

1. **卡片按钮全是 `open_url`。** 全仓唯一的 `behaviors` 在
   `crashguard/services/feishu_card.py:663`，type 是 `open_url`。
   `feishu_admin_open_ids`（注释写着「一键 PR approve 白名单」）是**死配置**——
   读进 settings 之后全仓零使用。
   ⇒ **不需要 interactivity / HMAC 回调端点 / `SSO_EXEMPT_PATHS` 放行 / 新 scope。**
2. **`feishu_message_id` 不是消息 id，是跨实例发送锁。** 存的是 `"locking"` /
   `"sent"` 哨兵（`daily_report.py:2290-2345`，`LOCK_TTL_MIN=10` 分钟接管孤儿锁），
   挂在 crashguard 三张表上，且**前端在读**（`crashguard/reports/page.tsx:262`
   用它显示「已发送」）。语义与渠道无关。⇒ **不改名、不 provider 化。**
3. **三级目标回落链重复了 6 遍。** `alert_email → target_chat_id → target_email`
   在 `job_health_alerter.py`（×2）/ `core_metric_alerter.py` / `hourly_alerter.py` /
   `symbol_coverage_monitor.py` / `daily_report.py` 各写一遍，coreguard 还在其上
   叠了群配额溢出（`feishu_group_daily_quota` → `overflow_email`）。
   ⇒ 任何一处漏改 = 某类告警切了渠道、另一类没切。**收口成一个 resolver 是
   这次改动与 Slack 无关的净收益。**

## 已定的四个决定

1. **范围：只迁通知层。** 飞书 SSO 登录（`auth_feishu.py` + `api/auth.py` 4 个端点）
   和多维表 oncall 同步（`oncall_feishu_sync.py`）**不动**。jarvis 没有 Google SSO，
   删了飞书登录就没人能登录。
2. **形态：模块粒度可切 + 预定下线。** crashguard / coreguard / graygate
   各自一个 `notify.provider`，飞书代码逐字节不动；某模块在 Slack 上跑满
   **2 周**且无回退，就删该模块的飞书 builder 与配置字段。
3. **折叠层：Slack thread。** 飞书 `collapsible_panel` → 主消息（TL;DR + 必看区
   + 按钮）+ 每个折叠段一条 thread 回复。层次同构，且不需要 interactivity。
4. **Slack app：复用 Apollo Notify。** 见下。

## Slack app：复用 Apollo Notify

Team `T05DCGBSHN2`（plaud），bot `apollo_agent` / `U0C3FBW0NKY`。
已授权 scope：`chat:write, groups:write, im:write, users:read, users:read.email,
files:write, usergroups:write` ——**对 jarvis 够用，不需要新审批**（因为按钮全是 URL）。

> 曾创建过独立 app「Jarvis Notify」（App ID `A0C34498HRV`），卡在工作区新增的
> 「管理员批准才能安装」策略上，未安装。改为复用 Apollo Notify。

三个必须接受的后果：

1. **署名是「Apollo Notify」。** 改 per-message 发送者名要 `chat:write.customize`，
   我们没有。由频道和卡片标题提供上下文。
2. **限速预算共用。** 实际冲突面小：jarvis 是 08:00/17:00 + 每 3h，Apollo 是 09:00
   催办扫描，频道也不同。
3. **凭证耦合：一个 token 进两个 `.env`，轮换会同时打断两个产品。**
   ⚠️ 这条必须写进 jarvis 的 `DEPLOY.md`，否则将来换 token 的人只会想到 Apollo。

### 频道

| 频道 | id | 用途 |
|---|---|---|
| `#jarvis-crashguard` | `C0C3S90ULGN` | crashguard + coreguard |
| `#jarvis-graygate` | `C0C3P854KN1` | graygate |

coreguard 不单独建频道——它今天就是回落到 crashguard 的 chat_id，保持同构。

## 架构

```
各模块 alerter / report
   │
   ├─ resolve_notify_target(module)  →  NotifyTarget(provider, channel="", email="")
   │        ↑ 把散在 6 处的三级回落链收成一个函数
   │
   └─ resolve_transport(provider).send_card(target, card, folds=[...])
            ├─ FeishuTransport  → 委托现有 feishu_cli（逐字节不动）
            └─ SlackTransport   → services/slack_cli.py（新增，httpx 直连）
```

**`slack_cli.py` 从 Apollo 移植一份，不抽共享包。** 2026-08 物理拆仓的前提就是
「jarvis 与 Apollo 互不依赖」，为一个 ~200 行的 httpx 封装重建依赖不值得。
移植时带上 Apollo 已经踩到的坑：

- `files.completeUploadExternal` 的 `channel_id` 校验 `^[CGDZ][A-Z0-9]{8,}$`，
  `U...` 直接被拒 ⇒ 私聊发文件必须先 `conversations.open`（纯文本 DM 可以直接
  `post_message(channel="U...")`）
- 按 channel 串行化 + 遵守 429 `Retry-After`

## 卡片：**不**做中立 IR

考虑过引入中立卡片 IR（`Card(title, severity, blocks=[Text|Fields|Fold|Buttons])`，
两个 provider 各自编译），**否决**：

| | 中立 IR | 各 provider 各写一份（选这个） |
|---|---|---|
| 改动面 | 重写 12 个 builder（~2300 行），而飞书是当下唯一在跑的渠道 | 飞书侧 0 改动 |
| 风险 | 动的是活路径，可能今天就把飞书早晚报改坏 | 飞书坏不了 |
| 长期 | 多一层 IR 要永久维护 | 按模块下线后只剩 Slack 一份渲染器 |
| 代价 | — | 烤期内文案要改两遍 |

决定性理由：**「预定下线」是按模块的**。graygate 切完就立刻删它的 592 行飞书
builder，重复只在那一个模块、只持续一个烤期。IR 那一层反而是永久成本。

### 三个 gap 的编译规则

| Feishu | Slack |
|---|---|
| `template: red/yellow/turquoise` | legacy attachment 的 `color` 左侧色条 |
| `column_set` 多列 | `section.fields`（两列，≤10 field，每个 ≤2000 字）；超出降级成逐行 |
| `lark_md` 表格 | mrkdwn 无表格 → 转 `fields`；对齐要紧的（crash-free 矩阵）用 code block |

### Block Kit 上限（**静默截断，不报错**）

每条消息 ≤50 blocks，`section.text` ≤3000 字，`header` ≤150 字，
`fields` ≤10 个 / 每个 ≤2000 字，button text ≤75 字。
⇒ 必须有一条黄金测试钉住这些上限。

## 配置形状

每个模块加一组字段，与现有飞书字段**并行**，不改名不动老的：

```yaml
crashguard:
  notify:
    provider: "feishu"          # feishu | slack —— 模块粒度开关
    slack_channel: ""           # C... 存 id 不存 name
    slack_alert_channel: ""     # 对应 feishu.alert_email 那条"告警不打扰群"的语义
  feishu:                        # 原样保留
    target_chat_id: "oc_7b7ed..."
```

三条硬约束：

1. **`slack_channel` 不给默认值、不硬编码兜底。** Apollo 的
   `config.py` 硬编码 `oncall_greeting_chat_id = "oc_517fdd..."` 已经证明这会怎么坏：
   切到另一个渠道时拿到前一个渠道的 id，发送方看起来成功、收件人是空气。
2. **`notify.provider` 必须显式加进 yaml→settings 映射。**
   `crashguard/config.py:706` 那段是逐 key 显式 `if "x" in f`，漏了就是死配置。
   graygate 是 env-only，走它自己的路。
3. **启动期 fail-fast 校验非法 provider 值**，判据是「实现了吗」（注册表）
   而不是字面量白名单。

DM 类目标（`alert_email` / `overflow_email` / `pr_reviewer_fallback_email`）
**不需要并行字段**——邮箱寻址在两个渠道下都成立，`users.lookupByEmail` 直接换。

## thread 落地的两个硬约束

- **thread 回复串行 + 失败不上抛。** 主消息成功即算成功；某条 FYI 回复失败只记
  日志。反过来会让「早晚报发出去了」变成 `ok=False` → `feishu_message_id` 送锁
  不落 `"sent"` → **下一个实例重发整张卡**。
- **`unfurl_links=false`。** 卡里全是 Datadog / GitHub / 前端深链，不关会把频道
  刷成一堆预览图。

## 迁移顺序

按卡片复杂度从低到高，每个模块自己烤：

| 顺序 | 模块 | 卡片 | 理由 |
|---|---|---|---|
| 1 | **graygate** | 1 个（592 行） | 最小、最独立、灰度结束可整体摘除，坏了影响面最小 |
| 2 | **coreguard** | 2 个（413 行） | 顺带把群配额溢出路由 provider 化 |
| 3 | **crashguard** | 9 个（1200 行） | 早晚报那张 TL;DR + N 折叠的卡最难，最后动 |

## 明确不动的范围

- 飞书 SSO 登录（`auth_feishu.py` / `api/auth.py` 的 4 个端点）
- 多维表 oncall 同步（`oncall_feishu_sync.py`）
- `feishu_message_id` 列名（跨实例发送锁，前端在读，改名零收益）
- `site_feedback.py` 的截图上传（不在这三个模块范围内；顺带：因此本次**不需要**
  `files:write`）
- `notify.py` / `feishu.py` 两个 deprecated shim

## 验证方式

1. 每个模块切换前：该模块现有测试全绿 = 「飞书侧零行为变化」的证据
   （`test_feishu_card.py` 9 条、`test_report_builder.py` 10 条、`test_card_builder.py`、
   `test_feishu_quota_routing.py` 5 条）
2. 新增 Slack 侧单测 mock httpx；**一条黄金测试钉住 Block Kit 上限**
3. 真机验证：每个模块在 Slack 频道实发一次，逐条核对色条 / fields 对齐 /
   thread 归属 / 按钮可点 / DM 到人
4. **切换后必须验「送锁没被 thread 失败带坏」**：mock 一条 thread 回复失败，
   确认 `feishu_message_id` 仍然落 `"sent"`、不会重发
