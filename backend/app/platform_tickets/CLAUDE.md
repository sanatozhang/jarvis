# Platform Tickets 后端模块

新平台（**web / mcp / desktop**）工单存储子模块。结构照搬 `app/crashguard/`（表前缀隔离、
独立 models/migrations/config、零跨界 FK 自检），但与 crashguard 有一处**关键设计差异**，
见下方「与 crashguard 的区别」。

---

## 模块目的

app 现有 `issues` 表（`IssueRecord`，`app/db/database.py`）已经积重难返：device_sn/firmware/
app_version + feishu/linear/zendesk 专属字段 + escalation + 十余处 `session.get(IssueRecord, id)`
直查 + oncall 全表扫描 + tracking 单表分页。强行把 web/mcp/desktop 工单塞进这张表 = 让新平台
继承历史包袱，且大改这张表会波及一大堆现有功能，风险高。

本模块给新平台开一张**独立、通用、单表 + JSON 载荷**的存储：`pt_tickets`。新增平台
（比如未来的 "watch"）只需要在 `app/platforms.py::PLATFORMS` 加一个值 + 把专属字段塞进
`payload_json`，**零建表迁移**。

---

## 表结构：`pt_tickets`（ORM `PlatformTicket`，`app/platform_tickets/models.py`）

- 通用生命周期字段：`id`（`pt_<uuid hex>` 前缀）、`platform`、`description`、`priority`、
  `source`、`status`、`rule_type`、`category`、`created_by`、`occurred_at`、`deleted`、
  `created_at_ms`/`created_at`/`updated_at`。
- 完整 escalation 字段集（对齐 `IssueRecord`，供 oncall 统一层零特判复用）：
  `escalated_at`/`escalated_by`/`escalation_note`/`escalation_status`/
  `escalation_resolved_at`/`escalation_chat_id`/`escalation_share_link`/
  `escalation_reminded_at`。
- `payload_json`：平台专属字段（web 的 url/browser/session、mcp 的 client/tool 等）的
  JSON 载荷。**没有** device_sn/firmware/app_version/log_files/feishu/linear/zendesk
  等 app 专属列——这些是 app 平台的历史包袱，不应该污染新平台的表结构。
- 建表本身走 SQLAlchemy `Base.metadata.create_all`（`app/db/database.py::init_db()`），
  不手写 `CREATE TABLE`。未来加列走 `migrations.py::_REQUIRED_COLUMNS`（目前为空骨架）。

## 隔离约束（保留）

- 表前缀固定 `pt_`。
- **零跨界外键**：`pt_*` 表不得有外键指向非 `pt_*` 表（与 crashguard 的 `crash_*` 同款
  约束，各自独立解耦域——不要求 `crash_*` 和 `pt_*` 互相指向对方合法）。
- 自检脚本：`backend/scripts/check_crash_decoupling.py`（`assert_crash_tables_decoupled()`
  已参数化为同时检查 `("crash_", "pt_")` 两个前缀域，函数名保持不变以兼容 `main.py` 现有
  调用）。启动时跑，违规则阻止启动。

## 与 crashguard 的区别（重要，勿套错模板）

crashguard 是**另一个领域**（崩溃监控，直连 Datadog），刻意用 `.importlinter` 的
`forbidden_modules` 合约把它和 jarvis 核心工单流程隔离开，防止耦合蔓延。

**platform_tickets 是同一领域的延伸**（还是工单，只是换了个存储）：
- 用户决策：web/mcp/desktop 工单要**走 app 同一套 AI 分析流程**
  （`app/workers/analysis_worker.py`），**第一天就统一进 tracking + oncall**。
- 这要求本模块被 jarvis 核心**读取**（统一读取层 `app/db/database.py` 要能查
  `pt_tickets`）、被核心**调用**（analysis_worker 要能把 pt 工单映射成 `Issue` 喂进
  现有分析流水线）。
- 因此：**本模块故意不写 `.importlinter` forbidden 合约**。crashguard 那种 import 墙
  用在这里会直接挡住上述融入路径，是设计上的反模式。
- 隔离改靠：独立表 + `payload_json` + 「id 前缀路由 → 统一读取层」这条路径保证，
  而不是靠禁止 import。跨界耦合是预期行为，不是需要 ADR 走审批的例外。

## 配置

- `app/platform_tickets/config.py::PlatformTicketsSettings`，env 前缀 `PT_`。
- 目前只有 `enabled: bool = True` 一个字段（占位，未接入任何三层 kill switch）。
  后续若需要运行时开关（类似 crashguard 的 `enabled`/`pr_enabled`/`feishu_enabled`
  三层结构），再对齐扩展。

## 启动接线（`app/main.py` lifespan）

- 注册 ORM：`from app.platform_tickets import models as _platform_tickets_models  # noqa: F401`
  （与 crashguard/coreguard 的 models import 并列，早于 `init_db()`）。
- 迁移骨架调用：`from app.platform_tickets.migrations import ensure_columns` +
  `await ensure_columns()`（当前是 no-op，保留调用位置供未来加列时直接生效）。

## 当前阶段（骨架，未接线业务流程）

本模块目前只有存储层骨架（models + migrations + config），**尚未**包含：
- id→存储路由 / 跨表统一读取层（`app.db.database` 侧，属于下一阶段，由其它 agent 负责）
- `/api/platform-tickets` 录入 API
- analysis_worker 接入
- analytics 平台维度、per-platform 分类体系
- 前端 tracking/oncall/analytics 平台展示

这些属于后续阶段，见项目根方案文档（多平台工单隔离架构）。**不要**假设本模块已经
被读取层/分析流程调用——目前只是建表 + 自检通过。
