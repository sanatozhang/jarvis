# Jarvis - Plaud 工单智能分析平台

AI 驱动的工单日志分析系统，将 Cursor Rule 中的值班流程产品化为独立 Web 服务，面向客服团队使用。

## 架构

```
┌─────────────┐     REST/SSE     ┌──────────────┐    Task Queue    ┌──────────────┐
│   Frontend   │ ◄──────────────► │   Backend    │ ◄──────────────► │   Workers    │
│  (Next.js)   │                  │  (FastAPI)   │                  │  (arq/Redis) │
└─────────────┘                  └──────────────┘                  └──────┬───────┘
                                                                          │
                                                          ┌───────────────┼───────────────┐
                                                          │               │               │
                                                   ┌──────▼──┐    ┌──────▼──┐    ┌───────▼───┐
                                                   │ Claude   │    │ Codex   │    │ Feishu    │
                                                   │ Code     │    │         │    │ API       │
                                                   └─────────┘    └─────────┘    └───────────┘
```

## 快速开始

### 1. 环境要求

- Python 3.11+
- Node.js 18+
- Redis
- Claude Code CLI 或 Codex CLI（至少一个）

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env 填入必要配置
```

### 3. 启动（Docker）

```bash
docker compose up -d
```

### 4. 启动（本地开发）

```bash
# 后端
cd backend
pip install -r requirements.txt
python -m app.main

# 前端
cd frontend
npm install
npm run dev
```

访问 http://localhost:3000

## 功能

- **工单列表**：自动从飞书拉取待处理工单
- **一键分析**：下载日志 → 解密 → 预提取 → AI Agent 分析
- **可配置 Agent**：支持 Claude Code / Codex，可按问题类型路由
- **规则管理**：类似 Cursor Rule，可热加载分析规则
- **实时进度**：SSE 推送分析进度
- **用户回复**：一键复制生成的客服回复模板
- **值班汇总**：自动生成每日汇总报告

## 项目结构

```
jarvis/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── config.py            # 配置管理
│   │   ├── models/              # 数据模型
│   │   ├── services/            # 业务逻辑
│   │   │   ├── feishu.py        # 飞书 API
│   │   │   ├── decrypt.py       # .plaud 解密
│   │   │   ├── rule_engine.py   # 规则引擎
│   │   │   ├── extractor.py     # 日志预提取
│   │   │   └── agent_orchestrator.py
│   │   ├── agents/              # Agent 实现
│   │   │   ├── base.py          # 抽象接口
│   │   │   ├── claude_code.py   # Claude Code
│   │   │   └── codex.py         # Codex
│   │   ├── api/                 # API 路由
│   │   ├── workers/             # 后台任务
│   │   └── db/                  # 数据库
│   └── rules/                   # 分析规则
├── frontend/                    # Next.js 前端
├── docker-compose.yml
├── config.yaml                  # 全局配置
└── .env.example
```
