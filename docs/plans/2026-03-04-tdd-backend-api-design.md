# TDD 后端 API 测试设计

## 目标

为 Jarvis 后端建立完整的 API 集成测试套件，实现：
- 本地 `pytest` 全通过 → 放心部署
- Docker 构建阶段自动跑测试，失败则构建中止

## 技术选型

- **pytest** + **pytest-asyncio** — 异步测试运行
- **httpx.AsyncClient** — 模拟 HTTP 请求到 FastAPI app
- **SQLite :memory:** — 每个测试独立数据库，自动销毁
- **unittest.mock.patch** — 隔离外部依赖

## 外部依赖隔离

| 外部依赖 | Mock 方式 |
|---------|----------|
| Feishu API | patch FeishuClient |
| Linear API | patch LinearClient |
| Zendesk API | patch ZendeskClient |
| Agent CLI (claude/codex) | patch subprocess 调用 |
| Redis | 跳过（项目已有可选逻辑） |
| OpenAI | patch summarize 调用 |

## 目录结构

```
backend/
├── tests/
│   ├── conftest.py            # 共享 fixtures
│   ├── test_health.py         # /api/health
│   ├── test_issues.py         # /api/issues
│   ├── test_tasks.py          # /api/tasks
│   ├── test_rules.py          # /api/rules
│   ├── test_settings.py       # /api/settings
│   ├── test_reports.py        # /api/reports
│   ├── test_feedback.py       # /api/feedback
│   ├── test_users.py          # /api/users
│   ├── test_oncall.py         # /api/oncall
│   ├── test_v1_analyze.py     # /api/v1
│   ├── test_env_settings.py   # /api/env
│   ├── test_analytics.py      # /api/analytics
│   ├── test_linear.py         # /api/linear
│   ├── test_local.py          # /api/local（含标记不准确）
│   ├── test_eval.py           # /api/eval
│   └── test_golden_samples.py # /api/golden-samples
├── pytest.ini
└── requirements.txt           # 新增 pytest 依赖
```

## conftest.py 核心 fixtures

- `db_session` — 每个测试独立的内存 SQLite + 自动建表/销毁
- `client` — 绑定测试数据库的 httpx.AsyncClient
- `seed_data` — 预置测试数据（用户、工单、规则等）
- `mock_feishu` / `mock_linear` / `mock_agent` — 外部依赖 mock

## 测试用例覆盖（按优先级）

### P0 — 核心业务

**test_health.py**
- GET /api/health → 200 + 组件状态
- GET /api/health/agents → agent 可用性

**test_tasks.py**
- POST /api/tasks → 创建任务
- GET /api/tasks/{id} → 任务状态
- GET /api/tasks/{id}/result → 分析结果
- GET /api/tasks → 列表
- POST /api/tasks/batch → 批量创建

**test_local.py**
- GET /api/local/tracking → 多维度过滤
- GET /api/local/in-progress / completed / failed
- GET /api/local/{id}/detail → 工单详情
- GET /api/local/{id}/analyses → 分析历史
- POST /api/local/{id}/inaccurate → 标记不准确（含原因）
- GET /api/local/inaccurate → 列表不准确工单
- DELETE /api/local/{id} → 软删除

**test_rules.py**
- CRUD 全流程
- POST /api/rules/{id}/test → 规则匹配
- POST /api/rules/reload → 热重载

### P1 — 重要功能

**test_users.py** — 登录/注册、获取用户、列表
**test_oncall.py** — 值班查询、更新、权限
**test_feedback.py** — 提交反馈（含文件）、触发分析
**test_analytics.py** — 事件追踪、仪表板、规则准确率
**test_reports.py** — 日报生成、Markdown、日期列表

### P2 — 集成/扩展

**test_v1_analyze.py** — API Key 认证、分析流程
**test_issues.py** — 飞书工单拉取（mock）
**test_linear.py** — Webhook 签名、触发词
**test_env_settings.py** — 配置读写、权限
**test_eval.py** — 数据集 CRUD、评估运行
**test_golden_samples.py** — 样本提升、统计

## 每个测试文件覆盖

- 正常路径（happy path）
- 404 / 400 异常路径
- 权限校验（如有）

## Docker 集成

```dockerfile
# 构建阶段：跑测试
FROM python:3.12-slim AS test
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN pytest --tb=short -q

# 生产阶段：测试通过才到这里
FROM python:3.12-slim AS production
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## 工作流

```
1. 写/改代码
2. cd backend && pytest           ← 本地验证
3. 全部通过 → docker compose up --build  ← 部署
4. 测试失败 → 构建中止，坏代码不会上线
```

## pytest.ini

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```
