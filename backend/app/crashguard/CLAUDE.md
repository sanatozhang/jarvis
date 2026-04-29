# Crashguard 模块隔离约束

⚠️ 这是独立模块，未来可能拆分为独立服务。修改前必读：

## 禁止项

1. ❌ 禁止 `from app.models import ...`（除 `app.db.database.get_session`）
2. ❌ 禁止 `from app.workers.analysis_worker import ...`
3. ❌ 禁止 `from app.services.rule_engine import ...`
4. ❌ 禁止 `from app.api.issues|tasks|feedback import ...`
5. ❌ 禁止 SQL join 到非 `crash_*` 表（如 `issues`、`tasks`、`feedbacks`）
6. ❌ 禁止把 crashguard 字段塞进 jarvis 全局配置（用顶层 `crashguard:` 段）

## 允许的耦合点（仅这 4 个）

1. ✅ `app.services.feishu_cli.send_message` — 群消息推送
2. ✅ `app.services.repo_updater.create_branch_pr` — git PR 能力（仅 draft）
3. ✅ `app.services.agent_orchestrator.run_agent` — agent 调度
4. ✅ `app.db.database.get_session` — 共用 connection pool

## 新增耦合点的流程

1. 先更新 `docs/adr/0001-crashguard-isolation.md`
2. 修改 `backend/.importlinter` 的 forbidden_modules 白名单
3. 在 PR 描述里说明引入的耦合点 + 必要性
4. 通过 lint：`cd backend && lint-imports`

## 关于 PR 创建（重要）

crashguard 创建 PR **必须**：
- 始终 `--draft`，永不取消 draft 状态
- 严禁调用 `gh pr merge`、`git merge`、`gh pr ready` 任何合入操作
- 所有 PR 由人工 review + approve + merge

如有任何疑问，参考 `docs/superpowers/specs/2026-04-27-crashguard-design.md`。
