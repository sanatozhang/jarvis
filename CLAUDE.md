# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Backend (Python / FastAPI)

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Development server (auto-reload)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Or via main.py directly
python -m app.main
```

### Frontend (Next.js)

```bash
cd frontend
npm install
npm run dev       # Development
npm run build     # Production build
npm run lint      # ESLint
npm start         # Production start
```

### Docker (full stack)

```bash
docker compose up -d
docker compose logs -f backend
```

#### macOS 首次部署前置要求

macOS 没有内置 Docker daemon，需要先安装 colima：

```bash
brew install colima docker-compose
colima start          # 启动 Docker daemon（每次重启 Mac 后需重新执行）
brew services start colima  # 或设置开机自启
```

#### Claude CLI 登录（Docker 环境）

容器内 claude 凭证通过 named volume `claude-auth` 持久化，**首次部署后执行一次登录**：

```bash
docker compose exec -it backend claude login
# 复制输出的 URL 到浏览器完成授权，登录信息永久保存在 claude-auth volume 中
```

验证登录状态：

```bash
docker compose exec backend claude config list
```

### Rule hot-reload (no restart needed)

```bash
curl -X POST http://localhost:8000/api/rules/reload
```

## Architecture

```
Frontend (Next.js 15 + React 19 + Tailwind CSS 4)
  ↕ REST + SSE
Backend (FastAPI + SQLAlchemy + SQLite)
  ↕ subprocess
Agents (Claude Code CLI / Codex CLI — external binaries)
```

### Configuration Layering

Config is merged in this priority order: **env vars > config.yaml > defaults**

- `.env` — secrets: `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `LINEAR_API_KEY`, `DATABASE_URL`, etc.
- `config.yaml` — agent selection, routing, concurrency, model names
- `backend/app/config.py` — loads both via `get_settings()` (cached singleton)

### Analysis Pipeline

The core flow lives in `backend/app/workers/analysis_worker.py:run_analysis_pipeline()`:

1. **Fetch issue** — from Feishu API, local DB (`fb_` prefix), or Linear webhook (`lin_` prefix)
2. **Download logs** — Feishu file attachments; results cached in `workspaces/_cache/{issue_id}/`
3. **Decrypt** — `.plaud` proprietary format processed in `services/decrypt.py`
4. **Match rules** — `RuleEngine` matches issue description against Markdown rules in `backend/rules/`
5. **Pre-extract** — grep patterns from matched rules run against log files (L1 extraction)
6. **Build workspace** — `workspaces/{task_id}/` with subdirs: `raw/`, `logs/`, `rules/`, `code/`, `output/`
7. **Run agent** — CLI subprocess (`claude` or `codex`) writes JSON to `output/result.json`
8. **Parse result** — `BaseAgent.parse_result()` reads JSON; falls back to raw stdout parsing

### Issue Sources

| Prefix | Source | How it enters |
|--------|--------|---------------|
| (none) | Feishu | Pulled from Feishu BitTable via API |
| `fb_`  | Local  | Submitted via feedback form UI |
| `lin_` | Linear | Linear webhook (`/api/linear`) + `@ai-agent` comment trigger |

### Agent Abstractions

- `backend/app/agents/base.py` — `BaseAgent` ABC with `analyze()` and static `build_prompt()` / `parse_result()`
- `backend/app/agents/claude_code.py` — runs `claude` CLI as subprocess
- `backend/app/agents/codex.py` — runs `codex` CLI as subprocess
- `backend/app/services/agent_orchestrator.py` — selects agent via routing config, builds prompt, calls agent

Agent routing: `config.yaml agent.routing` maps problem type → agent name. Falls back to `agent.default`.

### Analysis Result Schema

Agents must write `output/result.json` with these fields (all required):
- `problem_type` / `problem_type_en`
- `root_cause` / `root_cause_en`
- `confidence`: `"high" | "medium" | "low"`
- `key_evidence`: list of log lines (max 5)
- `user_reply` / `user_reply_en` — customer-facing reply templates
- `needs_engineer`: boolean
- `fix_suggestion`

### Rules System

Rules live in `backend/rules/` as Markdown files with YAML frontmatter. `RuleEngine`:
- Loads and syncs to DB on startup
- Matches rules against issue descriptions via keywords and regex
- Pre-extracts log patterns (grep) before agent runs
- Hot-reloadable without restart

### Frontend

- Next.js App Router under `frontend/src/app/`
- All API calls in `frontend/src/lib/api.ts`
- i18n: Chinese keys with English translations in `frontend/src/lib/i18n.ts`; use `useT()` hook everywhere
- Real-time progress via SSE (`subscribeTaskProgress` in `api.ts`)

Pages: `/` (main analysis), `/tracking`, `/feedback`, `/oncall`, `/analytics`, `/rules`, `/reports`, `/settings`

### Backend API Prefixes

`/api/issues`, `/api/tasks`, `/api/rules`, `/api/settings`, `/api/reports`, `/api/health`, `/api/local`, `/api/feedback`, `/api/users`, `/api/oncall`, `/api/v1`, `/api/env`, `/api/analytics`, `/api/linear`

API docs available at `http://localhost:8000/docs` when running locally.

### Database

SQLite by default at `data/appllo.db`（宿主机路径）. Switch to PostgreSQL by setting `DATABASE_URL=postgresql+asyncpg://...` and uncommenting `asyncpg` in `requirements.txt`.

Zombie task cleanup (tasks stuck in analyzing/queued states) runs automatically on startup.

## Docker 部署已知问题与修复记录

迁移到新服务器时必读，以下问题均已在配置文件中修复，但需要了解原因。

### 1. PROJECT_ROOT 路径问题（最重要）

**现象**：数据库、workspaces、config.yaml 找不到或不生效。

**根因**：`backend/app/config.py` 中：
```python
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
```
在容器内 `__file__` 是 `/app/app/config.py`，三级向上得到 `/`（根目录），而非 `/app`。

因此所有相对路径都基于 `/` 解析：
| 路径用途 | 容器内实际路径 | 错误挂载 | 正确挂载 |
|---------|-------------|---------|---------|
| config.yaml | `/config.yaml` | `/app/config.yaml` | `/config.yaml` |
| 数据库 | `/data/appllo.db` | `./data:/app/data` | `./data:/data` |
| 工作区 | `/workspaces/` | `./workspaces:/app/workspaces` | `./workspaces:/workspaces` |

**已修复**：`docker-compose.yml` 中挂载路径已按上表修正。

### 2. Frontend 无法连接 Backend（ECONNREFUSED 500错误）

**现象**：网页所有 API 请求返回 500，frontend 日志报 `ECONNREFUSED localhost:8000`。

**根因**：`frontend/Dockerfile` 构建时未声明 `ARG NEXT_PUBLIC_API_URL`，导致 docker-compose 传入的 `http://backend:8000` 被忽略，Next.js rewrites 目标地址回退为默认值 `http://localhost:8000`。容器内 localhost 找不到 backend。

**已修复**：`frontend/Dockerfile` 构建阶段加入：
```dockerfile
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
```

### 3. Claude CLI 不在容器内

**现象**：health check 显示 `claude_code: not_installed`。

**根因**：Claude CLI 是 npm 包，Dockerfile 原来只装了 codex，未装 claude。macOS 本机二进制是 Mach-O 格式，无法挂载到 Linux 容器直接使用。

**已修复**：`backend/Dockerfile` 加入：
```dockerfile
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || echo "Claude install skipped (optional)"
```

### 4. Claude 登录态不持久

**现象**：容器重启后 claude 需要重新登录。

**根因**：macOS 上 claude 凭证存储在系统 Keychain，不在 `~/.claude` 目录，无法通过挂载宿主机目录传递到容器。

**已修复**：`docker-compose.yml` 使用 named volume 持久化：
```yaml
volumes:
  - claude-auth:/root/.claude
```
首次部署执行一次 `docker compose exec -it backend claude login` 即可。

### 5. Agent providers 为空（No enabled agent found）

**现象**：分析任务报错 `No enabled agent found. Tried 'claude_code'. Available: []`。

**根因**：同问题1，config.yaml 挂载路径错误导致文件未被读取，providers 配置为空 dict。

**已修复**：修正 config.yaml 挂载路径后自动解决。
