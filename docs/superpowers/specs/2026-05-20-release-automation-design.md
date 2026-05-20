# Release 自动化设计 (Jarvis `/release` 模块)

**日期**: 2026-05-20
**作者**: sanato.zhang@plaud.ai
**状态**: v2 — Jarvis 纯编排（不读源码、不 bump 版本）

## 背景与目标

Plaud APP 发布流程目前分散在多个工具与人工操作之间。本设计在 Jarvis 平台新增 `/release` 模块，把以下两件**完全独立**的事一次性闭环：

1. **创建 release 分支**——多仓 fanout（共享一次 `mt` 调用） + 命名规范校验 + 创建后恢复 workspace 到 main + 飞书通知。
2. **触发构建**——选择 cn / global，通过 Jenkins API 触发对应 job（3 台 server 自动负载均衡）。**Jenkins 自己的 pipeline 负责 version bump + 打包 + 上传**——Jarvis 不参与。

## 范围与边界

- ✅ 涵盖：分支创建（含 workspace 状态恢复）、Jenkins 触发、状态追踪、产物下载、飞书通知、历史记录。
- ❌ 不涵盖：version bump（Jenkins pipeline 自己干）、读取源码、commit/push 业务文件、Android 自动上传 PlayStore（Jenkins 不支持）、release notes 生成、回滚发布。
- ✅ 顺带改造：`repo_updater.py` 改用 `mt` 调用（针对 mt 工作区自动检测），与 release 操作共享 workspace 文件锁。

## 关键决策

| 维度 | 选型 | 理由 |
|---|---|---|
| **多仓管理** | `mt` 工具 | 已装在本机与服务器；容器 Dockerfile 需后续加装 |
| **Workspace** | 复用 `code_repo_app` | 已被 `repo_updater` 维护，不新建；`mt` 命令从此目录 fanout |
| **创建分支基线** | `main` | 用户明确约定 |
| **Workspace 状态** | 创建后 `mt checkout main` 恢复 | 不影响 crashguard / agent_orchestrator 这些读源码的下游 |
| **Jenkins 负载均衡** | 查 3 台 `/queue/api/json` 选队列最短 | 准确够用，多 3 次轻量 HTTP |
| **构建状态追踪** | 后台 poller 每 30s 轮询 | 30min 构建不需要 SSE |
| **Artifact 下载** | 302 重定向到 Jenkins 内网 URL | 不背流量 |
| **去重 key** | `(branch, target)` 在 in-progress 状态 | 同分支 + 同 target 同时只允许一个进行中 |
| **权限** | 所有登录用户 | 因为信任所以简单 |
| **通知** | 飞书 → 触发人 + `jenkins.notify_emails` 配置（含 admin） | 复用 feishu_cli.send_message(email=...) |
| **Version bump** | **Jenkins pipeline 内部处理** | Jarvis 完全不碰源码 |
| **DB migration** | SQLAlchemy `Base.metadata.create_all` | 项目既有约定，不引入 alembic |

## 架构

```
Frontend (/release page)
   ↓ POST /api/release/branches    {branch}
   ↓ POST /api/release/builds      {branch, target, android_multi_channel_pack}
   ↓ GET  /api/release/branches    GET /api/release/builds
   ↓ GET  /api/release/builds/{id}/artifacts/{platform}  (302 → Jenkins)
Backend (FastAPI)
   ├── app/api/release.py              REST endpoints
   ├── app/services/mt_runner.py       mt subprocess + 文件锁
   ├── app/services/jenkins_client.py  Jenkins HTTP client
   └── app/workers/release_poller.py   30s 后台 poll
   ↓ subprocess mt (only for create-branch)
   ↓ HTTP (httpx) → Jenkins ×3
   ↓ feishu_cli.send_message → 飞书私信
   DB: releases / release_builds
```

## 创建分支流程

```
1. 校验分支名格式 `release/X.Y.Z_MMDD`
2. DB 预检：分支是否已存在
3. 抢 workspace 文件锁
4. mt reset --hard && mt clean -fd && mt fetch --all --prune
5. mt checkout main && mt pull origin main      # 拉到最新 main
6. mt checkout -b release/X.Y.Z_MMDD            # 多仓同时创建
7. mt push -u origin release/X.Y.Z_MMDD         # 推送
8. 各子仓 git rev-parse HEAD → repos_json       # 审计快照
9. mt checkout main                              # 恢复现场（warning-only：失败不阻塞）
10. 释放锁
11. 写 releases 表
12. 异步发飞书通知（不阻塞响应）
```

任一关键步骤失败 → 释放锁 → 500 含 stderr 节选；步骤 6/7 失败会先尝试 `mt branch -D <branch>` 清残留。

## 触发构建流程

```
1. 校验 target ∈ {cn, global}
2. DB 验 release 分支已注册
3. DB 验 (branch, target) 无 in-progress 任务
4. 查 3 台 Jenkins /queue/api/json 取队列最短
5. GET /crumbIssuer/api/json 拿 CSRF crumb（无 crumb 返回 404 时降级）
6. POST /job/<job>/buildWithParameters
   - BRANCH=release/X.Y.Z_MMDD
   - (cn) android_multi_channel_pack=true|false
7. 解析 Location header → queue_id
8. 写 release_builds 行（status=queued）
```

**Jarvis 完全不读 / 不改源码、不 commit、不 push 业务文件。** Jenkins 的 pipeline 脚本自己处理 version bump、打包、签名、上传 AppStore。

## 数据模型

### `releases` 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer PK | |
| `branch` | String UNIQUE | "release/3.2.0_1222" |
| `version` | String | "3.2.0" |
| `date_tag` | String | "1222" |
| `repos_json` | Text | 各子仓 HEAD：`[{"name":"plaud-flutter-common","commit_sha":"..."}]` |
| `created_by` | String index | 邮箱 |
| `created_at` | DateTime | |
| `status` | String | "created" / "deleted" |

### `release_builds` 表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer PK | |
| `branch` | String index | |
| `target` | String | "cn" / "global" |
| `android_multi_channel` | Boolean | cn 才生效 |
| `jenkins_server` | String | "http://10.0.52.101:8080" |
| `jenkins_job` | String | "plaud-app-publish-global" |
| `jenkins_queue_id` | Integer | POST 返回 |
| `jenkins_build_number` | Integer | poll queue 后填 |
| `jenkins_build_url` | String | |
| `status` | String index | pending / queued / running / success / failure / aborted / error |
| `started_at`, `finished_at` | DateTime | |
| `error_message` | Text | error/failure 时填 |
| `artifact_android_url`, `artifact_ios_url` | String | 成功后填 |
| `triggered_by` | String index | |
| `triggered_at` | DateTime | |

### 状态机

```
pending  ──Jenkins POST 201──▶  queued
queued   ──poll queue 出现 executable.number──▶  running
running  ──poll result==SUCCESS──▶  success
         ──result==FAILURE/UNSTABLE──▶  failure
         ──result==ABORTED──▶  aborted
*        ──Jarvis 端异常 / 超时──▶  error
```

## API

- `POST /api/release/branches` `{branch}` → 创建并 push，恢复 main，返回 Release
- `GET /api/release/branches` → 分页列表
- `GET /api/release/branches/{branch}` → 详情
- `POST /api/release/builds` `{branch, target, android_multi_channel_pack}` → 触发 Jenkins
- `GET /api/release/builds` → 分页列表（filter: branch/target/status）
- `GET /api/release/builds/{id}` → 详情
- `GET /api/release/builds/{id}/artifacts/{platform}` → 302 → Jenkins artifact URL

## Jenkins 集成

- **三台 Jenkins 各自独立账号体系**——配置里每台单独写 `user` + `token_env`，token 通过 .env 注入（如 `JENKINS_TOKEN_101 / _102 / _103`）。
- 客户端按 server URL 查对应 (user, token)，每次请求重新组装 Basic Auth header。
- 鉴权：Basic Auth，每次操作前 GET `/crumbIssuer/api/json` 取 CSRF crumb。crumb 接口 404 时降级（兼容禁用 crumb 的 Jenkins）。
- 触发：`POST {server}/job/{job}/buildWithParameters` form-data `BRANCH=...`，cn 还带 `android_multi_channel_pack=true|false`。
- 查询 build number：`GET {server}/queue/item/{queue_id}/api/json` → `executable.number` + `executable.url`。
- 查询构建状态：`GET {build_url}/api/json` → `building / result / artifacts[]`。
- Artifact URL 拼接：`{build_url}/artifact/{relativePath}`。

### 负载均衡

```python
async def pick_least_busy_server() -> str:
    # 并发 GET /queue/api/json 三台，取 items 长度最小者；
    # 单台失败给高位 sentinel（10**9），不影响整体决策。
```

## 通知

`feishu_cli.send_message(email=..., text=...)` 异步发送给：

- `created_by` / `triggered_by` 用户邮箱
- `jenkins.notify_emails` 配置里的额外抄送（含 admin）

发送失败仅 log warn，不阻塞主流程。

## mt_runner 实现要点

- `asyncio.to_thread` 包 `subprocess.run`，避免阻塞事件循环。
- 文件锁：`fcntl.flock(fd, LOCK_EX | LOCK_NB)` on `$workspace/.jarvis.lock`，spin-poll 0.2s × 60s。
- 错误：subprocess returncode != 0 → `MtRunnerError(stderr 节选)`。
- 关键封装：
  - `reset_workspace()` — `mt reset --hard && mt clean -fd && mt fetch --all --prune`
  - `checkout_main_and_pull()` — `mt checkout main && mt pull origin main`
  - `checkout_new_branch(b)` — `mt checkout -b <b>`
  - `checkout_existing_branch(b)` — `mt checkout <b>`（用于恢复 main）
  - `push_branch(b, set_upstream=True)` — `mt push -u origin <b>`
  - `get_commits()` — 各子仓 HEAD
  - `delete_local_branch(b)` — 失败回滚用

## release_poller

- 启动时机：FastAPI lifespan。`jenkins.enabled=false` 时本质 no-op（不访问 Jenkins）。
- 间隔：`jenkins.poll_interval_seconds`（默认 30s）。
- 每轮：DB 查 `status in (queued, running)` → 对每个并发查 Jenkins → 更新状态 → 终态时填 artifact URL。
- 超时兜底：`triggered_at` 距今超 `build_timeout_seconds`（默认 3600s）→ status=error。
- 出错隔离：单条更新失败 log 后跳过。
- **目前 poller 不主动发飞书**——构建终态通知未来用一个独立 hook 实现（P4，未启用）。

## 前端 UI（`/release` 页面）

单页布局：

1. **创建分支区域**：输入框 + 实时格式校验 + "创建分支"按钮 + 创建成功后显示各子仓 commit SHA。
2. **触发构建区域**：下拉选 release 分支 + radio 选 cn/global + （cn 时）`android_multi_channel_pack` checkbox + "触发构建"按钮。
3. **构建历史表格**：自动 10s polling 刷新；列：ID / 分支 / 目标 / 状态 / Jenkins server+build# / 触发人 / 耗时 / 触发时间 / 产物下载链接（成功才显示）。

## 配置

```yaml
jenkins:
  enabled: false                          # 默认关，配齐后改 true
  servers:
    - url: http://10.0.52.101:8080
      user: jarvis-bot
      token_env: JENKINS_TOKEN_101
    - url: http://10.0.52.102:8080
      user: jarvis-bot
      token_env: JENKINS_TOKEN_102
    - url: http://10.0.52.103:8080
      user: jarvis-bot
      token_env: JENKINS_TOKEN_103
  job_cn: plaud-app-publish-cn
  job_global: plaud-app-publish-global
  poll_interval_seconds: 30
  build_timeout_seconds: 3600
  mt_bin: mt
  notify_emails:
    - sanato.zhang@plaud.ai
```

## 容器变更

`backend/Dockerfile` 加装 `mt`。具体安装命令由用户提供（mt 二进制路径或 install script），暂保留为部署期 manual step。

## 错误处理与重试策略

| 错误场景 | 处理 |
|---|---|
| mt 命令非零退出 | 释放锁，返回 500 含 stderr 节选 |
| Jenkins 鉴权失败 (401) | 返回 502，message="Jenkins trigger failed: 401..." |
| Jenkins 网络超时 | 单台失败时挑下一台，3 台都失败 → 502 |
| poller 单条更新异常 | log + 跳过，下一轮重试；超过 build_timeout 置 error |
| 飞书通知失败 | log warn，不阻塞主流程 |
| 文件锁超时 60s | 返回 503 "Workspace busy" |
| 恢复 main 失败 | log warn，**不阻塞**（分支已推，业务可继续；只是 workspace 留在 release 分支上，下次操作会被 reset） |

## 测试策略

### 单元 + 集成

- `BRANCH_RE` 正则边界
- `JenkinsClient.pick_least_busy_server` mock httpx
- `JenkinsClient.pick_artifact_url` 抽取
- `mt_runner` mock subprocess
- `release` API TestClient 全链路（jenkins disabled 时的 503，无 workspace 时的 503，列表 / 查询）

### 手动验收

1. 配 `CODE_REPO_APP` 指向 mt 多仓父目录
2. 配 `jenkins.enabled=true` + `JENKINS_API_TOKEN`
3. POST `/api/release/branches`，确认三个子仓远端出现新分支
4. 确认 workspace 操作完已切回 main
5. 飞书私信到达
6. POST `/api/release/builds`，确认 Jenkins 队列有任务
7. 等 30min，构建成功后 artifact 下载链接 302 跳转

## 安全考量

- `JENKINS_API_TOKEN` 从环境变量读，不入库不入 git
- 所有路由要求登录（Feishu SSO 已就位）
- 文件锁防并发，避免 workspace 损坏
- 不存任何 Jenkins / 飞书 secrets 到 DB

## 待用户提供

- [ ] `JENKINS_TOKEN_101` / `_102` / `_103` 三个环境变量（在每台 Jenkins UI 的 "User → Configure → API Token → Add new Token" 处生成）
- [ ] `mt` 工具在容器中的安装方式（如启用容器部署）
- [ ] Jenkins job 参数名是否就是 `BRANCH` 和 `android_multi_channel_pack`
