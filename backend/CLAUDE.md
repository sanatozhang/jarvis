# Backend — Jarvis（FastAPI + SQLAlchemy）

通用后端基础设施。具体业务模块文档见根目录 `CLAUDE.md` 的「模块地图」。

## 启动

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Development server (auto-reload)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 单测（按模块）
pytest tests/crashguard/ -v
```

API 文档：`http://localhost:8000/docs`

## 配置分层（env > yaml > defaults）

| 来源 | 文件 | 用途 |
|------|------|------|
| env / `.env` | 项目根 `.env` | secrets：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`LINEAR_API_KEY`、`DATABASE_URL`、`CRASHGUARD_DATADOG_API_KEY` 等 |
| yaml | `config.yaml`（项目根） | agent 选择 / 路由 / 并发 / 模型名 / Crashguard 段 |
| defaults | `backend/app/config.py` | Pydantic `Settings`，`get_settings()` 缓存单例 |

⚠️ 容器内 `__file__` 三级 `parent` 解出来是 `/` 不是 `/app`，所以 docker-compose 挂载点用 `/config.yaml`、`/data/`、`/workspaces/`，不是 `/app/...`。修挂载点前看根 CLAUDE.md「Docker 已知问题 #1」。

## API 路由总览

总分两类：**jarvis 主流**（`app/api/`）与 **Crashguard 子模块**（`app/crashguard/api/`，独立挂载）。

| Prefix | 文件 | 模块 |
|--------|------|------|
| `/api/issues` | `api/issues.py` | 工单分析 |
| `/api/tasks` | `api/tasks.py` | 工单分析 |
| `/api/feedback` | `api/feedback.py` | 工单分析（本地工单） |
| `/api/linear` | `api/linear_webhook.py` | 工单分析（Linear webhook） |
| `/api/rules` | `api/rules.py` | 工单分析（规则 CRUD + 热加载） |
| `/api/reports` | `api/reports.py` | 工单分析（输出报表） |
| `/api/oncall` | `api/oncall.py` | Oncall 管理 |
| `/api/users` | `api/users.py` | Oncall 管理（用户/管理员） |
| `/api/analytics` | `api/analytics.py` | 数据统计 |
| `/api/crash` | `app/crashguard/api/crash.py` | Crashguard（独立子模块） |
| `/api/site-feedback` | `api/site_feedback.py` | 全局反馈 widget → 飞书私聊管理员 |
| `/api/settings`、`/api/env`、`/api/health`、`/api/local`、`/api/v1` | 通用 | 系统接口 |

## 数据库

- 默认 SQLite：`data/appllo.db`（宿主机相对于项目根；容器内挂在 `/data/`）
- 切 PostgreSQL：`DATABASE_URL=postgresql+asyncpg://...` + 取消 `requirements.txt` 中 `asyncpg` 注释
- 启动时跑 zombie task 清理（卡在 analyzing/queued 的任务自动重置）
- Crashguard 表前缀 `crash_*`，严禁与 jarvis 表 join（隔离合约见 `app/crashguard/CLAUDE.md`）

## 启动顺序（`app/main.py` lifespan）

1. 初始化 DB + 注册所有 SQLAlchemy 模型（含 `app.crashguard.models`）
2. 启动时 DB 自检：`scripts/check_crash_decoupling.py` 检查 crash_* 表外键纯净度，违反则启动失败
3. 执行 `crashguard.migrations.ensure_columns()` 增量列迁移
4. 清理 zombie task
5. 起 Crashguard 周期任务：`workers/scheduler.py`（早晚报 cron）+ `workers/warmup.py`（启动 warmup + 周期 pipeline）
6. 挂载 jarvis API router + crashguard API router

## 子模块文档

- `app/crashguard/CLAUDE.md` — Crashguard 子模块（隔离合约 + 全量文档）
- 工单分析 / Oncall / 数据统计 — 见 `docs/modules/*.md`
