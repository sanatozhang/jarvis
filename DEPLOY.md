# Jarvis 部署文档

## 目录

- [一、本地部署（开发模式）](#一本地部署开发模式)
- [二、远端服务器部署（Docker）](#二远端服务器部署docker)
- [三、远端服务器部署（手动）](#三远端服务器部署手动)
- [四、配置说明](#四配置说明)
- [五、Agent CLI 安装](#五agent-cli-安装)
- [六、验证部署](#六验证部署)
- [七、运维](#七运维)

---

## 一、本地部署（开发模式）

### 前提条件

- Python 3.11+
- Node.js 18+
- Claude Code CLI (`claude`) 或 Codex CLI (`codex`)

### 步骤

```bash
# 1. 进入项目目录
cd /Users/sanato/Desktop/code/newplaud/jarvis

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 FEISHU_APP_ID 和 FEISHU_APP_SECRET

# 3. 启动后端
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 4. 启动前端（新终端）
cd frontend
npm install
npm run dev
```

### 访问地址

| 服务 | 地址 |
|------|------|
| 前端界面 | http://localhost:3000 |
| 后端 API | http://localhost:8000 |
| API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/api/health |

---

## 二、远端服务器部署（Docker）

### 前提条件

- Docker 20+
- Docker Compose v2+
- 服务器需安装 Claude Code CLI 或 Codex CLI（Agent 分析需要）

### 步骤

```bash
# 1. 将代码传到服务器
scp -r jarvis/ user@server:/opt/jarvis/
# 或使用 git
git clone <repo_url> /opt/jarvis

# 2. 配置环境变量
cd /opt/jarvis
cp .env.example .env
vim .env
```

编辑 `.env`：
```bash
FEISHU_APP_ID=cli_a815a841797b500b
FEISHU_APP_SECRET=your_secret_here
DATABASE_URL=sqlite+aiosqlite:///./data/jarvis.db
CODE_REPO_PATH=/opt/plaud-flutter-common   # 可选，代码仓库路径
SECRET_KEY=change-to-random-string          # 生产环境必须更改
```

```bash
# 3. 启动服务
docker compose up -d

# 4. 查看日志
docker compose logs -f backend
docker compose logs -f frontend

# 5. 验证
curl http://localhost:8000/api/health
```

### Docker Compose 架构

```
docker compose up -d
  ├── backend   (FastAPI, port 8000)
  ├── frontend  (Next.js, port 3000)
  └── redis     (Task queue, port 6379)
```

### 注意事项

**Agent CLI 挂载**：Docker 容器内需要能访问 `claude` 或 `codex` CLI。
两种方式：

```yaml
# 方式 1：挂载宿主机的 CLI（推荐）
# 在 docker-compose.yml 的 backend service 中添加：
volumes:
  - /usr/local/bin/claude:/usr/local/bin/claude:ro
  - $HOME/.claude:/root/.claude:ro

# 方式 2：在 Dockerfile 中安装
# 在 backend/Dockerfile 中添加：
RUN npm install -g @anthropic-ai/claude-code
```

---

## 三、远端服务器部署（手动）

适用于不使用 Docker 的场景。

### 3.1 系统依赖

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3.12 python3.12-venv nodejs npm redis-server

# CentOS/RHEL
sudo dnf install python3.12 nodejs npm redis
```

### 3.2 安装 Agent CLI

参见 [第五节：Agent CLI 安装](#五agent-cli-安装)

### 3.3 部署后端

```bash
cd /opt/jarvis/backend

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 创建数据目录
mkdir -p ../data ../workspaces

# 测试启动
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 生产环境：使用 systemd 管理
```

### 3.4 Systemd 服务配置

创建 `/etc/systemd/system/jarvis-backend.service`：

```ini
[Unit]
Description=Jarvis Backend API
After=network.target redis.service

[Service]
Type=simple
User=jarvis
WorkingDirectory=/opt/jarvis/backend
Environment=PATH=/opt/jarvis/backend/.venv/bin:/usr/local/bin:/usr/bin
ExecStart=/opt/jarvis/backend/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable jarvis-backend
sudo systemctl start jarvis-backend
sudo systemctl status jarvis-backend
```

### 3.5 部署前端

```bash
cd /opt/jarvis/frontend
npm install
npm run build

# 生产环境启动
npm start
# 或使用 pm2
npm install -g pm2
pm2 start npm --name "jarvis-frontend" -- start
pm2 save
```

前端 systemd 服务 `/etc/systemd/system/jarvis-frontend.service`：

```ini
[Unit]
Description=Jarvis Frontend
After=network.target jarvis-backend.service

[Service]
Type=simple
User=jarvis
WorkingDirectory=/opt/jarvis/frontend
ExecStart=/usr/bin/npm start
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 3.6 Nginx 反向代理（推荐）

```nginx
server {
    listen 80;
    server_name jarvis.your-domain.com;

    # 前端
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }

    # 后端 API（前端已有 rewrite 代理，这里做备用直连）
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_read_timeout 600s;  # Agent 分析可能耗时较长

        # SSE support
        proxy_set_header Cache-Control no-cache;
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
    }
}
```

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## 四、配置说明

### 4.1 环境变量 (.env)

| 变量 | 必填 | 说明 | 示例 |
|------|------|------|------|
| `FEISHU_APP_ID` | 是 | 飞书应用 App ID | `cli_xxx` |
| `FEISHU_APP_SECRET` | 是 | 飞书应用 App Secret | `xxx` |
| `DATABASE_URL` | 否 | 数据库连接串 | `sqlite+aiosqlite:///./data/jarvis.db` |
| `REDIS_URL` | 否 | Redis 连接串（不填则用进程内队列） | `redis://localhost:6379/0` |
| `CODE_REPO_PATH` | 否 | 代码仓库路径（用于代码感知分析） | `/opt/plaud-flutter-common` |
| `HOST` | 否 | 监听地址 | `0.0.0.0` |
| `PORT` | 否 | 监听端口 | `8000` |
| `LOG_LEVEL` | 否 | 日志级别 | `info` |
| `SECRET_KEY` | 否 | 安全密钥（生产环境必改） | 随机字符串 |

### 4.2 全局配置 (config.yaml)

`config.yaml` 控制 Agent 选择、并发、规则路由等。修改后重启后端生效。

关键配置项：

```yaml
agent:
  default: claude_code          # 默认使用的 Agent
  timeout: 300                   # 分析超时（秒）
  providers:
    claude_code:
      enabled: true
      model: claude-sonnet-4-20250514
    codex:
      enabled: false             # 按需启用
      model: o3
  routing:                       # 按问题类型路由
    flutter_crash: claude_code
    recording_missing: claude_code
    general: claude_code

concurrency:
  max_workers: 3                 # 最大并行分析数
```

### 4.3 分析规则 (backend/rules/)

规则以 Markdown + YAML front matter 格式存储，支持热加载：

```bash
# 重载规则（不需要重启服务）
curl -X POST http://localhost:8000/api/rules/reload
```

添加新规则：在 `backend/rules/` 或 `backend/rules/custom/` 下创建 `.md` 文件即可。

---

## 五、Agent CLI 安装

Jarvis 依赖外部 Agent CLI 来执行日志分析。至少需安装一个。

### 5.1 Claude Code

```bash
# 安装
npm install -g @anthropic-ai/claude-code

# 验证
claude --version

# 首次需要认证
claude
# 按提示完成 Anthropic 账号认证
```

### 5.2 Codex (OpenAI)

```bash
# 安装
npm install -g @openai/codex

# 验证
codex --version

# 设置 API Key
export OPENAI_API_KEY=sk-xxx
```

### 5.3 验证 Agent 可用性

```bash
curl http://localhost:8000/api/health/agents
```

预期返回：
```json
{
  "claude_code": { "status": "ok", "available": true, "version": "2.x.x" },
  "codex": { "status": "ok", "available": true, "version": "0.x.x" }
}
```

---

## 六、验证部署

### 6.1 健康检查

```bash
# 完整健康检查
curl http://localhost:8000/api/health

# 预期结果：
# {
#   "status": "healthy",       ← 全部 OK 则为 healthy
#   "checks": {
#     "database": { "status": "ok" },
#     "redis": { "status": "ok" },       ← 没有 Redis 则 unavailable
#     "agents": { "claude_code": { "available": true } },
#     "rules": { "status": "ok", "count": 7 }
#   }
# }
```

### 6.2 验证工单获取

```bash
curl http://localhost:8000/api/issues | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'工单总数: {d[\"stats\"][\"total\"]}')
print(f'高优先级: {d[\"stats\"][\"high_priority\"]}')
"
```

### 6.3 验证规则加载

```bash
curl http://localhost:8000/api/rules | python3 -c "
import sys, json
rules = json.load(sys.stdin)
print(f'规则数: {len(rules)}')
for r in rules:
    print(f'  {r[\"meta\"][\"id\"]}: {r[\"meta\"][\"name\"]}')
"
```

### 6.4 访问前端

打开浏览器访问 `http://<server_ip>:3000`

---

## 七、运维

### 7.1 日志查看

```bash
# Docker
docker compose logs -f backend

# Systemd
journalctl -u jarvis-backend -f
```

### 7.2 数据库

SQLite 数据文件位于 `data/jarvis.db`。

```bash
# 备份
cp data/jarvis.db data/jarvis.db.bak

# 迁移到 PostgreSQL（生产推荐）
# 修改 .env:
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/jarvis
# 安装驱动:
pip install asyncpg
```

### 7.3 工作空间清理

每次分析会在 `workspaces/` 下创建临时文件夹，可定期清理：

```bash
# 清理 7 天前的工作空间
find workspaces/ -maxdepth 1 -mtime +7 -type d -exec rm -rf {} +
```

### 7.4 更新规则

```bash
# 方式 1：编辑文件后重载
vim backend/rules/new-rule.md
curl -X POST http://localhost:8000/api/rules/reload

# 方式 2：通过 Web UI
# 访问 http://localhost:3000/rules → 选择规则 → 编辑 → 保存

# 方式 3：通过 API
curl -X POST http://localhost:8000/api/rules \
  -H "Content-Type: application/json" \
  -d '{"id":"my-rule","name":"我的规则","triggers":{"keywords":["关键词"],"priority":5},"content":"规则内容..."}'
```

### 7.5 升级

```bash
# 拉取最新代码
git pull

# 更新后端依赖
cd backend && source .venv/bin/activate && pip install -r requirements.txt

# 更新前端依赖
cd frontend && npm install && npm run build

# 重启服务
sudo systemctl restart jarvis-backend
sudo systemctl restart jarvis-frontend
# 或 Docker:
docker compose up -d --build
```

### 7.6 问题排查

| 问题 | 排查方向 |
|------|---------|
| 工单列表为空 | 检查 FEISHU_APP_ID/SECRET 是否正确 |
| Agent 分析超时 | 增大 config.yaml 中的 timeout |
| Agent 不可用 | 访问 /api/health/agents 检查 CLI 安装 |
| 数据库错误 | 检查 data/ 目录权限 |
| 前端无法连接后端 | 检查后端是否运行在 8000 端口 |
