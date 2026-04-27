# Crashguard 设计文档

**项目代号**：Crashguard
**作为 jarvis 子模块**：`backend/app/crashguard/`
**设计日期**：2026-04-27
**Owner**：sanato
**状态**：Draft，待 spec review

---

## 0. 背景与目标

### 0.1 问题定义

Plaud app（Flutter + 原生 iOS/Android 壳）周发版节奏下，崩溃数据散落在 Datadog，缺少：

1. 自动化每日盘点 — 哪些是新增、哪些回归、哪些飙升
2. 根因到 fix 的闭环 — 谁来分析、谁来出 PR
3. 跨版本去重 — Datadog 自带的 issue grouping 在符号化重传 / 代码 refactor 后会切割同一个 bug
4. 主动通知 — 工程师必须主动登 Datadog 才能看到，被动响应

### 0.2 设计目标

- 每天 07:00 与 17:00 自动跑两次完整流水线，群消息推送 Top20 崩溃
- 全新 / 回归 / 飙升三维新增判定 + crash-free 影响度排序
- AI agent 完成根因 + 复现 + 修复方案 + fix_diff 生成
- 修复方案必须基于**石锤证据 + 可行度评分**，禁止猜测
- Flutter 走完整自动 PR 闭环（Level 2 单测验证），Android/iOS 走半自动 PR（人工 ✋ 一键 approve）
- 数据持久化、跨版本去重、可追溯
- 模块强解耦 — 未来可独立拆分为单独服务

### 0.3 非目标（v1 明确不做）

- 集成测试 / Widget 测试自动复现（Level 3）
- 模拟器/真机自动复现（Level 4）
- iOS Level 2 单测验证（需 macOS runner，远期）
- 自动合入 PR（永远 draft，强制人工 review）
- Web / Desktop 端崩溃（数据源未覆盖）

---

## 1. 顶层架构 + 模块边界

### 1.1 部署形态

作为 jarvis 子模块集成，复用基础设施 ≥ 60%：

| 复用 | 自有 |
|-----|-----|
| FastAPI、SQLAlchemy、SQLite | Datadog client |
| `services/feishu_cli.py` 群消息 | stack_fingerprint dedup |
| `services/agent_orchestrator.py` agent 调度 | 三维分类（全新/回归/飙升） |
| `services/repo_updater.py` git PR 能力 | crash-free 影响度排序 |
| `db/database.py` connection pool | Flutter 单测复现器 |
| `workers/` 流水线模板 | 半自动 PR 审批端点 |

### 1.2 项目结构

```
backend/app/crashguard/
├── __init__.py                    # 仅导出 public 接口
├── README.md                      # 模块文档（架构、接口契约）
├── CLAUDE.md                      # 模块级 AI 指引（隔离约束）
├── config.py                      # 模块配置加载
├── models.py                      # crash_* 表 SQLAlchemy 定义
├── api/
│   ├── __init__.py
│   ├── crash.py                   # /api/crash/* 通用端点
│   ├── pr_approval.py             # /api/crash/approve-pr/<id>
│   └── reports.py                 # /api/crash/reports/*
├── services/
│   ├── datadog_client.py          # Datadog Error Tracking API
│   ├── dedup.py                   # stack_fingerprint 算法
│   ├── classifier.py              # 三维判定
│   ├── ranker.py                  # Top20 排序
│   ├── analyzer_router.py         # 按平台路由
│   ├── flutter_analyzer.py        # Flutter 专用 prompt + Level 2 hooks
│   ├── android_analyzer.py        # Android 专用（仅 Level 1）
│   ├── ios_analyzer.py            # iOS 专用（仅 Level 1）
│   ├── verifier.py                # Flutter 单测复现器
│   └── reporter.py                # Feishu 卡片格式化
├── workers/
│   ├── scheduler.py               # APScheduler 触发
│   └── pipeline.py                # 端到端 9 步流水线
├── agents/
│   ├── prompts.py                 # 平台分支 prompt 模板
│   └── result_schema.py           # 强约束输出契约
└── tests/
    ├── unit/
    └── integration/
```

### 1.3 与 jarvis 核心的耦合点（受控、显式、最小）

| 调用方向 | 接触面 | 解耦保证 |
|---------|-------|---------|
| crashguard → jarvis | `services/feishu_cli.py::send_message()` | 函数签名稳定，可一键替换为 webhook |
| crashguard → jarvis | `services/agent_orchestrator.py::run_agent()` | DTO 传参，可换 agent runner |
| crashguard → jarvis | `services/repo_updater.py::create_branch_pr()` | git 操作封装，可换 GitHub API |
| crashguard → jarvis | `db/database.py::get_session()` | 仅复用 connection pool，**禁止 join jarvis 表** |
| jarvis → crashguard | （v1 不做） | 未来通过 `crashguard.api.public::lookup_by_fingerprint()` |

### 1.4 解耦约束的强制机制（防腐）

**1️⃣ 自动化 lint** — `backend/.importlinter.cfg`：

```ini
[importlinter]
root_packages = app

[importlinter:contract:crashguard-isolation]
name = crashguard 模块隔离合约
type = forbidden
source_modules = app.crashguard
forbidden_modules =
    app.models
    app.workers.analysis_worker
    app.services.rule_engine
    app.api.issues
    app.api.tasks
    app.api.feedback
```

CI / pre-commit 强制跑，违反即 build fail。

**2️⃣ DB 隔离自检** — `scripts/check_crash_decoupling.py`，启动时跑：crash_* 表外键不能指向非 crash_* 表，违反则阻止启动。

**3️⃣ ADR + PR Checklist** — `docs/adr/0001-crashguard-isolation.md` 记录决策；PR 模板增加：

```markdown
- [ ] 改动了 app/crashguard/ 的，已确认未引入新的 jarvis 耦合点（参见 ADR-0001）
```

**4️⃣ 模块级 CLAUDE.md** — `backend/app/crashguard/CLAUDE.md`，AI 修改时强制读取：

```markdown
# Crashguard 模块隔离约束

⚠️ 这是独立模块，未来可能拆分为独立服务。

1. ❌ 禁止 `from app.models import ...`（除 `app.db.database.get_session`）
2. ❌ 禁止 join jarvis 表（issues/tasks/feedbacks/...）
3. ❌ 禁止把 crashguard 字段塞进 jarvis 全局配置
4. ✅ 仅允许的对外调用：feishu_cli / repo_updater / agent_orchestrator
5. 任何新增耦合点先更新 ADR-0001 并通过 lint
```

### 1.5 未来拆分预案（写入 README，不实施）

```
拆分剧本（备忘）:
1. crashguard/ 整体迁移到独立 repo
2. 替换 4 个 jarvis 函数调用 → HTTP 调用对应 jarvis API
3. DB 拆分: crash_* 表迁移到独立 SQLite
4. 部署: 独立 docker-compose service
```

---

## 2. 数据库 Schema

全部表前缀 `crash_*`，无外键指向 jarvis 表。

### 2.1 `crash_issues` — 当前状态主表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | 自增 |
| `datadog_issue_id` | TEXT UNIQUE | Datadog 侧 issue_id（**Layer 1 去重键**） |
| `stack_fingerprint` | TEXT INDEX | 我们自算的归一化指纹（**Layer 2 去重键**） |
| `title` | TEXT | 错误类型 + 顶层帧 |
| `platform` | TEXT | flutter / ios / android |
| `service` | TEXT | Datadog service tag |
| `first_seen_at` | DATETIME | issue 首次出现时间 |
| `first_seen_version` | TEXT | 首次出现的 app 版本号 |
| `last_seen_at` | DATETIME | 最近一次崩溃时间 |
| `last_seen_version` | TEXT | 最近一次崩溃的 app 版本 |
| `status` | TEXT | open / resolved_by_pr / ignored / wontfix |
| `total_events` | INTEGER | 累计崩溃次数 |
| `total_users_affected` | INTEGER | 累计影响用户数 |
| `representative_stack` | TEXT | 代表性堆栈 |
| `tags` | JSON | Datadog tags |
| `external_refs` | JSON | 关联工单 / Linear ticket（应用层 lookup） |
| `created_at` / `updated_at` | DATETIME | |

### 2.2 `crash_snapshots` — 每日时序快照

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | |
| `datadog_issue_id` | TEXT INDEX | |
| `snapshot_date` | DATE | |
| `app_version` | TEXT | 该日主导版本 |
| `events_count` | INTEGER | 当日崩溃次数 |
| `users_affected` | INTEGER | 当日影响用户数 |
| `crash_free_rate` | REAL | 当日 crash-free 用户百分比 |
| `crash_free_impact_score` | REAL | 对全局 crash-free 拖累分（**Top20 排序依据**） |
| `is_new_in_version` | BOOL | 当日该版本首次出现 |
| `is_regression` | BOOL | 是否回归 |
| `is_surge` | BOOL | 是否飙升 |
| `created_at` | DATETIME | |

`UNIQUE(datadog_issue_id, snapshot_date)` 保证当天同 issue 只 upsert 一次。

### 2.3 `crash_fingerprints` — 指纹聚合表

| 字段 | 类型 | 说明 |
|------|------|------|
| `fingerprint` | TEXT PK | hash 值 |
| `datadog_issue_ids` | JSON | 该指纹下所有 Datadog issue（跨版本漂移） |
| `first_seen_version` | TEXT | 跨所有版本里最早出现的版本 |
| `total_events_across_versions` | INTEGER | 跨版本累计 |
| `normalized_top_frames` | JSON | 归一化后的 top-5 帧（调试用） |
| `updated_at` | DATETIME | |

### 2.4 `crash_analyses` — Agent 分析结果

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | |
| `datadog_issue_id` | TEXT INDEX | |
| `analysis_run_id` | TEXT UNIQUE | UUID，对应 workspace 目录 |
| `agent_name` | TEXT | claude_code / codex |
| `triggered_by` | TEXT | scheduled / manual / regression_alert |
| `problem_type` | TEXT | 沿用 jarvis 既有分类 |
| `root_cause` | TEXT | |
| `scenario` | TEXT | 复现场景描述 |
| `key_evidence` | JSON | 石锤证据列表 |
| `reproducibility` | TEXT | reproduced / partial / unreproducible |
| `verification_method` | TEXT | static / unit_test |
| `verification_result` | TEXT | test_red_then_green / static_only / test_failed |
| `feasibility_score` | REAL | 修复方案可行度 0-1 |
| `feasibility_reasoning` | TEXT | 评分依据 |
| `fix_suggestion` | TEXT | 修复方案文档 |
| `fix_diff` | TEXT NULL | unified diff（high complexity 为 NULL） |
| `reproduction_test_path` | TEXT NULL | 测试文件相对路径 |
| `reproduction_test_code` | TEXT NULL | 测试源码 |
| `verification_log` | TEXT | 跑测试 stdout/stderr |
| `complexity_level` | TEXT | low / high |
| `confidence` | TEXT | high / medium / low |
| `agent_raw_output` | TEXT | 原始 JSON 兜底 |
| `status` | TEXT | success / failed |
| `error` | TEXT NULL | 失败原因 |
| `created_at` | DATETIME | |

### 2.5 `crash_pull_requests` — PR 追踪

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | |
| `analysis_id` | INTEGER INDEX | crash_analyses.id |
| `datadog_issue_id` | TEXT INDEX | |
| `repo` | TEXT | plaud_ai / plaud_ios / plaud_android |
| `branch_name` | TEXT | crashguard/auto-fix/<id>-<date> |
| `pr_url` | TEXT | GitHub PR 链接 |
| `pr_number` | INTEGER | |
| `pr_status` | TEXT | draft / open / merged / closed |
| `triggered_by` | TEXT | auto_verified / human_approved |
| `approved_by` | TEXT NULL | 人工审批者 open_id |
| `approved_at` | DATETIME NULL | |
| `verification_status` | TEXT | pending / crash_resolved / crash_persists |
| `verified_at` | DATETIME NULL | 合入后跟踪崩溃是否消失 |
| `created_at` | DATETIME | |

### 2.6 `crash_daily_reports` — 群消息日报记录

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PK | |
| `report_date` | DATE | |
| `report_type` | TEXT | morning / evening |
| `top_n` | INTEGER | 推送条数 |
| `new_count` / `regression_count` / `surge_count` | INTEGER | 三维计数 |
| `feishu_message_id` | TEXT | 群消息 message_id |
| `report_payload` | JSON | 完整推送内容（可重发） |
| `created_at` | DATETIME | |

`UNIQUE(report_date, report_type)` 防同日同型重发。

### 2.7 `crash_versions` — App 版本元数据

| 字段 | 类型 | 说明 |
|------|------|------|
| `version` | TEXT | |
| `platform` | TEXT | flutter / ios / android |
| `released_at` | DATETIME | 发版时间 |
| `is_active` | BOOL | 是否当前线上版本 |
| `notes` | TEXT | 备注 |

`PRIMARY KEY (version, platform)`。

### 2.8 索引策略

- `crash_issues(stack_fingerprint)` — 跨版本回归查询
- `crash_snapshots(snapshot_date, crash_free_impact_score DESC)` — Top N 排序
- `crash_snapshots(datadog_issue_id, snapshot_date)` — 单 issue 时序
- `crash_analyses(datadog_issue_id, created_at DESC)` — 拿最新分析

### 2.9 数据保留策略

- `crash_snapshots` 滚动 90 天清理
- `crash_analyses` 永久（数据量小）
- `crash_daily_reports` 永久（审计）

---

## 3. 端到端流水线

### 3.1 触发器

| 时间 | 行为 | 设计意图 |
|------|------|---------|
| **07:00** | 完整流水线 + 发日报（"昨日全量 + 凌晨增量"） | 工程师上班前看到，安排白天工作 |
| **17:00** | 完整流水线 + 发日报（"白天增量 + 当日趋势"） | 下班前对账，问题不过夜 |
| 手动 | `POST /api/crash/trigger` + UI 按钮 | 紧急排查 / 调试 / 验证修复 |
| 回归告警 | 检测到 issue 回归立即触发单 issue 分析 | 不等到下次定时器 |

### 3.2 9 步流水线

```
[1] 拉版本基线 ── version_sync.py
        从 release.yml / git tag / 内部发版表拉最新版本
        写入 crash_versions
        ↓
[2] 拉 Datadog issue 全量 ── DatadogClient.list_issues(window=24h)
        分页拉取，含 stack trace / events count / users / versions
        失败重试 3x 指数退避
        ↓
[3] 计算 stack_fingerprint ── dedup.compute_fingerprint(stack)
        归一化 → 取 top-5 帧 → SHA1
        upsert crash_fingerprints
        ↓
[4] Upsert 状态 + 写快照 ── 单事务
        crash_issues 按 datadog_issue_id upsert
        crash_snapshots 按 (issue_id, today) upsert
        ↓
[5] 三维分类 ── classifier.classify_today()
        is_new_in_version  = (first_seen_version == latest_release)
        is_regression      = (fingerprint 在最近 N 版本静默后再现)
        is_surge           = (events_count > 1.5 × prev_avg AND > 10)
        ↓
[6] Top20 排序 ── ranker.pick_top_n(date=today, n=20)
        P0: is_new_in_version OR is_regression  → 强制入选
        P1: 剩余席位按 crash_free_impact_score DESC 填满
        限制单 issue 同周不重复推送（除非 is_surge）
        ↓
[7] Agent 分析（并行，max_workers=3）
        7.1 准备 workspace: workspaces/crash_<issue_id>_<date>/
        7.2 拉源码上下文（reproducer.gather_context）
        7.3 调 agent_orchestrator.run_agent → 写 result.json
        7.4 解析: reproducibility / feasibility / complexity_level
        7.5 写 crash_analyses
        ↓
[7.5] Verification ── verifier.run(repo, test_file, diff)  ← 仅 Flutter
        7.5.1 git worktree add 临时分支
        7.5.2 写入 test_file，跑 flutter test → 期望 RED
        7.5.3 应用 fix_diff，再跑 flutter test → 期望 GREEN
        7.5.4 git worktree remove 清理
        7.5.5 verification_result = "test_red_then_green" / "static_only" / "test_failed"
        7.5.6 计算 feasibility_score，写回 crash_analyses
        ↓
[8] PR 分级（按顺序判定，命中即停）
        a) complexity_level == "high" OR fix_diff is NULL
            → 仅方案文档，不开 PR
        b) feasibility_score >= 0.7 AND verification_result == "test_red_then_green"
            → 自动 draft PR (triggered_by=auto_verified)
        c) feasibility_score in [0.5, 0.7) (含静态分析路径)
            → 半自动：写 crash_analyses，等人工 ✋ approve
        d) feasibility_score < 0.5 (含 test_failed=0.3、unreproducible=0.0)
            → 仅方案文档，不开 PR
        ↓
[9] 群消息推送 ── reporter.send_daily_report()
        组装 Feishu interactive card
        feishu_cli.send_message → 拿 message_id
        写 crash_daily_reports
```

### 3.3 石锤证据采集（Step 7.2）

```python
class ReproducerContext:
    stack_trace: str                       # Datadog 原始堆栈
    related_events: list[dict]             # 同 issue 最近 10 条事件
    user_breadcrumbs: list[str]            # 崩溃前的操作链（Datadog RUM）
    source_files: list[Path]               # 来自 CODE_REPO_*，只读
    repo_root: Path                        # 仓库根目录（明确告知 agent）
    repo_name: Literal["plaud_ai", "plaud_ios", "plaud_android"]
    git_blame: dict                        # 关键代码行 git blame
    related_logs: list[str]                # 关联 plaud .log（可选）
```

### 3.4 Agent 输出契约（强约束）

```json
{
  "problem_type": "...",
  "root_cause": "...",
  "scenario": "...",
  "key_evidence": ["证据1（行号）", "证据2"],
  "reproducibility": "reproduced | partial | unreproducible",
  "fix_suggestion": "修复方案文档",
  "fix_diff": "--- a/lib/...\n+++ b/lib/...\n@@ ...",
  "diff_target_repo": "plaud_ai",
  "diff_base_branch": "main",
  "reproduction_test_path": "test/crashguard_repro/<issue_id>_test.dart",
  "reproduction_test_code": "...",
  "complexity_level": "low | high",
  "feasibility_score": 0.0,
  "feasibility_reasoning": "...",
  "confidence": "high | medium | low"
}
```

### 3.5 Quality Gate（feasibility 计算）

```python
CONFIDENCE_NUMERIC = {"high": 1.0, "medium": 0.5, "low": 0.0}

def compute_feasibility(verification_result: str, agent_confidence: str) -> float:
    """
    verification_result ∈ {test_red_then_green, static_only, test_failed, unreproducible}
    agent_confidence ∈ {high, medium, low}
    """
    conf = CONFIDENCE_NUMERIC[agent_confidence]
    if verification_result == "test_red_then_green":
        return min(1.0, 0.7 + 0.3 * conf)            # 0.7 - 1.0
    elif verification_result == "static_only":
        return min(0.7, 0.5 + 0.2 * conf)            # 0.5 - 0.7（顶到 0.7 仍不会触发自动 PR）
    elif verification_result == "test_failed":
        return 0.3                                    # 复现测试跑了但没 red→green，明确低分
    else:                                             # unreproducible 或异常
        return 0.0
```

**PR 触发线 = 0.7 + verification == test_red_then_green**（双重门槛）。
仅静态分析最高 0.7 → 因 verification 条件不满足 → 进半自动通道，由人工把关。

### 3.6 平台覆盖矩阵

| 能力 | Flutter | Android | iOS |
|------|:------:|:-------:|:---:|
| 源码分析（Read/Grep） | ✅ MVP | ✅ MVP | ✅ MVP |
| 根因 + 场景说明 | ✅ MVP | ✅ MVP | ✅ MVP |
| 修复方案文档 | ✅ MVP | ✅ MVP | ✅ MVP |
| fix_diff 生成 | ✅ MVP | ✅ MVP | ✅ MVP |
| Level 2 单测验证 | ✅ MVP | ⏳ 阶段 2 | ⏳ 阶段 3 |
| 自动 draft PR | ✅ MVP（验证通过） | ⚠️ 半自动 | ⚠️ 半自动 |
| feasibility 上限 | 1.0 | 0.7 | 0.7 |

### 3.7 Agent 工具白名单（仅读，禁写）

```yaml
crashguard:
  agent_allowed_tools:
    - "Read"
    - "Grep"
    - "Glob"
    - "Shell(grep:*)"
    - "Shell(wc:*)"
    - "Shell(head:*)"
    - "Shell(tail:*)"
    - "Shell(sed:*)"
    - "Shell(awk:*)"
    - "Shell(cat:*)"
    # 显式不开放: Write, Edit, Shell(git:*), Shell(gh:*)
```

### 3.8 PR 提交安全栏（services/repo_updater.py 唯一执刀手）

- 始终 draft，绝不合入
- 分支命名：`crashguard/auto-fix/<datadog_issue_id>-<date>`
- PR body 强制包含：root cause / evidence / feasibility score / "AI 生成，需人工 review" 声明
- 标题前缀 `[crashguard]`
- 冷却机制：同一 fingerprint 7 天内已提过 PR 不再提
- 验证：`git apply --check` + target_repo 白名单 + base_branch 非受保护

### 3.9 Stack Fingerprint 算法（dedup.py）

```
归一化步骤:
1. 取 stack trace 前 5 帧
2. 剥离行号: foo.dart:123 → foo.dart
3. 剥离匿名闭包/生成代码: <anonymous>, _$xxxx, closure_at_
4. 剥离版本号路径: pub-cache/.../package-1.2.3/ → package-*
5. 业务包名优先: 剥离 SDK/framework 噪音帧 (dart:async, Flutter framework, libsystem)
6. 剩余规范化文本拼接 → SHA1

Layer 2 联动:
- 命中相同 stack_fingerprint 但 datadog_issue_id 不同 → 跨版本同 bug
- 自动 link 到 crash_fingerprints.datadog_issue_ids
```

### 3.10 整体超时预算

```
Step 1-6 (数据层):    ≤ 5 min
Step 7+7.5 (并发分析): ≤ 30 min（20 issues / 3 worker，单任务 ≤ 5min）
Step 8 (PR):          ≤ 5 min
Step 9 (推送):        ≤ 1 min
─────────────────────
总预算:                ≤ 45 min
```

### 3.11 半自动 PR 闭环（Android/iOS）

```
agent 生成 fix_diff（android/ios，仅静态分析）
        ↓
feasibility_score ≤ 0.7（无 Level 2 验证）
        ↓
进入日报"📋 修复方案待人工 approve"分区
        ↓
日报每条带 ✋ 一键提交 draft PR 按钮
        ↓
工程师点击 → POST /api/crash/approve-pr/<analysis_id>
        ↓
后端校验（Feishu open_id 鉴权 → 必须 admin 角色）
        ↓
repo_updater 创建 draft PR
        ↓
回写 crash_pull_requests（pr_status=draft, triggered_by=human_approved）
```

安全栏：
- 一键 PR 接口必须鉴权（admin 白名单）
- diff 持久化在 `crash_analyses.fix_diff`，jarvis Web UI 可预览
- 同 issue 同方案 30 天内只能 approve 一次

---

## 4. 群消息日报格式（Feishu Interactive Card）

### 4.1 设计原则

- 可扫读：5 秒决定深入与否
- 可操作：每条 issue 自带 action button
- 分区清晰：四类 issue 独立分区
- 早晚报区分：早报偏概览 + 计划，晚报偏对账 + 趋势

### 4.2 卡片结构（视觉示意）

```
╔═══════════════════════════════════════════════════════╗
║  🌅 Crashguard 早报 · 2026-04-27 07:00                 ║
║  当前版本: v1.4.7 (本周一发布)                          ║
╠═══════════════════════════════════════════════════════╣
║  📊 健康度概览                                          ║
║  Crash-free 用户: 99.42% (↓ 0.08% vs 昨日)             ║
║  24h 崩溃事件: 1,247 (↑ 12%)                           ║
║  影响用户: 89 (↑ 5)                                    ║
╠═══════════════════════════════════════════════════════╣
║  🆕 全新崩溃 · 3 个                                     ║
║  ────────────────────────────                          ║
║  [#1] NullPointerException @ AudioPlayer.play          ║
║       📱 plaud_ai (Flutter) · v1.4.7 首发              ║
║       👥 23 用户 · 📊 145 次 · 🔥 影响分 8.2           ║
║       💡 根因: 释放后调用 → buffer null check 缺失      ║
║       ✅ 已自动开 draft PR (Level 2 验证通过)           ║
║       [🔗 PR #1234] [🔍 Datadog] [详情]                ║
║                                                        ║
║  [#2] EXC_BAD_ACCESS @ AudioEngine.swift:78            ║
║       📱 plaud_ios · v1.4.7 首发                       ║
║       👥 12 用户 · 📊 67 次                            ║
║       💡 根因: weak ref 提前释放                        ║
║       ✋ 修复方案待 approve (静态, feasibility 0.65)    ║
║       [✋ 一键提交 PR] [🔍 Datadog] [📄 查看 diff]      ║
╠═══════════════════════════════════════════════════════╣
║  🔁 回归崩溃 · 1 个                                     ║
╠═══════════════════════════════════════════════════════╣
║  📈 飙升崩溃 · 2 个                                     ║
╠═══════════════════════════════════════════════════════╣
║  📊 高频遗留 · 14 个 (折叠)                             ║
║  📌 已开 PR 跟踪中: 3 个                               ║
║  🔇 已忽略: 5 个                                       ║
╠═══════════════════════════════════════════════════════╣
║  ⚙️ 流水线健康                                          ║
║  分析成功: 18/20 · 失败: 2 (超时 1, agent 错误 1)      ║
║  自动 PR: 3 · 待 approve: 7 · 仅方案: 8                ║
║  [📊 完整报告] [⚙️ 流水线日志]                          ║
╚═══════════════════════════════════════════════════════╝
```

### 4.3 早报 vs 晚报差异

| 元素 | 🌅 早报 (07:00) | 🌃 晚报 (17:00) |
|-----|----------------|------------------|
| Header | "早报" + 当前版本 | "晚报" + 今日趋势箭头 |
| 健康度对比基线 | vs 昨日 | vs 早报 |
| Section 排序 | 全新 → 回归 → 飙升 → 高频 | **新增/变化** → 全新 → 回归 → 飙升 → 高频 |
| 流水线健康 | 早班分析数据 | 早+晚累计 |
| 推送规则 | 全量发 | **变化驱动**：若 Top20 与早报完全一致且无新增 → 缩水成"📊 今日无新增崩溃"短消息 |

### 4.4 Action Button 状态机

| 状态 | 按钮组合 | 后端动作 |
|-----|---------|---------|
| ✅ 已自动 PR | `[🔗 PR链接]` `[🔍 Datadog]` | 仅展示 |
| ✋ 待 approve | `[✋ 一键提交 PR]` `[🔍 Datadog]` `[📄 查看 diff]` | `POST /api/crash/approve-pr/<id>` |
| 🔍 仅方案 | `[📄 查看方案]` `[🔍 Datadog]` `[👨‍💻 分配工程师]` | 跳详情页 |
| ⚠️ 分析失败 | `[🔄 重试]` `[🔍 Datadog]` | `POST /api/crash/retry/<id>` |

一键 PR 鉴权：Feishu callback 带 `open_id` → lookup jarvis users 表 → 必须 admin 角色。

### 4.5 长度控制

- Top 6 完整展示（全新 3 + 回归 1 + 飙升 2）
- 高频遗留 14 个 → 折叠 collapsible block
- Top20 完整可点开
- 超过 20 个 → 仍只发 Top20，剩余在 `/crashes` 看

### 4.6 消息持久化

- Feishu API 返回 `message_id` → 写 `crash_daily_reports.feishu_message_id`
- 后续状态变更（如 PR 合入）→ 在原消息 thread reply，不发新卡片

### 4.7 群配置

```yaml
# config.yaml
crashguard:
  enabled: true                          # kill switch
  pr_enabled: true                       # PR 开关
  feishu_enabled: true                   # 群消息开关
  feishu:
    target_chat_id: "oc_xxxx"
    admin_open_ids: ["ou_xxx"]
    morning_cron: "0 7 * * *"
    evening_cron: "0 17 * * *"
    max_top_n: 20
  datadog:
    site: "datadoghq.com"                # API 站点
    # api_key / app_key 通过 .env: CRASHGUARD_DATADOG_API_KEY / CRASHGUARD_DATADOG_APP_KEY
  thresholds:
    surge_multiplier: 1.5                # 飙升判定阈值
    surge_min_events: 10                 # 飙升最小事件数
    regression_silent_versions: 3        # 回归判定的静默版本数
    feasibility_pr_threshold: 0.7        # 自动 PR 触发线
```

### 4.8 兜底降级

| 场景 | 行为 |
|-----|------|
| Feishu API 失败 | 重试 2x → payload 落库 + admin 私信告警 + Web UI 可重发 |
| 卡片字段 schema 升级 | 新字段 lazy 渲染，缺失显示"-"，不阻塞 |
| Top20 为空 | 短消息"📊 今日无 Top 崩溃，crash-free 99.5%+，继续保持" |

---

## 5. 错误处理 + 测试 + 灰度

### 5.1 错误分类矩阵

| 等级 | 场景 | 处置 | 阻断? |
|-----|------|------|:----:|
| **P0** | Datadog API 完全不可达 | 重试 3x → 失败则全流水线终止 + admin 告警 | ✅ |
| **P1** | 单 issue agent 分析失败/超时 | 跳过，写 `crash_analyses(status=failed)`，日报"⚠️ 分析失败"分区 | ❌ |
| **P1** | verifier Flutter 测试失败（环境问题）| 降级 `verification_method=static`，feasibility 上限 0.7 | ❌ |
| **P2** | repo_updater 创建 PR 失败 | 降级为半自动 PR | ❌ |
| **P2** | Feishu 推送失败 | 重试 2x → payload 落库 + admin 告警 | ❌ |
| **P3** | stack_fingerprint 计算异常 | 跳过 dedup，仅用 datadog_issue_id | ❌ |

### 5.2 重试与幂等

| 场景 | 策略 |
|-----|------|
| Datadog 限流 (429) | 指数退避 1s/2s/4s，最多 3x；429 单独熔断（10min 内 5 次 → 暂停 30min） |
| Agent 调用 | 沿用 jarvis timeout=600 + max_turns=50；超时算 P1 |
| DB 写冲突 | UNIQUE(issue_id, date) → upsert ON CONFLICT；天然幂等 |
| 流水线重跑 | 任意时间点重跑同日 → 数据收敛；PR 表防重靠 7 天冷却 |

### 5.3 监控埋点

```
crashguard.pipeline.start         {trigger=cron|manual, run_id}
crashguard.step.{1..9}.duration   每步耗时
crashguard.issue.processed        {issue_id, platform, success/failed, duration}
crashguard.pr.created             {pr_url, issue_id, triggered_by}
crashguard.report.sent            {chat_id, message_id, top_n}
crashguard.error                  {step, error_type, traceback}
```

告警（admin 私信，复用 `feishu_cli.send_message` 单聊）：
- P0 立即推
- P1 聚合：单次流水线失败 issue 数 > 5 → 推汇总
- 流水线完成后无论成败 → 健康度私信

Health endpoint：`GET /api/crash/health` 返回最近一次流水线 status + 数据新鲜度。

### 5.4 测试策略

#### 单元测试（pytest，覆盖率 ≥ 70%）

| 模块 | 重点 |
|-----|------|
| `dedup.py` | 归一化各种栈格式、跨版本同 bug 命中 |
| `classifier.py` | 三维判定边界条件（首版/N 版静默/恰好 50%） |
| `ranker.py` | Top20 优先级 + 单 issue 同周不重复 |
| `verifier.py` | red→green 流程：mock subprocess |
| `analyzer_router.py` | 平台识别 + repo 路由正确 |

#### 集成测试

| 场景 | 验证 |
|-----|------|
| 全流水线 mock Datadog → 端到端 | 9 步全跑通，DB 写入正确，假日报生成 |
| Datadog 429 限流 | 重试 + 熔断生效 |
| agent 超时 | P1 处理，不阻断后续 issue |
| Feishu 推送失败 | payload 落库 + 重发可用 |

#### 手动验收（上线前）

- 历史 Datadog 真数据跑 dry-run（写 DB 但**不**发群消息、**不**提 PR）
- 人工 review 至少 5 个 Top issue 的 agent 输出
- 人工 review 至少 1 个 fix_diff（apply 到本地 worktree 看是否合理）
- 半自动 PR 一键按钮端到端走一遍

### 5.5 灰度上线（owner 闭环）

```
阶段 1 (Day 1-3): 影子模式
  cron 跑流水线，仅 admin 私信，不发群消息，不开 PR
  目的：观察数据质量、agent 输出准确度

阶段 2 (Day 4-7): 半灰度
  群消息开（仅 5 个 issue 缩减版）
  自动 PR 开（仅 Flutter Level 2 验证通过）
  半自动 PR 关（仍只私信 admin）
  目的：观察工程师反馈

阶段 3 (Day 8+): 全量
  Top20 完整推送
  自动 PR + 半自动 PR 全开
  1 周后 retro 调阈值
```

### 5.6 紧急回滚

`crashguard:` 段三个 kill switch：

```yaml
crashguard:
  enabled: true
  pr_enabled: true
  feishu_enabled: true
```

支持 `/api/rules/reload` 同款热加载（**复用** jarvis 现有模式）→ 无需重启。

---

## 6. 实施路线图

### 阶段 1（MVP，预计 2 周）

- 数据层：Datadog client、dedup、classifier、ranker
- DB：7 张 crash_* 表 + Alembic migration + 隔离自检
- 流水线：9 步 + APScheduler 双触发（07:00 / 17:00）
- Agent：三平台 analyzer + Flutter verifier (Level 2)
- PR：自动（Flutter）+ 半自动（Android/iOS）
- 群消息：Feishu interactive card + 早晚报
- Web UI：`/crashes` 列表页 + 详情页（极简）
- 灰度三阶段（影子 → 半灰度 → 全量）

### 阶段 2（+1 周，可选）

- Android Level 2 单测验证（Robolectric / JUnit）
- Top20 阈值调优（根据 Day 8+ 数据）
- crash → 工单关联反查（如有需求）

### 阶段 3（远期，可选）

- iOS Level 2（macOS GitHub Actions runner）
- Web/Desktop 端崩溃接入（如有数据源）
- 跨数据源融合（Firebase Crashlytics 兜底）

---

## 7. Open Questions（待 review 时确认）

1. **CODE_REPO_IOS / CODE_REPO_ANDROID 是否已有仓库？** 设计假设这两个仓库存在并可被 jarvis 容器挂载
2. **Datadog API 站点**：是 `datadoghq.com` 还是 `datadoghq.eu`？决定 `crashguard.datadog.site` 默认值
3. **Feishu 群 chat_id**：上线前需要提供
4. **Admin open_ids 白名单**：上线前需要提供
5. **数据源延迟容忍**：Datadog 聚合延迟 ~30min，07:00 跑能否覆盖到 06:30 的崩溃？需上线后实测
6. **plaud_android / plaud_ios 仓库的测试基础设施**：阶段 2 评估时再确认

---

## 8. 关键约束清单（review checklist）

- ✅ 模块强解耦，未来可独立拆分
- ✅ 4 个对外耦合点显式声明，import-linter 强制
- ✅ DB 表前缀 `crash_*`，无 jarvis 表外键
- ✅ Agent 只读源码，禁止写
- ✅ PR 永远 draft，禁止合入
- ✅ PR 必须基于石锤证据 + 可行度评分（≥ 0.7 才进自动 PR）
- ✅ Flutter 走 Level 2 单测验证 (red→green) 才能自动 PR
- ✅ Android/iOS 走半自动 PR（人工 ✋ approve）
- ✅ 三层 kill switch + 灰度三阶段 + 紧急回滚
