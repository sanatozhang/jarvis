# jarvis - Plaud 崩溃自动化平台

AI 驱动的崩溃/核心指标自动化监控与修复系统：crashguard（崩溃自动分析→开 PR）+
coreguard（核心指标 SHoW 对比告警）+ graygate（灰度期临时监控）。

工单处理是独立的姊妹项目 **Apollo**（`github.com/Plaud-AI/Apollo`），2026-08 已完成
物理仓库拆分，两者互不依赖、互不支持，各自独立部署，不共享数据库。

## 部署脚本

| 机器 | 部署方式 | 命令 |
|------|---------|------|
| 102 机器 | Docker 模式 | `./deploy.sh update` |
| 100 机器 | Bare-metal 模式 | `./deploy-bare.sh update` |

> 在对应机器的项目根目录执行；`update` 子命令会拉最新代码并重建/重启服务。

## 架构

```
Frontend (Next.js 15 + React 19 + Tailwind CSS 4)
  ↕ REST + SSE
Backend (FastAPI + SQLAlchemy + SQLite)
  ↕ subprocess
Agents (Claude Code CLI / Codex CLI)
  ↕
Redis (缓存) + Datadog (Error Tracking / RUM，crashguard·coreguard 直连)
```

### 数据流（crashguard）

```
Datadog Error Tracking (崩溃事件)
  → 拉取 issue → 符号化（llvm-symbolizer / dSYM） → repo_router 定位源码
  → 构建 workspace → Agent 分析根因 → 起草 PR（draft，禁止自动合入）
  → 早晚报 / 实时告警 推飞书
```

## 功能

- **crashguard**：崩溃自动分析 → 定位源码 → 起草修复 PR（draft-only，人工 review 合入）
- **coreguard**：核心业务指标每小时 SHoW（同比历史同期）对比，异常预测带告警
- **graygate**：灰度版本（如 4.0.3）期间的临时崩溃/卡顿日报监控
- **Release 自动化**：Jenkins 构建状态轮询，接入发布流程
- **多 Agent 支持**：Claude Code / Codex，按问题类型路由
- **实时进度**：SSE 推送分析状态
- **DB 隔离自检**：启动时校验 `crash_*`/`pt_*` 两个前缀域无跨界外键，违反则拒绝启动

## 快速开始

### 环境要求

- Python 3.12+
- Node.js 20+
- Redis
- Claude Code CLI 或 Codex CLI（至少一个）
- Datadog API/App Key（crashguard/coreguard 必需）

### 配置

```bash
cp .env.example .env
# 编辑 .env 填入必要配置（飞书、Datadog、GitHub token 等）
```

配置优先级：**环境变量 > config.local.yaml（每台服务器独立，不进 git）> config.yaml（默认值）> 代码默认值**

- `.env` — 密钥：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`CRASHGUARD_DATADOG_API_KEY`、`CRASHGUARD_DATADOG_APP_KEY`、`GH_TOKEN` 等
- `config.yaml` — Agent 选择、路由、并发、模型配置、crashguard 段默认值

### Docker 部署（推荐）

```bash
docker compose up -d
```

服务组成：
- `backend` — FastAPI，端口 8000
- `frontend` — Next.js standalone，端口 3000
- `redis` — Redis 7 Alpine

#### macOS 首次部署前置

macOS 没有内置 Docker daemon，需先安装 colima：

```bash
brew install colima docker-compose
colima start                    # 启动 Docker daemon（每次重启 Mac 后需重新执行）
brew services start colima      # 或设置开机自启
```

#### Claude CLI 登录

容器内 Claude 凭证通过 named volume `claude-auth` 持久化，首次部署后执行一次：

```bash
docker compose exec -it backend claude login
# 复制输出的 URL 到浏览器完成授权
```

验证登录状态：

```bash
docker compose exec backend claude config list
```

### 本地开发

```bash
# 后端
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 前端
cd frontend
npm install
npm run dev
```

访问 http://localhost:3000（自动跳转 `/crashguard`） | API 文档 http://localhost:8000/docs

## 项目结构

```
jarvis/
├── backend/
│   ├── app/
│   │   ├── main.py                    # FastAPI 入口
│   │   ├── config.py                  # 配置管理（yaml + env 合并）
│   │   ├── crashguard/                # 崩溃自动分析 + 开 PR（独立子模块）
│   │   ├── coreguard/                 # 核心指标监控（独立子模块）
│   │   ├── graygate/                  # 灰度期临时监控（独立子模块）
│   │   ├── services/
│   │   │   ├── feishu_cli.py          # 飞书 API 集成（lark-cli 封装）
│   │   │   ├── agent_orchestrator.py  # Agent 选择 + 调度
│   │   │   └── repo_router.py         # 按平台/版本解析源码仓路径
│   │   ├── agents/
│   │   │   ├── base.py                # Agent 抽象基类
│   │   │   ├── claude_code.py         # Claude Code CLI 封装
│   │   │   └── codex.py               # Codex CLI 封装
│   │   ├── api/                       # 通用 FastAPI 路由（settings/health/users/release 等）
│   │   ├── workers/                   # release_poller 等后台任务
│   │   └── db/                        # 数据库操作
│   └── tests/                         # 测试（构建时可选自动运行）
├── frontend/
│   └── src/
│       ├── app/                       # Next.js App Router 页面（crashguard/release/settings）
│       ├── lib/
│       │   ├── api.ts                 # API 调用
│       │   └── i18n.ts                # 国际化（中/英）
│       └── components/                # UI 组件
├── docker-compose.yml                 # 三服务编排（backend + frontend + redis）
├── config.yaml                        # 全局配置（Agent、并发、路由、crashguard 段）
└── .env.example                       # 环境变量模板
```

### 前端页面

| 路径 | 功能 |
|------|------|
| `/` | 重定向到 `/crashguard` |
| `/crashguard` | 崩溃看板 |
| `/release` | 发布管理 |
| `/settings` | 系统设置 |

### 后端 API

| 路由前缀 | 用途 |
|----------|------|
| `/api/crash` | crashguard（崩溃分析、PR、早晚报） |
| `/api/coreguard` | coreguard（核心指标监控） |
| `/api/graygate` | graygate（灰度期监控） |
| `/api/release` | Release 自动化（Jenkins 状态） |
| `/api/users` | 用户账号 |
| `/api/site-feedback` | 全局反馈 widget |
| `/api/settings`、`/api/env`、`/api/health`、`/api/auth` | 系统接口 |

## Docker 部署注意事项

### PROJECT_ROOT 路径问题

`backend/app/config.py` 中 `PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent`，在容器内解析为 `/`（根目录）而非 `/app`。因此 `docker-compose.yml` 中的卷挂载已按此调整：

| 用途 | 容器内路径 | 挂载配置 |
|------|-----------|---------|
| config.yaml | `/config.yaml` | `./config.yaml:/config.yaml:ro` |
| 数据库 | `/data/appllo.db` | `./data:/data` |
| 工作区 | `/workspaces/` | `./workspaces:/workspaces` |
| Claude 凭证 | `/root/.claude` | `claude-auth:/root/.claude` |

### 数据库

默认 SQLite，路径 `data/appllo.db`（文件名沿用历史命名）。可切换 PostgreSQL：

```bash
# .env
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/jarvis
```

并取消 `requirements.txt` 中 `asyncpg` 的注释。
