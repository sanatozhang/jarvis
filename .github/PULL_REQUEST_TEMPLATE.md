## Crashguard 隔离检查（仅当本 PR 改动 `backend/app/crashguard/`）

- [ ] 已确认未引入新的 jarvis 耦合点（参见 ADR-0001）
- [ ] `lint-imports` 通过
- [ ] crash_* 表无新增外键指向非 crash_* 表
