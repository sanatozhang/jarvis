# 工单 Token / 费用追踪 + Analytics 趋势 — 设计 Spec

- 日期：2026-06-19
- 模块：工单分析（非 crashguard）
- 状态：待 review

## 1. 背景与目标

### 问题
当前工单分析跑完后，不记录消耗的 token 与费用。运营/工程师无法知道单个工单（含每次追问）烧了多少钱，也无法在 analytics 看整体趋势与成本。

### 目标
1. **每个工单/每次追问独立记录** token 用量与费用（USD）。
2. **结果页**每张分析卡片（首次 + 每条追问各自）显示「本次：N tokens · $X」。
3. **Analytics tab** 在 Daily Trend 展示费用与 token 趋势：双线双轴折线图（工单数 + token），费用进 hover tooltip + 顶部「本期总计」汇总卡；无数据的天不显示。

### Non-goals
- 不回填历史分析（只统计上线后；历史显示「—」）。
- 不做预算/告警/限额。
- 不改 agent 路由策略。
- 不做按用户/按规则的成本分摊报表（未来可加）。

## 2. 关键现状（探查结论，作为设计依据）

- **主力 agent 是 `claude_code` CLI**：config.yaml 路由表所有问题类型→`claude_code`。当前 `backend/app/agents/claude_code.py` 用 `--output-format text`，拿不到 usage。Claude Code CLI 2.1.169 的 `--output-format json` 直接返回 `{result, usage:{input_tokens,output_tokens,cache_read_input_tokens,cache_creation_input_tokens}, total_cost_usd, duration_ms, num_turns}` —— **token + 费用 CLI 直接给**。
- **`claude_api` agent（少用）**：已把逐轮 usage 写 `workspace/{task_id}/output/agent_trace.jsonl`，`GET /api/tasks/{task_id}/trace` 已汇总。
- **`codex` CLI（少用）**：usage 输出不确定，best-effort。
- **condenser（L1.5 预提取，haiku，API）**：`backend/app/services/context_condenser.py` 各 `_call_*` 拿 HTTP 响应，响应含 `usage`（仅 token，无 cost）。经 `_run_context_condensation`（`backend/app/workers/analysis_worker.py:715`）上抛。
- **数据模型**：`AnalysisRecord`（表 `analyses`，`backend/app/db/database.py:80`）。**一行 = 一次分析 or 一次追问**（追问 = 新 task = 新行，`followup_question` 非空，同 `issue_id`）→ 追问天然独立计费。当前无 token/cost 字段。增量列迁移模式见 `database.py:387` 附近的 `ensure_columns` 列表。
- **结果页**：`frontend/src/components/AnalysisResultView.tsx` 已按 task 逐条渲染（首次 + 每条追问），已有仅对 claude_api 生效的 `AgentTraceBlock`。
- **Analytics**：`GET /api/analytics/dashboard?days=N` → `db.get_analytics`（`database.py:1532` 附近）按天 group by 聚合，**只返回有数据的天（不补零）**。前端 `frontend/src/app/analytics/page.tsx` Daily Trend 为纯 CSS 横条（无图表库），同页已有 SVG 自绘折线图（问题分类趋势）含 `buildPath` 平滑曲线可复用。

## 3. 决策（已与用户确认）

| # | 决策 | 选择 |
|---|------|------|
| 1 | 「每工单费用」口径 | **agent + condenser 预提取**（真实总成本） |
| 2 | 历史数据 | **只统计今后**（历史显示「—」） |
| 3 | Daily Trend 图 | **双线双轴**：左轴工单数 + 右轴 token；费用进 hover tooltip + 顶部「本期总 tokens · $」汇总卡 |

## 4. 设计

### 4.1 数据模型（`analyses` 表新增列，全部 nullable）

| 列 | 类型 | 用途 |
|---|---|---|
| `total_tokens` | INTEGER | (agent + condenser) 的 input+output+cache 之和，供 analytics 按天 SUM |
| `total_cost_usd` | FLOAT | agent_cost + condenser_cost，供 SUM 与结果页 |
| `usage_json` | TEXT | 拆分明细 JSON：`{agent:{input,output,cache_read,cache_creation,cost_usd,source}, condenser:{input,output,cost_usd,model}}` |
| `cost_source` | VARCHAR(16) | `cli_reported`（claude_code）/ `computed`（API 路径按定价表算）/ `partial`（codex 等缺 usage） |

迁移：在 `ensure_columns` 列表加 4 行（参照既有 `("deleted","BOOLEAN","0")` 模式，SQLite 安全增量）。

### 4.2 Token / 费用捕获（四路）

1. **`claude_code`（`agents/claude_code.py`）**：命令 `--output-format text` → `json`。解析信封：取 `result` 字段作为正文（**等价于原 stdout 文本，行为不变**），抽 `usage` + `total_cost_usd` + `duration_ms`，挂到 agent 返回结构。**Fallback**：若 stdout 非合法 JSON（CLI 版本差异/异常），退回「整段当文本」旧逻辑，usage 置空、`cost_source=partial`。新增单测覆盖三种情形。
2. **`condenser`（`context_condenser.py`）**：`CondensationResult` 增 `usage` 字段；`_call_anthropic`（及其余 `_call_*`）从响应 `usage` 填入；`_run_context_condensation` 返回 dict 增 `usage` key，上抛到 worker。
3. **`claude_api`（`agents/claude_api.py`）**：已有 trace usage，汇总后挂到返回结构（与 claude_code 统一形状）。
4. **`codex`（`agents/codex.py`）**：best-effort；拿不到 → usage 空、`cost_source=partial`、前端显示「—」。

### 4.3 定价

- `claude_code`：直接用 CLI 的 `total_cost_usd`，**不查定价表**。
- API 路径（condenser haiku、claude_api）：新增 config `pricing:` 段（`model → {input, output, cache_read, cache_write} USD/Mtok`），按 claude-api 当前定价播种；`cost = Σ token × 单价`。
- 工单 `total_cost_usd = agent_cost + condenser_cost`。
- 新增 `backend/app/services/cost.py`（纯函数：`compute_cost(model, usage) -> float`，读 pricing config），便于单测。

### 4.4 聚合与持久化（`analysis_worker.py`）

分析结束、`save_analysis` 前：
1. 取 agent usage/cost（claude_code 直接给 cost；API 路径用 `cost.py` 算）。
2. 取 condenser usage（来自 `_run_context_condensation` 返回），用 `cost.py` 算 condenser cost。
3. 组装 `usage_json` 明细 + 计算 `total_tokens` / `total_cost_usd` / `cost_source`，写入该 task 的 `AnalysisRecord`。

### 4.5 结果页（`AnalysisResultView.tsx`）

- 每张分析卡片（首次 + 每条追问）新增一行：**「本次：1,234 tokens · $0.05」**；hover/展开看 agent vs condenser 拆分。
- 缺数据（历史 / codex partial）显示「—」。
- 数据来源改为持久化字段（claude_code 也能显示），逐步弱化只读 trace 的 `AgentTraceBlock`（保留兼容）。
- 链路：`AnalysisResult` schema（`backend/app/models/schemas.py`）+ `/api/tasks/{task_id}/result` + `/api/local/{issue_id}/analyses` 返回新字段；`frontend/src/lib/api.ts` 的 `AnalysisResult` 类型扩展。

### 4.6 Analytics

- **后端 `db.get_analytics`**：daily 聚合增 `SUM(total_tokens)`、`SUM(total_cost_usd)`（按 `analyses.created_at` 的日期）；顶层增本期总计 `total_tokens` / `total_cost_usd`。沿用「只返回有数据的天」。
- **前端 Daily Trend（`analytics/page.tsx`）**：CSS 横条 → SVG 双轴折线图（复用同页 `buildPath`）。
  - 左轴：工单数；右轴：token 消耗；两条线不同色。
  - hover tooltip：`日期 · N 工单 · X tok · $Y`。
  - 顶部汇总卡：「本期总 tokens · $」。
  - 无数据的天不显示（后端天然满足）。

## 5. 测试

- 后端单测：
  - `claude_code` json 信封解析（有 usage / 无 usage / 畸形→fallback 当文本）。
  - `cost.compute_cost`（各模型、含 cache 单价、未知模型回退）。
  - condenser usage 上抛进 worker 并落库。
  - `get_analytics` daily 含 tokens/cost 聚合 + 本期总计；无数据天不出现。
  - `AnalysisRecord` 新列迁移与读写。
- 现有 crashguard / 工单 / analytics 测试保持绿（注意：`test_download_logs_decrypted_takes_priority_over_raw` 为**预存在失败**，与本功能无关）。

## 6. 风险与缓解

1. **`claude_code` 改 json 输出是最敏感处**——必须保证 `result` 正文提取与原 text 完全一致 + fallback + 测试。先在测试机（100）跑真实工单验证正文无回归再上 102。
2. **codex token 可能始终缺失** → 显示「—」、`cost_source=partial`，不阻塞整体。
3. **定价表需人工维护**（config 注释标注来源与更新日期）；claude_code 主路径用 CLI cost 不受影响。
4. **condenser 多 provider**（anthropic/gemini/openai/claude_cli）usage 字段名不同——逐 provider 适配，缺失则该段 cost 记 0 + 标注。

## 7. 未来（out of scope）
- 按用户/规则/问题类型的成本分摊报表。
- 预算告警 / 月度成本卡。
- 历史回填（仅 claude_api 旧 workspace 可回填，收益低）。
