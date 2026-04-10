# Jarvis - Plaud 工单智能分析平台

AI 驱动的工单日志分析系统，将值班流程产品化为独立 Web 服务，面向客服团队与工程师使用。

## 架构

```
Frontend (Next.js 15 + React 19 + Tailwind CSS 4)
  ↕ REST + SSE
Backend (FastAPI + SQLAlchemy + SQLite)
  ↕ subprocess
Agents (Claude Code CLI / Codex CLI)
  ↕
Redis (任务队列 + 缓存)
```

### 数据流

```
工单来源 (飞书 / Linear / 本地反馈 / Zendesk)
  → 下载日志附件 → 解密 .plaud → 规则匹配 + 预提取
  → 构建 workspace → Agent 分析 → 结构化结果 + 客服回复模板
```

### 工单来源

| 前缀 | 来源 | 接入方式 |
|------|------|---------|
| (无) | 飞书 | 从飞书多维表格拉取 |
| `fb_` | 本地 | 通过反馈表单提交 |
| `lin_` | Linear | Webhook + `@ai-agent` 评论触发 |
| (无) | Zendesk | 一键导入 |

## 功能

- **工单分析**：下载日志 → 解密 → 规则匹配 → 预提取 → AI Agent 分析
- **多 Agent 支持**：Claude Code / Codex，按问题类型路由
- **规则引擎**：Markdown + YAML frontmatter 格式规则，热加载无需重启
- **实时进度**：SSE 推送分析状态
- **多来源接入**：飞书、Linear、Zendesk、本地反馈
- **一键导入**：Zendesk 工单批量导入
- **一键转飞书**：分析结果回写飞书工单
- **值班汇总**：自动生成每日/自定义时段汇总报告
- **统计分析**：问题趋势、分类统计、分析结果概览
- **评测系统**：Golden Sample 基准 + Agent 输出评测
- **工具箱**：日志解密、蓝牙分析等独立工具

## 快速开始

### 环境要求

- Python 3.12+
- Node.js 20+
- Redis
- Claude Code CLI 或 Codex CLI（至少一个）

### 配置

```bash
cp .env.example .env
# 编辑 .env 填入必要配置（飞书、Linear 等 API 密钥）
```

配置优先级：**环境变量 > config.yaml > 代码默认值**

- `.env` — 密钥：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`LINEAR_API_KEY` 等
- `config.yaml` — Agent 选择、路由、并发、模型配置

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

访问 http://localhost:3000 | API 文档 http://localhost:8000/docs

### 规则热加载

```bash
curl -X POST http://localhost:8000/api/rules/reload
```

## 项目结构

```
jarvis/
├── backend/
│   ├── app/
│   │   ├── main.py                    # FastAPI 入口
│   │   ├── config.py                  # 配置管理（yaml + env 合并）
│   │   ├── models/                    # SQLAlchemy 数据模型
│   │   ├── services/
│   │   │   ├── feishu.py              # 飞书 API 集成
│   │   │   ├── decrypt.py             # .plaud 日志解密
│   │   │   ├── rule_engine.py         # 规则引擎（匹配 + 预提取）
│   │   │   ├── extractor.py           # 日志模式预提取
│   │   │   └── agent_orchestrator.py  # Agent 选择 + 调度
│   │   ├── agents/
│   │   │   ├── base.py                # Agent 抽象基类
│   │   │   ├── claude_code.py         # Claude Code CLI 封装
│   │   │   └── codex.py               # Codex CLI 封装
│   │   ├── api/                       # FastAPI 路由
│   │   ├── workers/                   # 后台分析 pipeline
│   │   └── db/                        # 数据库操作
│   ├── rules/                         # 分析规则（Markdown + YAML）
│   └── tests/                         # 测试（构建时自动运行）
├── frontend/
│   └── src/
│       ├── app/                       # Next.js App Router 页面
│       ├── lib/
│       │   ├── api.ts                 # API 调用 + SSE 订阅
│       │   └── i18n.ts                # 国际化（中/英）
│       └── components/                # UI 组件
├── docker-compose.yml                 # 三服务编排（backend + frontend + redis）
├── config.yaml                        # 全局配置（Agent、并发、路由）
└── .env.example                       # 环境变量模板
```

### 前端页面

| 路径 | 功能 |
|------|------|
| `/` | 工单分析主页 |
| `/tracking` | 分析任务跟踪 |
| `/feedback` | 用户反馈管理 |
| `/oncall` | 值班汇总 |
| `/analytics` | 统计分析 |
| `/reports` | 报告管理 |
| `/rules` | 规则管理 |
| `/eval` | 评测系统 |
| `/samples` | Golden Samples |
| `/tools` | 工具箱 |
| `/wishes` | 需求池 |
| `/settings` | 系统设置 |

### 后端 API

| 路由前缀 | 用途 |
|----------|------|
| `/api/issues` | 飞书工单 |
| `/api/local` | 本地工单（进行中/已完成/失败） |
| `/api/tasks` | 分析任务管理 |
| `/api/rules` | 规则 CRUD + 热加载 |
| `/api/feedback` | 用户反馈 |
| `/api/analytics` | 统计数据 |
| `/api/reports` | 汇总报告 |
| `/api/oncall` | 值班相关 |
| `/api/settings` | 系统配置 |
| `/api/linear` | Linear Webhook |
| `/api/v1` | 外部分析 API |
| `/api/health` | 健康检查 |

## Docker 部署注意事项

### PROJECT_ROOT 路径问题

`backend/app/config.py` 中 `PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent`，在容器内解析为 `/`（根目录）而非 `/app`。因此 `docker-compose.yml` 中的卷挂载已按此调整：

| 用途 | 容器内路径 | 挂载配置 |
|------|-----------|---------|
| config.yaml | `/config.yaml` | `./config.yaml:/config.yaml:ro` |
| 数据库 | `/data/appllo.db` | `./data:/data` |
| 工作区 | `/workspaces/` | `./workspaces:/workspaces` |
| 规则文件 | `/app/rules/` | `./backend/rules:/app/rules:ro` |
| Claude 凭证 | `/root/.claude` | `claude-auth:/root/.claude` |

### 数据库

默认 SQLite，路径 `data/appllo.db`。可切换 PostgreSQL：

```bash
# .env
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/jarvis
```

并取消 `requirements.txt` 中 `asyncpg` 的注释。
