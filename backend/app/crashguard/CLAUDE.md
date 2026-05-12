# Crashguard 后端模块

崩溃自动化分析 + 自动开 draft PR 子模块。**独立模块，未来可能拆分为独立服务**。

前端文档见 `frontend/src/app/crashguard/CLAUDE.md`。

---

## ⚠️ 隔离合约（硬约束）

### 禁止项

1. ❌ `from app.models import ...`（仅允许 `app.db.database.get_session`）
2. ❌ `from app.workers.analysis_worker import ...`
3. ❌ `from app.services.rule_engine import ...`
4. ❌ `from app.api.{issues,tasks,feedback} import ...`
5. ❌ SQL join 到非 `crash_*` 表（`issues` / `tasks` / `feedbacks` 等）
6. ❌ 把 crashguard 字段塞进 jarvis 全局配置（独立的 `crashguard:` 段）

### 允许的对外耦合点（仅这 4 个）

| 函数 | 用途 |
|------|------|
| `app.services.feishu_cli.send_message` | 群消息 / 私聊推送 |
| `app.services.repo_updater.create_branch_pr` | Git PR（**强制 `--draft`**，严禁 `gh pr merge` / `git merge` / `gh pr ready`） |
| `app.services.agent_orchestrator.run_agent` | agent 调度 |
| `app.db.database.get_session` | 共用 connection pool |

### 防腐机制（违反硬阻断）

- `backend/.importlinter` — `forbidden_modules` 白名单，CI / pre-commit 跑 `lint-imports`
- `backend/scripts/check_crash_decoupling.py` — 启动时跑，检查 crash_* 表外键纯净度
- ADR：`docs/adr/0001-crashguard-isolation.md`

### 新增耦合点流程

1. 改 `docs/adr/0001-crashguard-isolation.md` 记录决策
2. 加白名单到 `backend/.importlinter`
3. PR 描述里说明耦合点 + 必要性
4. CI lint 通过

---

## 入口与文件位置

| 关注点 | 路径 |
|--------|------|
| 模块根 | `backend/app/crashguard/` |
| API（单 router，prefix `/api/crash`） | `api/crash.py` |
| 7 张 DB 表（`crash_*` 前缀） | `models.py` + `migrations.py`（增量列在 `ensure_columns`） |
| 数据流水线 Step 1-6 | `workers/pipeline.py::run_data_phase` |
| AI 分析（Step 7+） | `services/analyzer.py` / `services/batch_analyzer.py` |
| 早晚报 + 自动分析 cron tick | `workers/scheduler.py`（极简 cron 解析） |
| 启动 warmup + 周期 pipeline | `workers/warmup.py` |
| Top20 排序 / 三维分类 / 指纹去重 | `services/{ranker,classifier,dedup}.py` |
| 日报构建（Feishu interactive card） | `services/daily_report.py` + `services/feishu_card.py` |
| 自动 / 半自动 draft PR + GitHub 状态同步 | `services/{pr_drafter,pr_sync}.py` |
| Datadog client（双路：fatal / non_fatal） | `services/datadog_client.py` |
| 「线上最新版本」+「用户量最大版本」 | `services/version_util.py` |
| 解耦自检（启动时跑） | `backend/scripts/check_crash_decoupling.py` |
| 单元测试 | `backend/tests/crashguard/` |

## 主要 API

| Method | Path | 用途 |
|--------|------|------|
| `POST` | `/api/crash/trigger` | 手动跑数据阶段 Step 1-6 |
| `POST` | `/api/crash/warmup` | 手动触发启动 warmup（含全量分析） |
| `GET`  | `/api/crash/top` | Top N issue 列表 |
| `GET`  | `/api/crash/issues/{id}` | issue 详情 |
| `PATCH`| `/api/crash/issues/{id}` | 状态变更（open / investigating / resolved_by_pr / ignored / wontfix） |
| `POST` | `/api/crash/analyze/{id}` | 单 issue AI 分析（UI 重新分析**强制重跑**绕过 dedup） |
| `GET`  | `/api/crash/analyses/{run_id}` | 分析结果 |
| `POST` | `/api/crash/issues/{id}/followup` | 追问 |
| `POST` | `/api/crash/batch-analyze` | 批量分析 |
| `POST` | `/api/crash/reports/run-now` | 立即出日报 |
| `POST` | `/api/crash/approve-pr/{analysis_id}` | Android/iOS 半自动 PR 一键 approve |
| `GET`  | `/api/crash/auto-pr-queue` | auto-PR 队列 |
| `GET`  | `/api/crash/pull-requests` | PR 列表 |
| `GET`  | `/api/crash/latest-release` | 「线上最新版本」+「用户量最大版本」（按平台 + source 标注） |
| `GET`  | `/api/crash/health` | 健康探针（**不受 enabled kill switch 拦截**，前端用它探测） |

## 配置（env > yaml > defaults）

- env 前缀 `CRASHGUARD_`（如 `CRASHGUARD_DATADOG_API_KEY`、`CRASHGUARD_REPO_PATH_FLUTTER`）
- yaml 顶层 `crashguard:` 段（`config.yaml`）
- Pydantic 模型 + yaml 映射在 `config.py`
- 三层 kill switch：`enabled` / `pr_enabled` / `feishu_enabled`
- 多实例兜底：`scheduler_enabled`（只有该机器跑 cron）
- 默认 cron：
  - 早报 `0 7 * * *` / 晚报 `0 17 * * *`
  - 周期 pipeline `0 */4 * * *`
  - PR 状态同步 `*/15 * * * *`
  - AI 分析 tick `*/5 * * * *`（每 tick 1 个 issue，防 timeout）
- 容器内必须设 `TZ=Asia/Shanghai`（否则早报延后 8h，见 commit `6bb8f81`）

## 关键设计要点（不可推导的语义）

- **双路 Datadog query**：`datadog_query_fatal`（crash + ANR + App Hang）与 `datadog_query_nonfatal`（业务捕获异常）分开拉，互不挤压 Top 100 配额
- **stack_fingerprint 跨版本去重**：Datadog 自带 grouping 在符号化重传后会切割同一 bug；本模块按归一化 top-5 帧 SHA1 做 Layer 2 dedup
- **「线上最新版本」口径**（`version_util.py::resolve_effective_latest_release`）：`config.current_release.{flutter,android,ios}` 覆盖 > 按崩溃数据派生（版本累计 events ≥ `latest_version_min_events`，默认 300，再取 semver 最大）> 空字符串
- **「用户量最大版本」口径**（`version_util.py::derive_top_user_version_from_crashes` + `datadog_client.py::top_user_version_by_platform`）：24h Datadog RUM `cardinality(@session.id)` group by (`@os.name`, `@application.version`)，**session 维度代理 user 维度**（Plaud RUM SDK 未调 setUser，`@usr.id` 几乎全空；24h 内同一 user 通常 1-3 session，相关性极高）。失败回落 `crash_issues.top_app_version × total_events` 加权聚合（**不用 `total_users_affected`，那个字段全 0，是已知 data hole，见 `models.py:71` 注释**）。前端 source 字段标 `datadog_rum` / `crash_issues_fallback` / `unknown`
- **每 issue 的 RUM 分布缓存**：`crash_issues.top_os` / `top_device` / `top_app_version` 由 `services/distribution_prewarmer.py` 在 analyzer 运行时刷新，格式 `"3.16.0-634 (60%), 3.15.1-631 (30%)"`
- **AI 分析去重**：自动入口（warmup/cron/batch）`analysis_dedup_hours` 内复用既有 success 分析；UI 重新分析按钮强制重跑
- **PR 安全栏**：始终 `--draft`，title 前缀 `[crashguard][DRAFT]`，分支 `crashguard/auto-fix/<issue_id>-<date>`，同 fingerprint `pr_dedup_days` 内不再开 PR
- **早晚报防重发**：`crash_daily_reports` 上 `UNIQUE(report_date, report_type)`；scheduler 自身用 `_last_fired` 做分钟级幂等

## 开发

```bash
cd backend
pytest tests/crashguard/ -v              # 单测
lint-imports                             # 隔离合约 lint
python -m scripts.check_crash_decoupling # DB 外键自检
```

## 定时任务全图（运营对照）

7 个 cron + 1 个启动一次性任务，全部走 `crash_job_heartbeats` 表心跳记录，前端 `/crashguard/jobs` 可视化。

| # | 任务（job_name） | Cron 默认 | 触发条件 | 关键阈值 | kill switch |
|---|------|----------|---------|----------|-------------|
| 1 | `core_metric` 核心指标告警 | `*/10 * * * *` | 当前 10min crash-free % vs 前 1h 加权均值 | `change_threshold_pp=0.3` pp / `min_sessions=100` / `platforms="android,ios"` | `core_metric_enabled` |
| 2 | `analyze_tick` AI 分析 tick | `*/5 * * * *` | 今日 attention pool 未 success 的 issue | `analyze_max_per_tick=1` / `analysis_dedup_hours=6` | `enabled` |
| 3 | `hourly_alert` 小时级告警 (SHoW-3h) | `5 */3 * * *` | 过去 3h fatal events vs 上周同 3h 块 | `growth_threshold_pct=10`%/`min_baseline_events=20`/`min_sessions=60`/`max_items=10` | `hourly_alert_enabled` |
| 4 | `pr_sync` PR 状态同步 | `*/30 * * * *` | DB 内 draft/open PR 拉 GitHub 现态 | 无阈值 | `enabled` |
| 5 | `pipeline` 数据 pipeline | `0 */4 * * *` | 全量拉 Datadog → snapshot + issue upsert + auto-analyze + auto-PR | `datadog_window_hours=24`/`pr_dedup_days=30`/`feasibility_pr_threshold=0.7` | `enabled` |
| 6 | `morning_daily` 日报 | `0 7 * * *` | 昨日 24h 总览，SHoW-24h 基线 | `daily_surge_threshold=+10%`/`daily_drop_threshold=-10%`/`daily_attention_min_events=100` | `feishu_enabled` |
| 7 | `evening_daily` 速报 | `0 17 * * *` | 日内 10h 增量，SHoW-Nh 基线 | 同上 + `evening_window_hours=10`，DB fallback baseline 强制清空 | `feishu_enabled` |
| ✱ | `warmup` 启动一次性 | 无（启动后延后 N 秒） | 重启后补一遍 pipeline + auto-analyze | `warmup_on_startup=true` | `enabled` |

### 可观测性闭环（治本，不靠人盯）

| 层 | 实现 | 位置 |
|----|------|------|
| 数据底座 | `crash_job_heartbeats` 表：每 tick 写一行（job_name/fired_at/status/duration_ms/summary/error） | `models.py::CrashJobHeartbeat` |
| 通用包装器 | `record_heartbeat(job_name)` async context manager，自动捕异常 → status=failed | `services/job_heartbeat.py` |
| 状态 API | `GET /api/crash/jobs/status` 返回每任务的 cron/上次/下次/连续失败数/健康度 | `api/crash.py` |
| 历史 API | `GET /api/crash/jobs/{job_name}/heartbeats?limit=50` | 同上 |
| 前端页面 | `/crashguard/jobs` 表格，每 30s 自动刷新；超期/连续失败红色高亮 | `frontend/src/app/crashguard/jobs/page.tsx` |
| 健康度判定 | `stale`（last_success_at 超过 2× 预期间隔）/ `failing`（连续 ≥3 次失败）/ `degraded`（近 50 次中 ≥10 次失败）/ `ok` | `api/crash.py::jobs_status` |
| 失败告警（待落） | 任一任务 `health` ∈ (`failing`, `stale`) → 飞书告警（下一 sprint） | TBD |

### 多实例去重 + kill switch

| 层 | 机制 |
|----|------|
| 进程级 | `_last_fired` dict 同分钟不重跑 |
| 实例级 | `scheduler_enabled` 标志位（多机部署只让一台跑 cron） |
| DB 级 | `UNIQUE(report_date, report_type)` / `UNIQUE(hour_utc)` / `UNIQUE(window_start)` |

```yaml
enabled: true              # 总开关
pr_enabled: true           # PR 创建总开关
feishu_enabled: true       # 飞书消息开关
scheduler_enabled: true    # 该实例是否跑 cron
```

### 容器内必备 env

```bash
TZ=Asia/Shanghai             # 否则早报延迟 8h
CRASHGUARD_DATADOG_API_KEY   # 必填
CRASHGUARD_DATADOG_APP_KEY   # 必填
CRASHGUARD_FRONTEND_BASE_URL # Docker 必填，否则拿 bridge IP
```

## 未来拆分预案

1. `backend/app/crashguard/` 整体迁移到独立 repo
2. 替换 4 个 jarvis 函数调用 → HTTP 调用对应 jarvis API
3. `crash_*` 表迁移到独立 SQLite / PG
4. 部署：独立 docker-compose service

详见 ADR-0001。
