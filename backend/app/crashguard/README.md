# Crashguard 模块

崩溃自动化分析与 PR 提交（jarvis 子模块）。

## 概览

每天 07:00 + 17:00 自动从 Datadog 拉崩溃 → 三维分类（全新/回归/飙升）→ Top20 排序 → AI agent 分析 → Flutter 自动 draft PR / Android·iOS 半自动 → Feishu 群消息日报。

## 入口

- API: `/api/crash/*`（详见 `api/`）
- 调度: APScheduler in `workers/scheduler.py`
- 手动触发: `POST /api/crash/trigger`

## 隔离约束

⚠️ **必读** `CLAUDE.md` — 修改本模块前的隔离规则与允许的对外耦合点。

ADR: `docs/adr/0001-crashguard-isolation.md`

## 配置

- env: `CRASHGUARD_*`（如 `CRASHGUARD_DATADOG_API_KEY`）
- yaml: `config.yaml` 顶层 `crashguard:` 段

## 开发

```bash
# 单元测试
cd backend
pytest tests/crashguard/ -v

# 解耦 lint
lint-imports

# 启动时 DB 自检
python -m scripts.check_crash_decoupling
```

## 未来拆分预案

如未来要拆出独立服务，按以下顺序：

1. `backend/app/crashguard/` 整体迁移到独立 repo
2. 替换 4 个 jarvis 函数调用 → HTTP 调用对应 jarvis API
3. `crash_*` 表迁移到独立 SQLite
4. 部署: 独立 docker-compose service

详见 ADR-0001。
