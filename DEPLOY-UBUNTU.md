# Jarvis — Ubuntu 部署清单

> 拉通文档：deploy.sh 已自带 Linux/systemd 分支，本文件只列 Ubuntu-only 前置 + 易踩坑项。

底层逻辑：mac 用 colima + brew，Ubuntu 用 dockerd + systemd。runtime 完全等价；不同点只在第一次装 docker 和首次给容器登录 Claude。

---

## 1. 系统前置

```bash
# 1.1 装 docker 引擎 + compose 插件（官方源 22.04+）
sudo apt update
sudo apt install -y \
    apt-transport-https ca-certificates curl gnupg lsb-release
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# 1.2 把当前用户加 docker 组（免每次 sudo）
sudo usermod -aG docker $USER
newgrp docker        # 当前 shell 立刻生效，或 logout 重登

# 1.3 装 sshpass / git（jarvis 内部脚本会用）
sudo apt install -y sshpass git
```

## 2. 拉代码 + 配置

```bash
cd /opt   # 或任何持久目录（避免 /tmp / 家目录权限混乱）
sudo git clone git@github.com:Plaud-AI/jarvis.git
sudo chown -R $USER:$USER jarvis
cd jarvis
cp .env.example .env
vim .env
```

### `.env` 必填项（Ubuntu 容易漏）

```bash
# === 飞书必填 ===
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

# === Datadog 必填 ===
CRASHGUARD_DATADOG_API_KEY=xxx
CRASHGUARD_DATADOG_APP_KEY=xxx

# === Crashguard 链接对外可达 IP（必填！）===
# Ubuntu 容器内 _autodetect_frontend_base_url 会拿 docker0 bridge IP
# （172.17.0.x，外网不可达）。必须显式覆盖：
CRASHGUARD_FRONTEND_BASE_URL=http://<ubuntu-host-ip>:3000

# 三平台仓库路径（按需）
CODE_REPO_ANDROID=/opt/repos/plaud-android
CODE_REPO_IOS=/opt/repos/plaud-ios
CODE_REPO_APP=/opt/repos/plaud_ai
```

> ⚠️ 如果忘了设 `CRASHGUARD_FRONTEND_BASE_URL`，后端会启动失败 / 飞书消息里链接变 `http://CONFIGURE_CRASHGUARD_FRONTEND_BASE_URL:3000` ——见 `backend/app/crashguard/config.py::_autodetect_frontend_base_url`。

## 3. 一键启动

```bash
./deploy.sh setup
```

`setup` 会自动：
- `dc up -d` 起服务
- 写 `/etc/systemd/system/jarvis.service`（机器重启自动拉起）
- `systemctl enable docker.service`

## 4. 首次部署后必做：容器内登录 Claude

```bash
# 进容器 Claude CLI 登录（凭证持久化在 named volume `claude-auth`）
docker compose exec -it backend claude login
# 验证
docker compose exec backend claude config list
```

不做这一步 → crashguard 自动 PR 的 implementation agent 报 "claude: 401 unauthorized" → 12 闸门 G1/G5 没机会跑。

## 5. （可选）跨机迁移 Claude 凭证

named volume `claude-auth` 不能跨机直接拷，只能各机各登一次。如果有强需求：

```bash
# 在已登录机器
docker run --rm -v jarvis_claude-auth:/data -v $(pwd):/backup alpine \
    tar -czf /backup/claude-auth.tar.gz -C /data .
# 复制 claude-auth.tar.gz 到目标 Ubuntu，导回
docker volume create jarvis_claude-auth
docker run --rm -v jarvis_claude-auth:/data -v $(pwd):/backup alpine \
    tar -xzf /backup/claude-auth.tar.gz -C /data
```

## 6. 验证健康

```bash
# 后端
curl http://localhost:8000/api/crash/health
# 期望：{"module":"crashguard","enabled":true,"datadog_configured":true,...}

# 前端
curl http://localhost:3000

# systemd
sudo systemctl status jarvis.service
sudo systemctl status docker.service
```

## 7. 日常运维

```bash
./deploy.sh logs        # 看日志
./deploy.sh restart
./deploy.sh update      # git pull + 重建 + image prune
./deploy.sh status
```

## 8. 易踩坑速查

| 现象 | 根因 | 解 |
|------|------|-----|
| `permission denied while trying to connect to the Docker daemon socket` | 当前用户没加 `docker` 组 | `sudo usermod -aG docker $USER && newgrp docker` |
| 后端 `Restarting (3)` 死循环 | `.env` 缺 `CRASHGUARD_DATADOG_API_KEY` 或 `FEISHU_APP_ID` | 补齐 `.env` 后 `dc restart backend` |
| 飞书链接里出现 `172.17.0.x` 死链 | `CRASHGUARD_FRONTEND_BASE_URL` 没设 / 设错 | 设为 `http://<对外可达 IP>:3000` |
| crashguard 早报延后 8 小时 | 容器默认 UTC | docker-compose.yml 已设 `TZ=Asia/Shanghai`，重启即生效 |
| Build 异常慢（122 MB apt 包重下） | Dockerfile 改了 apt-get 行触发 layer 失效 | 正常现象，新加 apt 包推荐塞独立 RUN layer |
| `docker image` 越攒越多吃磁盘 | dangling images 没清 | `./deploy.sh update` 已带 `image prune`；手动 `docker image prune -f` |
| `gh pr create` 报 401 | 容器内 gh 未登录 | `docker compose exec backend gh auth login` |

## 9. 与 mac 部署的差异（速查）

| 维度 | macOS (102) | Ubuntu |
|------|------------|--------|
| Docker daemon | colima（brew services 自启） | dockerd（systemd 自启） |
| auto-start | LaunchAgent (`brew services start colima`) | `jarvis.service` systemd unit |
| 首次装 docker | `brew install colima docker-compose` | `apt install docker-ce docker-compose-plugin` |
| Claude CLI 凭证 | 容器 named volume 持久化 | 同（一次性 `claude login`） |
| 出口 IP | 自动 socket connect 探测 | 容器内拿 bridge IP，**必须显式 .env** |
| 备份 | `data/` + `workspaces/` 直接 rsync | 同 |

---

## 10. Smoke 测验收清单（Ubuntu 部署后）

```bash
# 12 道 PR 质量闸门是否生效
docker compose exec backend python3 -c "
from app.crashguard.config import get_crashguard_settings
s = get_crashguard_settings()
for k in dir(s):
    if k.startswith('gate_') or k.startswith('pr_'):
        print(f'{k}: {getattr(s, k)}')
"

# 数据库迁移检查
docker compose exec backend python3 -m scripts.check_crash_decoupling

# 全量单测
docker compose exec backend python3 -m pytest tests/crashguard/ -q
```

期望：`193+ passed`，无 `failed`。
