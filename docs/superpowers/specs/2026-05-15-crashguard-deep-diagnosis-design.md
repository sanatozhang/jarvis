# Crashguard 深度诊断系统设计文档

**日期**: 2026-05-15  
**状态**: 已审阅  
**背景**: 现有 auto-PR pipeline 分析准确率偏低，AI 单次分析上下文有限，对卡顿/ANR 等复杂问题缺乏有效诊断手段。本设计在现有系统上引入两阶段诊断架构，提升根因分析质量。

---

## 1. 问题陈述

### 现状痛点

1. **单次分析，上下文固定**：现有 Analyzer 拿到固定的 5 层上下文（堆栈 + Datadog enrichment + 源码导航 + 路径预解析 + 分布数据），AI 无法主动决定"我还需要查什么"
2. **输出格式强迫 AI 给结论**：prompt 要求直接输出 `fix_diff`，导致 AI 在证据不足时也倾向于"猜一个修复方案"，而不是诚实说"我不确定"
3. **卡顿/ANR 类问题无专项路径**：堆栈只告诉你"卡在哪"，不告诉你"为什么卡"；需要运行时性能数据，现有架构无法获取
4. **无法利用历史修复经验**：类似 crash 被修过的历史 `fix_diff` 没有被复用
5. **git 历史盲区**：AI 不知道"是哪次提交引入了这个 bug"

### 目标

- AI 能主动调度工具收集所需上下文（代码 + 运行时数据 + git 历史 + 历史 crash）
- 输出多假设诊断报告，而非强制给出单一修复方案
- 对"数据不足"的 crash 给出结构化监控采集建议
- 人工确认假设后，Phase 2 生成高质量 PR
- 高置信度（≥0.9 单假设）场景支持全自动快车道

---

## 2. 整体架构

### 两阶段 + 三条数据流

```
【流 1 — 主路径，数据充分】
Issue → Phase 1: 深度诊断 Agent → 多假设报告
     → 人工在前端选择/确认假设
     → Phase 2: 修复 Agent（现有 analyzer + pr_drafter）→ PR

【流 2 — 数据不足路径】
Issue → Phase 1 → "数据不足"结论 → 监控采集方案（埋点代码 + Datadog query）
     → 工程师人工部署 → 手动触发 Phase 1 重跑（带新数据）

【流 3 — 高置信快车道】
Issue → Phase 1 → 单假设 + confidence ≥ 0.9 + data_gaps 为空
     → auto_proceed_to_fix = true → 直接进 Phase 2（无需人工确认）
```

### 与现有系统的兼容性原则

- 现有"分析"按钮（UI）继续触发旧的单次 Analyzer，不受影响
- `analyze_tick` cron 继续跑现有逻辑；Phase 1 仅由人工触发（或未来可扩展为高优先级自动触发）
- 旧 `crash_analyses` 数据 `phase=NULL` 等同于 `"fix"`，前端向后兼容
- Phase 2 复用现有全部 14 道质量闸，不降低安全标准

---

## 3. Phase 1：深度诊断 Agent

### 3.1 新文件：`services/deep_analyzer.py`

职责：
- 构建 Phase 1 prompt（与现有 `_PROMPT_TEMPLATE` 完全分离）
- 生成 workspace + `tools/` 目录（含 helper 脚本）
- 调用 Claude Code agent（超时 1800s，可配置）
- 解析 `output/diagnosis.json`
- 持久化到 `crash_analyses`（`phase="diagnosis"`）
- 判断是否触发快车道（`auto_proceed_to_fix`）

**`crash_type` 判定逻辑**（在调用 agent 前由 Python 代码预判，注入 prompt）：
- `anr`：Datadog `@error.type` 含 "ANR" 或 issue title 含 "ANR" / "Application Not Responding"
- `freeze`：title 含 "freeze" / "卡顿" / "hang" / "Watchdog"
- `oom`：title 含 "OOM" / "OutOfMemory" / "low memory"
- `native_crash`：堆栈含 `SIGSEGV` / `SIGABRT` / `EXC_BAD_ACCESS`
- 默认：`crash`

### 3.2 工具注册表（Tools Registry）

不引入新 MCP 基础设施。在 agent workspace 的 `tools/` 目录里放 Python helper 脚本，Prompt 里告知 AI 通过 Bash 调用。

| 脚本 | 功能 | 底层调用 |
|------|------|---------|
| `tools/datadog_query.py --dql "<query>"` | 任意 Datadog DQL 查询 | 现有 `DatadogClient` |
| `tools/git_blame.py --file <path> --line <n>` | git blame 单行 | `git blame -L n,n <file>` |
| `tools/git_pickaxe.py --keyword <kw>` | 搜引入时机 | `git log -S <keyword> --oneline` |
| `tools/find_similar.py --fingerprint <fp>` | 历史相似 crash + fix_diff | 查 `crash_analyses` DB |
| `tools/get_session.py --session-id <id>` | 完整 RUM session 事件流 | Datadog RUM API |

每个脚本输出 JSON，AI 解析后决定下一步调查方向。

### 3.3 Phase 1 Prompt 核心原则（与现有 prompt 的关键差异）

```
目标：调查并形成假设，不要急着给修复代码。

强制约束：
1. 必须使用至少 2 个工具调用收集证据后才能下结论
2. 必须给出 1-5 个假设，每个假设必须有具体证据
3. 对于 ANR/freeze 类型，必须查 Datadog 帧率/性能数据
4. 如果证据不足以支持任何假设（confidence < 0.5），必须给出 data_gaps
5. 不得编造证据；宁可说"不确定"也不猜测

ANR/freeze 专项调查（crash_type=anr|freeze 时强制执行）：
- 检查主线程调用栈是否含 IO/网络/锁等待
- 查询 tools/datadog_query.py 获取同 session 帧率数据
- 检查是否存在跨线程数据竞争
- 建议 Timeline.startSync() / Performance.mark() 埋点位置
```

### 3.4 `diagnosis.json` 输出结构

```json
{
  "crash_type": "crash | anr | freeze | oom | native_crash",
  "investigation_log": [
    "读取 lib/services/config_loader.dart:42",
    "git_pickaxe 搜索 'readFile' → 发现 commit abc123 (2026-04-01) 引入",
    "datadog_query: 查询 crash session 的帧率数据 → 崩溃前 500ms 帧率降至 8fps"
  ],
  "hypotheses": [
    {
      "id": "h1",
      "title": "主线程 IO 阻塞（ConfigLoader.readFile）",
      "evidence": [
        "堆栈第3帧: ConfigLoader.readFile() on main thread",
        "git blame: 该行由 commit abc123 (张三, 2026-04-01) 引入",
        "Datadog: 100% 触发于弱网环境（WiFi off）"
      ],
      "confidence": 0.85,
      "fix_direction": "将 readFile 移至 background isolate，使用 compute() 或 Isolate.spawn()",
      "code_pointers": ["lib/services/config_loader.dart:42"],
      "can_fix_now": true,
      "complexity": "simple"
    },
    {
      "id": "h2",
      "title": "flutter_inappwebview dispose 时死锁",
      "evidence": [
        "堆栈含 InAppWebView.dispose() 连续两帧",
        "find_similar: 类似指纹的 crash 在 2026-03-15 被修过（升级到 6.1.0 解决）"
      ],
      "confidence": 0.40,
      "fix_direction": "升级 flutter_inappwebview 到 ≥6.1.0",
      "code_pointers": ["pubspec.yaml:flutter_inappwebview"],
      "can_fix_now": true,
      "complexity": "simple"
    }
  ],
  "data_gaps": [
    {
      "description": "不确定 readFile 是被哪个用户操作触发的（点击了什么按钮）",
      "collection_method": "在 ConfigLoader.load() 入口加 Timeline 日志",
      "instrumentation_code": "Timeline.startSync('config_load', arguments: {'source': caller});",
      "datadog_query": "SELECT @usr.action.name FROM rum_events WHERE @error.id = 'xxx' LIMIT 100"
    }
  ],
  "overall_confidence": 0.72,
  "recommended_hypothesis": "h1",
  "auto_proceed_to_fix": false
}
```

**`auto_proceed_to_fix = true` 条件（全部满足）**：
- `hypotheses` 只有 1 条
- `hypotheses[0].confidence >= 0.9`
- `hypotheses[0].can_fix_now = true`
- `data_gaps` 为空列表

---

## 4. Phase 2：修复 Agent（现有增强）

### 4.1 变更最小化

Phase 2 基本复用现有 `analyzer.py` + `pr_drafter.py`，仅在 prompt 上下文中追加：

```
## 已确认的根因假设（Phase 1 深度诊断结论）

- **假设 ID**: h1
- **标题**: 主线程 IO 阻塞（ConfigLoader.readFile）
- **置信度**: 0.85
- **修复方向**: 将 readFile 移至 background isolate
- **定位**: lib/services/config_loader.dart:42
- **调查依据**:
  - 堆栈第3帧: ConfigLoader.readFile() on main thread
  - git blame: 由 commit abc123 引入
  - Datadog: 100% 触发于弱网环境

请基于以上确认的假设直接生成 fix_diff，不需要重新分析根因。
```

现有 14 道质量闸全部保留，标准不降低。

---

## 5. 数据模型变更

### 5.1 `crash_analyses` 表新增列（增量迁移）

```python
# migrations.py ensure_columns() 追加
("phase", "TEXT DEFAULT 'fix'"),              # "diagnosis" | "fix"
("crash_type", "TEXT DEFAULT ''"),            # crash|anr|freeze|oom|native_crash
("hypotheses", "TEXT DEFAULT '[]'"),          # JSON: List[Hypothesis]
("data_gaps", "TEXT DEFAULT '[]'"),           # JSON: List[DataGap]
("confirmed_hypothesis_id", "TEXT DEFAULT ''"),
("investigation_log", "TEXT DEFAULT '[]'"),   # JSON: List[str]
("parent_diagnosis_run_id", "TEXT DEFAULT ''"), # Phase2 行 → Phase1 run_id
```

旧数据 `phase=NULL` 等同于 `"fix"`，向后兼容。

---

## 6. API 变更

### 6.1 新增端点（3 个）

```
POST /api/crash/issues/{id}/deep-analyze
  → 触发 Phase 1 深度诊断（异步）
  → 返回: {"run_id": "uuid"}
  → 若该 issue 6h 内已有 diagnosis phase 成功记录，返回已有 run_id（dedup）

POST /api/crash/analyses/{run_id}/confirm-hypothesis
  Body: {"hypothesis_id": "h1"}
  → 写入 confirmed_hypothesis_id
  → 触发 Phase 2（异步），返回 {"phase2_run_id": "uuid"}

POST /api/crash/analyses/{run_id}/mark-data-needed
  Body: {"note": "已安排埋点，预计2天后重跑"}
  → 更新状态，前端显示"等待数据中"
```

### 6.2 现有端点不变

`POST /api/crash/analyze/{id}`（UI 重新分析按钮）继续触发现有单次 Analyzer，不受影响。

---

## 7. 前端变更

### 7.1 Issue 详情页新增"深度诊断" Tab

```
┌─────────────────────────────────────────────────┐
│  [现有 Tab: 分析结果]  [新 Tab: 深度诊断]           │
└─────────────────────────────────────────────────┘

深度诊断 Tab 内容：

1. 触发按钮（未运行时）：
   [🔍 启动深度诊断]（预计 15-30 分钟）

2. 运行中状态：
   ⏳ AI 正在调查... 已用时 3m12s
   调查日志（实时滚动，折叠）

3. 完成后——假设列表：
   ┌─────────────────────────────────────────┐
   │ 假设 1  ████████░░ 85%  [推荐]           │
   │ 主线程 IO 阻塞（ConfigLoader.readFile）   │
   │ 证据: 堆栈第3帧 / commit abc123 引入 / 弱网100% │
   │ 修复方向: 移至 background isolate        │
   │ [✓ 确认此假设 → 生成修复 PR]             │
   ├─────────────────────────────────────────┤
   │ 假设 2  ████░░░░░░ 40%                  │
   │ flutter_inappwebview 死锁               │
   │ [✓ 确认此假设 → 生成修复 PR]             │
   └─────────────────────────────────────────┘

4. 数据缺口（如有）：
   ⚠️ 不确定触发路径 → 建议埋点
   [查看监控方案]  [标记"已安排监控，等待数据"]
```

---

## 8. 配置项（`config.yaml crashguard:` 段）

```yaml
crashguard:
  # Phase 1 深度诊断
  deep_analysis_enabled: true
  deep_analysis_timeout_seconds: 1800    # 30 分钟
  deep_analysis_dedup_hours: 6           # 6h 内不重复跑
  deep_analysis_auto_proceed_threshold: 0.9  # 快车道置信度门槛
```

---

## 9. 实现优先级

| 优先级 | 工作项 | 预估 |
|--------|--------|------|
| P0 | `deep_analyzer.py` + Phase 1 prompt + `diagnosis.json` 解析 | 3天 |
| P0 | DB 迁移（新增列）+ 3 个新 API 端点 | 1天 |
| P0 | 工具注册表 5 个 helper 脚本（datadog_query / git_blame / git_pickaxe / find_similar / get_session） | 2天 |
| P1 | Phase 2 prompt 追加"已确认假设"上下文 | 0.5天 |
| P1 | 前端深度诊断 Tab + 假设确认交互 | 2天 |
| P2 | 快车道自动触发逻辑（auto_proceed_to_fix） | 1天 |
| P2 | ANR/freeze 专项调查路径增强 | 1天 |

**总估算：约 10-11 个工作日（2 周 sprint）**

---

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Phase 1 耗时过长（30 分钟）影响用户体验 | 前端实时显示调查日志（SSE 推送）；异步执行；前端轮询状态自动刷新 |
| AI 工具调用失败（Datadog API 限流 / git 命令超时） | 每个 helper 脚本有独立超时和 fallback；失败只记录到 investigation_log，不阻断主流程 |
| `diagnosis.json` 未写入（和现有 `result.json not found` 同款问题） | 复用现有 retry 机制；Phase 1 结束后自动 retry 一次 |
| 历史相似 crash 查询无结果（早期 DB 数据少） | `find_similar.py` 返回空时 AI 跳过该工具，不阻断 |
