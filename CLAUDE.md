# CLAUDE.md

This file orients Claude Code to the jarvis project. **Read the per-模块 CLAUDE.md for details when editing a specific module.**

## 项目概览

jarvis 是 Plaud 内部的崩溃自动化平台：crashguard（崩溃自动分析→开 PR）+ coreguard（核心指标监控）+ graygate（灰度期监控）。工单处理是独立的姊妹项目 **Apollo**（`github.com/Plaud-AI/Apollo`），2026-08 已完成物理仓库拆分，jarvis 与 Apollo 互不依赖、互不支持，各自独立部署。

```
Frontend (Next.js 15 + React 19 + Tailwind CSS 4)
  ↕ REST + SSE
Backend  (FastAPI + SQLAlchemy + SQLite)
  ↕ subprocess
Agents   (Claude Code CLI / Codex CLI — external binaries)
  +
Datadog  (crashguard/coreguard 直连 Datadog Error Tracking + RUM)
```

## 模块地图（编辑前先读对应文档）

| 模块 | 涉及代码 | 详细文档 |
|------|---------|---------|
| **Crashguard 崩溃监控** | 后端 `backend/app/crashguard/`<br>前端 `frontend/src/app/crashguard/` | 后端：`backend/app/crashguard/CLAUDE.md`<br>前端：`frontend/src/app/crashguard/CLAUDE.md` |
| **Coreguard 核心指标** | 后端 `backend/app/coreguard/` | `docs/modules/coreguard-thresholds.md` |
| **Graygate 灰度监控** | 后端 `backend/app/graygate/` | 无独立文档，代码即文档（模块小，见 `backend/app/graygate/CLAUDE.md` 若存在） |
| **Release 自动化** | 后端 `app/api/release.py` + `app/workers/release_poller.py` | `docs/superpowers/specs/2026-05-20-release-automation-design.md` |

通用基础设施文档：

- 后端整体（FastAPI / DB / 配置分层 / 启动顺序 / API 前缀总表）→ `backend/CLAUDE.md`
- 前端整体（App Router / i18n / api.ts / SSE 约定）→ `frontend/CLAUDE.md`

## 开发命令（速查）

```bash
# Backend dev
cd backend && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Frontend dev
cd frontend && npm install && npm run dev

# Full stack (Docker)
docker compose up -d
docker compose logs -f backend

# Crashguard tests + 隔离 lint
cd backend && pytest tests/crashguard/ -v
cd backend && lint-imports
```

详细命令见 `backend/CLAUDE.md` / `frontend/CLAUDE.md`。

## Docker 部署（macOS 前置）

macOS 无原生 Docker daemon，先装 colima：

```bash
brew install colima docker-compose
colima start                       # 每次重启 Mac 后需重新执行
brew services start colima         # 或设置开机自启
```

Claude CLI 在容器内通过 named volume `claude-auth` 持久化，**首次部署登录一次**：

```bash
docker compose exec -it backend claude login
docker compose exec backend claude config list   # 验证
```

## Docker 部署已知问题与修复记录

迁移到新服务器时必读。以下问题均已在配置文件中修复，但需了解原因。

### 1. PROJECT_ROOT 路径问题（最重要）

**现象**：数据库、workspaces、config.yaml 找不到。

**根因**：`backend/app/config.py`:
```python
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
```
容器内 `__file__ = /app/app/config.py`，三级向上得 `/`（根目录），不是 `/app`。所以挂载点用 `/...` 而非 `/app/...`：

| 路径用途 | 容器内实际路径 | 错误挂载 | 正确挂载 |
|---------|-------------|---------|---------|
| config.yaml | `/config.yaml` | `/app/config.yaml` | `/config.yaml` |
| 数据库 | `/data/appllo.db` | `./data:/app/data` | `./data:/data` |
| 工作区 | `/workspaces/` | `./workspaces:/app/workspaces` | `./workspaces:/workspaces` |

**已修复**：`docker-compose.yml`。

### 2. Frontend 无法连接 Backend（ECONNREFUSED 500）

**根因**：`frontend/Dockerfile` 构建时未声明 `ARG NEXT_PUBLIC_API_URL` → docker-compose 传入的 `http://backend:8000` 被忽略 → Next.js rewrites 回退 `http://localhost:8000` → 容器内 localhost 找不到 backend。

**已修复**：`frontend/Dockerfile` 构建阶段：
```dockerfile
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
```

### 3. Claude CLI 不在容器内

**根因**：macOS 本机 Mach-O 二进制无法挂到 Linux 容器；Dockerfile 原本只装 codex。

**已修复**：`backend/Dockerfile`:
```dockerfile
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || echo "Claude install skipped (optional)"
```

### 4. Claude 登录态不持久

**根因**：macOS 上 claude 凭证存 Keychain，不在 `~/.claude`，无法挂载传容器。

**已修复**：`docker-compose.yml` named volume：
```yaml
volumes:
  - claude-auth:/root/.claude
```
首次部署 `docker compose exec -it backend claude login`。

### 5. Agent providers 为空（No enabled agent found）

**根因**：同 #1，config.yaml 挂载路径错 → 文件没读到 → providers `{}`。

**已修复**：修正 config.yaml 挂载路径后自动解决。

### 6. 早晚报延后 8 小时

**根因**：容器默认 UTC 时区，cron `0 7 * * *` 在 UTC 7:00 触发即北京 15:00。

**已修复**：`docker-compose.yml` 给 backend 显式设 `TZ=Asia/Shanghai`（commit `6bb8f81`）。
