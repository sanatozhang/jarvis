# 深度分析（Deep Analysis）按钮 — 设计

- 日期：2026-06-12
- 状态：已与用户确认设计，待写实现计划
- 相关：[[project_windowing_recent_policy]]（窗口口径）、本次会话对 rec27zFZSkfFpN / fb_853230bbde 两单失败的根因排查

## 1. 背景与目标

工单分析失败后，详情页目前只有「重试」按钮（用相同 pipeline 重跑，仍受 windowing 约束）。
对于「窗口选错 / 证据被截断 / 时间锚点不准」导致的失败（本次排查的 ① rec27zFZSkfFpN
全量日志超时、② fb_853230bbde 证据不在窗口），单纯重试无效。

**目标**：在失败工单加一个「深度分析」按钮，跳过 windowing，把**完整原始日志**交给 AI
自由探索（grep）并给出结果；同时**精确限制 AI 读取日志的次数**，避免在超大日志上无限
grep 失控/超时。

## 2. 范围

In scope：
- 后端：`deep_analysis` 标志贯穿 `TaskCreate → _run_task → run_analysis`；深度模式跳过
  windowing、放宽 agent 配置、注入读取上限 hook；结果另存一条分析记录。
- 前端：失败态加「深度分析」按钮；`createTask` 增加 `deepAnalysis` 可选参数。

Out of scope（本设计不做）：
- ② 的 `system_failure` 语义细分（失败 vs 信息不足）—— 单独评估。
- `coverage<0.5` 全量回退改造 —— 单独评估。
- 非 claude_code agent 的读取上限（codex/claude_api 无 hook 机制）。

## 3. 关键决策（已与用户确认）

| 决策点 | 结论 |
|--------|------|
| 日志输入 | **完整原始日志，不截断**（深度模式跳过 windowing） |
| 限流机制 | **PreToolUse hook 精确数读取次数**，超限 deny |
| 上限数值 | **30 次日志读取** / `max_turns=40` 兜底（可后续调） |
| 结果落库 | **新增一条 AnalysisRecord**，保留原失败记录，详情页可对比 |
| 超时 | **1200s**（复用 `task_timeout_large`） |
| 按钮门控 | **所有人可点**（与「重试」一致） |
| 执行 agent | **强制 claude_code**（唯一支持 hook 的 agent） |
| 发布门禁 | **不豁免**：深度跑完仍 `system_failure` 照常如实标记（已给全量日志，无"截断"借口） |
| 接口 | **不新开接口**，TaskCreate 加 `deep_analysis` 标志，复用整条 pipeline |

## 4. 架构与数据流

```
前端「深度分析」按钮（失败态）
  → createTask(issueId, deepAnalysis=true)
  → POST /api/tasks  { issue_id, deep_analysis: true }
  → TaskCreate.deep_analysis: bool = False        # schema 新增
  → _run_task(deep_analysis=...)
  → run_analysis(deep_analysis=...)
       ├─ _run_context_condensation(deep=True)
       │     → 跳过 window_log_files，workspace_log_paths = 完整解密日志
       │     → logs/ 直接放全量
       └─ agent 配置（deep 档）
             → agent_override = "claude_code"（强制）
             → max_turns=40, timeout=1200
             → 注入 PreToolUse 读取上限 hook
  → save_analysis(新记录, agent_type="claude_code_deep")
```

## 5. 组件设计

### 5.1 限流 hook（核心，防失控）

- 复用 `claude_code.py` 现有 `.claude/settings.json` 注入机制（已有 Stop hook）。
- 深度模式额外写入 **PreToolUse hook**：
  - 触发条件：工具为 `Grep` / `Read`，或 `Bash` 命令含 `logs/`（grep/cat/rg/head/tail 等）。
  - 行为：读 `.claude/.log_read_count`，+1 持久化；超过 30 → 返回 deny + reason
    「已达日志读取上限（30 次），请立即基于已有证据写出 `output/result.json`，不要再 grep」。
  - 跨轮计数文件与现有 `.stop_block_count` 同目录、同 fail-open 原则（hook 自身异常一律放行）。
- `max_turns=40` 作总轮数兜底；任一触发即停。
- **实现期需验证**：PreToolUse hook 的 deny 返回 JSON 契约（`hookSpecificOutput.permissionDecision=deny`
  vs 旧 `decision:block`）依赖所装 claude CLI 版本，实现时先用当前 CLI 验证格式，不写死。

### 5.2 windowing 跳过

- `_run_context_condensation` 增加 `deep: bool` 入参；deep=True 时直接返回原始
  `log_paths`（不调用 `window_log_files`），并在 `windowing_meta` 标 `{"deep_mode": true, "windowed": false}`。

### 5.3 agent 配置（deep 档）

- 深度模式强制 `agent_override="claude_code"`。
- `max_turns=40`、`timeout=1200`（复用 `task_timeout_large` 语义，避免按钮自己超时——
  ① 正是全量日志 600s 超时）。

### 5.4 提示词（base.py 新增 deep 分支）

- 告知 agent：「你拿到的是**完整未截断日志**（logs/），有 **30 次读取预算**，自由 grep 定位
  根因，但要尽快收敛并**必须**写 `output/result.json`；超预算会被强制停止」。

### 5.5 结果落库

- 沿用现有 `save_analysis`（本就先存再判失败），深度结果以 `agent_type="claude_code_deep"`
  新增一条 `AnalysisRecord`，原失败记录保留。
- 详情页「多次分析」列表天然展示两条，可对比。

### 5.6 前端

- 失败态按钮区（`page.tsx:~1079` / `tracking/page.tsx:~547` 的「重试」旁）加「深度分析」按钮。
- `api.ts` `createTask` 增加可选 `deepAnalysis?: boolean`，POST body 带 `deep_analysis`。
- i18n 新增 key「深度分析」/「深度分析中...」。
- 进度/结果复用现有 SSE（`subscribeTaskProgress`）。

## 6. 错误处理

- hook 自身异常 → fail-open（放行），绝不因计数脚本卡死 agent。
- 深度模式仍超时（1200s）→ 走现有 timeout salvage 路径，结果如实标记。
- 深度结果 `system_failure=True` → 不豁免，照现有发布门禁判定。

## 7. 测试

- `test_log_windower` / worker：`deep=True` 时跳过窗口、`workspace_log_paths==全量`。
- hook 计数脚本单测：第 31 次读取被 deny，前 30 次放行；计数文件跨调用累加；异常 fail-open。
- TaskCreate→worker：`deep_analysis` 标志正确贯穿，且 deep 强制 claude_code。
- 前端：失败态出现按钮，点击带 `deep_analysis=true`。

## 8. 待办 / 风险

- PreToolUse hook JSON 契约随 claude 版本（实现期验证）。
- 完整 42MB 日志 + 30 次读取在 1200s 内能否稳定收敛——上线后观测；必要时调上限/超时。
- 仅 claude_code 支持读取上限；若未来 deep 想支持其他 agent 需另设机制。
