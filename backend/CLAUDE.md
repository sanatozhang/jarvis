# Backend — jarvis（FastAPI + SQLAlchemy）

通用后端基础设施。crashguard/coreguard/graygate 各自的模块文档见根目录 `CLAUDE.md` 的「模块地图」。

工单处理相关代码（`api/issues,tasks,feedback,linear_webhook,rules,reports,oncall,analytics,...`）已物理迁出到独立仓库 Apollo（2026-08），本仓库不再包含。`config.py`/`db/database.py`/`feishu_cli.py`/`agent_orchestrator.py`/`auth.py`/`users.py` 等基础设施两边各自保留一份完整副本，是已知的后续瘦身项，非 bug。

## 启动

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Development server (auto-reload)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 单测
pytest tests/crashguard/ -v
pytest tests/  # 全量
```

API 文档：`http://localhost:8000/docs`

## 配置分层（env > yaml(local override) > yaml(默认) > defaults）

| 来源 | 文件 | 用途 |
|------|------|------|
| env / `.env` | 项目根 `.env` | secrets：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`DATABASE_URL`、`CRASHGUARD_DATADOG_API_KEY` 等 |
| yaml（每台服务器独立，**不进 git**） | `config.local.yaml`（项目根，`.gitignore` 排除，模板见 `config.local.yaml.example`） | `/settings` 页面"无需重启即可持久化"的开关（如 crashguard `qa_capture_enabled`、`symbol_upload_keep_versions`）写在这里；`app/config.py::write_local_override()` 读-合并-写。**不要**手动把这些字段加进 `config.yaml`——deploy 时的 `git pull` 不该覆盖某台服务器的临时调整（2026-07-21：曾把这类开关直接写 `config.yaml`，但 docker 部署把它挂载成只读，写入静默失败，重启后又变回默认值，见 commit 附近历史） |
| yaml（默认值，git 版本控制） | `config.yaml`（项目根） | agent 选择 / 路由 / 并发 / 模型名 / crashguard 段的默认值/模板 |
| defaults | `backend/app/config.py` | Pydantic `Settings`，`get_settings()` 缓存单例 |

⚠️ 容器内 `__file__` 三级 `parent` 解出来是 `/` 不是 `/app`，所以 docker-compose 挂载点用 `/config.yaml`、`/data/`、`/workspaces/`，不是 `/app/...`。修挂载点前看根 CLAUDE.md「Docker 已知问题 #1」。

## API 路由总览

| Prefix | 文件 | 模块 |
|--------|------|------|
| `/api/crash` | `app/crashguard/api/crash.py` | crashguard（独立子模块） |
| `/api/coreguard` | `app/coreguard/api/coreguard.py` | coreguard（独立子模块） |
| `/api/graygate` | `app/graygate/api/graygate.py` | graygate（独立子模块） |
| `/api/release` | `api/release.py` | Release 自动化（Jenkins 状态轮询） |
| `/api/users` | `api/users.py` | 用户账号（登录/鉴权，不含 oncall——oncall 现在是 Apollo 的功能） |
| `/api/site-feedback` | `api/site_feedback.py` | 全局反馈 widget → 飞书私聊管理员 |
| `/api/settings`、`/api/env`、`/api/health`、`/api/auth` | 通用 | 系统接口 |

## 数据库

- 默认 SQLite：`data/appllo.db`（宿主机相对于项目根；容器内挂在 `/data/`）——文件名沿用历史命名，与内容无关
- 切 PostgreSQL：`DATABASE_URL=postgresql+asyncpg://...` + 取消 `requirements.txt` 中 `asyncpg` 注释
- 表前缀域：`crash_*`（crashguard）、`coreguard_*`、`pt_*`（platform_tickets，仅为兼容 `db/database.py` 里未清理的 UNION 查询保留，本仓库无 API 会写入/读取）——三者互相隔离，无跨界外键，启动自检见下
- 严禁跨前缀域 join（隔离合约见 `app/crashguard/CLAUDE.md`，历史决策见 `docs/adr/0001-crashguard-isolation.md`）

## 启动顺序（`app/main.py` lifespan）

1. 初始化 DB + 注册 SQLAlchemy 模型（`crashguard`/`coreguard`/`platform_tickets`）
2. 启动时 DB 自检：`scripts/check_crash_decoupling.py` 检查 `crash_*`/`pt_*` 两个前缀域外键纯净度，违反则启动失败
3. 执行 `crashguard.migrations.ensure_columns()` 增量列迁移
4. 起周期任务：`db_health_monitor_loop`（SQLite 健康监控）、`repo_updater`（每日拉取代码仓）、`release_poller`（Jenkins 状态轮询）、crashguard `report_scheduler_loop`（早晚报）+ `warmup`/`pipeline_scheduler_loop`、coreguard/graygate 各自的 `scheduler_loop`（均受各自 `enabled`/`scheduler_enabled` 开关控制）
5. 挂载各子模块 API router

## 子模块文档

- `app/crashguard/CLAUDE.md` — crashguard 子模块（隔离合约 + 全量文档）
- `docs/modules/coreguard-thresholds.md` — coreguard 阈值设计
