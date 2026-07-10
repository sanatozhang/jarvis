# ADR-0001 / Crashguard 模块隔离

**状态:** Accepted
**日期:** 2026-04-27
**决策者:** sanato

## 背景

Crashguard 是 jarvis 的子模块，用于自动化崩溃分析与 PR 提交。
未来可能拆分为独立服务，因此当前必须维持强解耦边界。

## 决策

1. 所有 crashguard 代码限制在 `backend/app/crashguard/` 子包内
2. 数据库表前缀 `crash_*`，无外键指向 jarvis 既有表
3. 仅允许 6 个对外耦合点（见模块 CLAUDE.md 及下方表格）
4. 通过 import-linter + DB 自检脚本强制约束
5. PR 必须 draft 创建，禁止任何合入操作

## 后果

**正面：**
- 未来可独立拆分为微服务，迁移成本可控
- 模块边界清晰，jarvis 主线 refactor 不影响 crashguard
- AI agent 修改时有明确指引（CLAUDE.md）

**负面：**
- 短期开发成本 +10%（无法直接复用 jarvis 业务代码）
- 跨模块查询需要应用层 lookup（如 crash → 工单关联）

## 实施要点

- `backend/.importlinter`：forbidden 合约（lint-imports 默认文件名）
- `backend/scripts/check_crash_decoupling.py`：启动时跑外键自检
- `backend/app/crashguard/CLAUDE.md`：AI 修改指引
- PR 模板加 checkbox：确认未引入新耦合点

## 允许的对外耦合点

| 函数 | 用途 |
|------|------|
| `app.services.feishu_cli.send_message` | 群消息 / 私聊推送 |
| `app.services.repo_updater.create_branch_pr` | Git PR（强制 `--draft`） |
| `app.services.agent_orchestrator.run_agent` | agent 调度 |
| `app.db.database.get_session` | 共用 connection pool |
| `app.services.repo_router.resolve` | 按 (platform, version) 解析源码/PR/符号化目标仓（Flutter→native 版本切换，2026-06-26）|
| `app.services.mt_runner.acquire_workspace_lock_async` / `release_workspace_lock_async` | 跨进程仓库文件锁，让 `pr_drafter` 的 git 操作与 `app.services.repo_updater` 的夜间同步任务协调（2026-07-10） |

**关于 `.importlinter`**：`app.services.repo_router` 不在 `forbidden_modules` 列表中，故 crashguard import 它已被合约允许。`repo_router.py` 仅使用 stdlib（os, re, logging, dataclasses），不引入任何禁止的推移依赖。因此 `.importlinter` 无需修改。同理 `app.services.mt_runner` 也不在 `forbidden_modules` 列表中，`lint-imports` 已验证通过（PASS，0 broken）。

## 修订历史

- 2026-04-27 创建
- 2026-06-26 因 native 迁移按版本路由仓库，新增 `app.services.repo_router` 为第 5 个允许耦合点；`repo_router` 不在 forbidden_modules 中且无禁止推移依赖，`.importlinter` 无需修改
- 2026-07-10 发现 `pr_drafter` 的 git 操作与 `app.services.repo_updater` 早已存在的夜间仓库同步任务（`repo_update_loop`，`main.py` 启动时注册）从未协调过锁，存在竞态。新增 `app.services.mt_runner.acquire_workspace_lock_async`/`release_workspace_lock_async` 为第 6 个允许耦合点，让 `pr_drafter` 也能拿到 `repo_updater` 已经在用的跨进程 `workspace_lock`；`mt_runner` 不在 forbidden_modules 中，`.importlinter` 无需修改
