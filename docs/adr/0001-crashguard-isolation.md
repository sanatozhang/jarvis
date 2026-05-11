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
3. 仅允许 4 个对外耦合点（见模块 CLAUDE.md）
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

## 修订历史

- 2026-04-27 创建
