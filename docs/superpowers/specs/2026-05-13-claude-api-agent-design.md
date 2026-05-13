# Claude API Agent 切换方案设计

**Date**: 2026-05-13
**Author**: sanato + AI
**Status**: Draft → Pending Review

---

## 1. 背景与目标

### 1.1 现状痛点

工单分析当前依赖 `claude` CLI 子进程：

| 痛点 | 表现 |
|---|---|
| 容器化部署难 | macOS Mach-O 二进制无法挂到 Linux 容器；必须 Dockerfile 里 `npm install -g @anthropic-ai/claude-code` |
| 登录态持久化 | 需要 `claude-auth` named volume + 首次部署 `docker compose exec backend claude login` |
| 进程模型重 | 每个并发分析 spawn 一个 Node.js 进程 ~150MB；3 并发即 500MB |
| 黑盒 | 失败时只有 stdout buffer，无法定位"挂在哪一步" |
| 错误识别脆弱 | 靠 grep stderr 字符串识别限流/超时（`claude_code.py:67-87`） |
| stdout buffer 易丢 | 超时 SIGKILL 后 dump 不可靠（已加 dump 逻辑兜底但仍有遗漏） |

### 1.2 目标

1. 新增 `ClaudeApiAgent`：通过公司 Vertex AI 代理（`http://34.216.169.232:30001/vertex`）直连 Anthropic API
2. **保留** `ClaudeCodeAgent`（CLI）作为回滚后手
3. Settings 页面新增"调用方式"开关，运行时可切换 API/CLI
4. L1.5 `context_condenser` 同步迁移，与主 Agent 共享同一开关
5. 引入 Agent Trace 观测层（jsonl + 详情页可折叠展示块），失败可定位

### 1.3 非目标

- 不修改 prompt 结构（`BaseAgent.build_prompt_with_meta` 不动）
- 不修改 result.json 解析逻辑（`BaseAgent.parse_result` 不动）
- 不调整并发上限（`max_workers=3` 不变）
- 不重构 `CodexAgent`
- 不迁移 DB schema（除新增可选 jsonl 文件路径）

---

## 2. 范围与约束

### 2.1 部署环境

- macOS（开发）+ Linux（生产容器）
- Python 3.11 + FastAPI
- Vertex 代理仅在公司 VPN 内可达；生产容器已在 VPN 网段内

### 2.2 认证

代理使用**自定义鉴权**（非标准 GCP ADC）：
- 请求头 `x-api-key: <ANTHROPIC_API_KEY>`
- `ANTHROPIC_VERTEX_PROJECT_ID=dummy`
- `CLAUDE_CODE_SKIP_VERTEX_AUTH=1`（CLI 等价行为：跳过 google-auth）

### 2.3 兼容协议

代理对外暴露 Vertex 格式包装的 Anthropic Messages API。已知端点：
- `POST /vertex/v1/messages` （推测，需 Spike 验证）

---

## 3. 架构概览

```
┌────────────────────────────────────────────────────┐
│ AgentOrchestrator.run_analysis(...)                │
│   ├─ select_agent(rule_type)                       │
│   │   ↓ 读 settings.agent.default                  │
│   │   ↓                                             │
│   │   ├──→ ClaudeApiAgent      ← 新增（默认）       │
│   │   ├──→ ClaudeCodeAgent     ← 保留（回滚）       │
│   │   └──→ CodexAgent          ← 保留              │
│   └─ agent.analyze(workspace, prompt)              │
└────────────────────────────────────────────────────┘
                       │
                       ↓
┌────────────────────────────────────────────────────┐
│ ClaudeApiAgent                                     │
│   ├─ AnthropicHttpClient（自管 httpx.AsyncClient）  │
│   ├─ ToolRegistry                                  │
│   │   ├─ read_file                                 │
│   │   ├─ write_file                                │
│   │   ├─ grep                                      │
│   │   └─ glob                                      │
│   ├─ AgentLoop                                     │
│   │   - turn N: messages.create → tool_use →       │
│   │     execute tools → append tool_result →       │
│   │     emit_progress → log to trace.jsonl         │
│   └─ TraceWriter → output/agent_trace.jsonl        │
└────────────────────────────────────────────────────┘
                       │
                       ↓
              ┌──────────────────┐
              │ workspace/       │
              │  ├─ logs/        │
              │  ├─ context/     │
              │  ├─ rules/       │
              │  ├─ images/      │
              │  └─ output/      │
              │     ├─ result.json        │
              │     └─ agent_trace.jsonl  │  ← 新增
              └──────────────────┘
```

---

## 4. 详细设计

### 4.1 HTTP 层（`app/agents/claude_api.py` 内嵌）

**策略：用 `anthropic.AnthropicVertex` 客户端，若鉴权不可覆盖则降级裸 httpx。**

```python
import httpx
from anthropic import AsyncAnthropicVertex

class _Client:
    def __init__(self, base_url: str, api_key: str, timeout: float):
        # 尝试 1：AsyncAnthropicVertex + 自定义 http_client
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"x-api-key": api_key},
        )
        self._client = AsyncAnthropicVertex(
            base_url=base_url,
            project_id="dummy",
            region="us-east5",      # 占位，proxy 不解析
            http_client=self._http,
        )

    async def create_message(self, **kwargs):
        return await self._client.messages.create(**kwargs)
```

**降级路径（Spike 验证失败时）**：直接 `httpx.AsyncClient.post(base_url + "/v1/messages", json=...)`，手动构造 Messages API 请求体。封装在同一个 `_Client` 类，对上层 `AgentLoop` 透明。

### 4.2 工具层（`app/agents/tools/`）

新建子包，4 个工具：

```
app/agents/tools/
├── __init__.py        # TOOLS_SCHEMA 导出 + dispatch
├── base.py            # ToolError, sandbox path check
├── read_file.py
├── write_file.py
├── grep.py
└── glob_tool.py
```

#### 4.2.1 `read_file`

```json
{
  "name": "read_file",
  "description": "Read a file relative to workspace. Returns content. Max 2MB.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "Path relative to workspace root"},
      "offset": {"type": "integer", "description": "Byte offset to start reading", "default": 0},
      "limit": {"type": "integer", "description": "Max bytes to read", "default": 2_000_000}
    },
    "required": ["path"]
  }
}
```

#### 4.2.2 `write_file`

```json
{
  "name": "write_file",
  "description": "Write a file under output/ subdirectory. Used for result.json.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "Path under output/ (e.g. 'output/result.json')"},
      "content": {"type": "string", "description": "File content"}
    },
    "required": ["path", "content"]
  }
}
```

**护栏**：path 必须以 `output/` 开头，否则返回 `ToolError`。

#### 4.2.3 `grep`

```json
{
  "name": "grep",
  "description": "Search for a regex pattern. Calls ripgrep under the hood.",
  "input_schema": {
    "type": "object",
    "properties": {
      "pattern": {"type": "string"},
      "path": {"type": "string", "description": "Directory or file relative to workspace", "default": "."},
      "glob": {"type": "string", "description": "Optional file glob filter (e.g. '*.log')"},
      "context_lines": {"type": "integer", "default": 0, "description": "Lines of context (-C N)"},
      "max_matches": {"type": "integer", "default": 200, "description": "Cap matches to avoid bloat"}
    },
    "required": ["pattern"]
  }
}
```

**实现**：`asyncio.create_subprocess_exec("rg", "--max-count", str(max_matches), ...)`，stdout 截断 1MB。

#### 4.2.4 `glob`

```json
{
  "name": "glob",
  "description": "List files matching a glob pattern under workspace.",
  "input_schema": {
    "type": "object",
    "properties": {
      "pattern": {"type": "string", "description": "e.g. 'logs/**/*.log'"},
      "max_results": {"type": "integer", "default": 500}
    },
    "required": ["pattern"]
  }
}
```

#### 4.2.5 安全护栏（`base.py`）

```python
def resolve_safe_path(workspace: Path, user_path: str) -> Path:
    """Resolve user_path within workspace; raise ToolError if outside."""
    resolved = (workspace / user_path).resolve()
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError:
        raise ToolError(f"Path escapes workspace: {user_path}")
    return resolved
```

### 4.3 Agent Loop（`app/agents/claude_api.py`）

```python
class ClaudeApiAgent(BaseAgent):
    async def analyze(self, workspace, prompt, on_progress=None) -> AnalysisResult:
        trace = TraceWriter(workspace / "output" / "agent_trace.jsonl")
        client = _Client(...)
        messages = [{
            "role": "user",
            "content": [{"type": "text", "text": prompt,
                         "cache_control": {"type": "ephemeral"}}]
        }]

        for turn in range(self.config.max_turns):
            t0 = time.perf_counter()
            try:
                resp = await asyncio.wait_for(
                    client.create_message(
                        model=self.config.model,
                        max_tokens=8192,
                        tools=TOOLS_SCHEMA,
                        messages=messages,
                    ),
                    timeout=self.config.per_turn_timeout,
                )
            except RateLimitError as e:
                trace.write({"turn": turn, "error": "rate_limit", "msg": str(e)})
                return _quota_exhausted_result(self.config.agent_type, str(e))
            except OverloadedError as e:
                trace.write({"turn": turn, "error": "overloaded", "msg": str(e)})
                # 触发 fallback_model：同一 client，下一轮换 model 名重试
                self.config.model = self.config.fallback_model
                continue

            tool_calls_log = []
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "end_turn":
                trace.write({"turn": turn, "stop_reason": "end_turn",
                             "usage": resp.usage.model_dump(),
                             "duration_ms": int((time.perf_counter()-t0)*1000)})
                break

            if resp.stop_reason == "tool_use":
                tool_results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    try:
                        result_text = await execute_tool(
                            block.name, block.input, workspace=workspace
                        )
                        tool_calls_log.append({
                            "name": block.name, "input": block.input,
                            "ok": True, "result_chars": len(result_text)
                        })
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": result_text,
                        })
                    except ToolError as e:
                        tool_calls_log.append({
                            "name": block.name, "input": block.input,
                            "ok": False, "error": str(e)
                        })
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": f"ERROR: {e}", "is_error": True,
                        })
                messages.append({"role": "user", "content": tool_results})

                trace.write({"turn": turn, "stop_reason": "tool_use",
                             "tool_calls": tool_calls_log,
                             "usage": resp.usage.model_dump(),
                             "duration_ms": int((time.perf_counter()-t0)*1000)})

                if on_progress:
                    await _maybe_await(on_progress(60 + turn*1, f"Turn {turn+1}: {[t['name'] for t in tool_calls_log]}"))
            else:
                # max_tokens / stop_sequence / refusal
                trace.write({"turn": turn, "stop_reason": resp.stop_reason,
                             "duration_ms": int((time.perf_counter()-t0)*1000)})
                break

        result = self.parse_result(workspace, "")
        result.agent_type = "claude_api"
        return result
```

**超时**：双层
- per-turn `asyncio.wait_for` 120s
- 整体 `asyncio.wait_for` 包在 orchestrator 层 600s（已有）

**Prompt caching**：user prompt 首块加 `cache_control: {type: "ephemeral"}`，跨轮命中率 ~90%（rules+context 不变）。

### 4.4 观测层（jsonl trace + 前端折叠展示）

#### 4.4.1 落盘格式

`workspace/output/agent_trace.jsonl`，每行一个 JSON：

```jsonl
{"turn":0,"stop_reason":"tool_use","tool_calls":[{"name":"grep","input":{"pattern":"BLE.*timeout","path":"logs/"},"ok":true,"result_chars":420}],"usage":{"input_tokens":42103,"output_tokens":812,"cache_read_input_tokens":0,"cache_creation_input_tokens":38200},"duration_ms":2341}
{"turn":1,"stop_reason":"tool_use","tool_calls":[{"name":"read_file","input":{"path":"logs/main.log","offset":1200,"limit":4096},"ok":true,"result_chars":4096}],"usage":{"input_tokens":3500,"output_tokens":230,"cache_read_input_tokens":38200,"cache_creation_input_tokens":0},"duration_ms":1100}
{"turn":2,"stop_reason":"tool_use","tool_calls":[{"name":"write_file","input":{"path":"output/result.json","content":"{...trimmed...}"},"ok":true,"result_chars":12}],"usage":{...},"duration_ms":800}
{"turn":3,"stop_reason":"end_turn","usage":{...},"duration_ms":50}
```

`write_file` 工具的 `content` 在 trace 中**截断到 2KB**，避免 jsonl 文件过大；完整内容已经写入 workspace 文件。

#### 4.4.2 后端 API

新增 `GET /api/tasks/{task_id}/trace`：
- 定位 workspace 目录（已有 task → workspace 映射）
- 读 `output/agent_trace.jsonl`，逐行 parse
- 返回 `{turns: [...], summary: {total_turns, total_input_tokens, total_output_tokens, cache_hit_ratio, total_duration_ms}}`
- 不存在 → 404（CLI 模式跑的旧 task 没有 trace）

#### 4.4.3 前端展示

工单详情视图（modal/drawer）新增一个**默认折叠**的"Agent 执行轨迹"区块：

```
┌──────────────────────────────────────────────┐
│ ▶ Agent 执行轨迹 (12 turns, 145k tokens)     │  ← 默认折叠
└──────────────────────────────────────────────┘

展开后：
┌──────────────────────────────────────────────┐
│ ▼ Agent 执行轨迹                              │
│   Total: 12 turns · 145k input · 4.2k output │
│   Cache hit: 87%  ·  Duration: 38.5s         │
│                                              │
│ ┌──────────────────────────────────────────┐ │
│ │ Turn 1 [tool_use] · 2.3s                 │ │
│ │   grep("BLE.*timeout", "logs/")          │ │
│ │   → 3 matches                            │ │
│ ├──────────────────────────────────────────┤ │
│ │ Turn 2 [tool_use] · 1.1s                 │ │
│ │   read_file("logs/main.log", offset=1200)│ │
│ │   → 4.0 KB                               │ │
│ ├──────────────────────────────────────────┤ │
│ │ Turn 8 [tool_use] · 0.8s                 │ │
│ │   write_file("output/result.json")       │ │
│ │   → 1.2 KB written                       │ │
│ ├──────────────────────────────────────────┤ │
│ │ Turn 9 [end_turn] · 0.05s                │ │
│ └──────────────────────────────────────────┘ │
└──────────────────────────────────────────────┘
```

**实现要点**：
- 用 `<details>` 原生折叠，或 shadcn `<Collapsible>`
- 失败 task（无 trace）不展示这个块
- CLI 模式跑的 task 也不展示
- "Tool 输入参数"块默认折叠二层（避免 grep pattern 太长撑爆 UI）

### 4.5 Settings 切换（共享 L1.5 + 主 Agent）

#### 4.5.1 配置项

`config.yaml`：

```yaml
agent:
  default: claude_api               # ← 新默认
  call_mode: api                    # ← 新增字段："api" | "cli"
  providers:
    claude_api:
      enabled: true
      base_url: "http://34.216.169.232:30001/vertex"
      model: "claude-sonnet-4-6"        # 与 CLI 一致；[1m] 后缀仅 CLI 识别
      fallback_model: "claude-haiku-4-5"
      max_tokens: 8192
      per_turn_timeout: 120
      max_matches_per_grep: 200
    claude_code:
      enabled: true                 # 保留，由 call_mode 决定是否激活
      model: "claude-sonnet-4-6[1m]"
      effort: "high"
      fallback_model: "claude-sonnet-4-6"
      allowed_tools: [...]          # 不变

context_condensation:
  enabled: true
  provider: anthropic              # 字段名不变
  model: "claude-haiku-4-5"        # 字段名不变
  # base_url 不在这里配，直接复用 agent.providers.claude_api.base_url
  ...
```

**联动规则**：

| `agent.call_mode` | 主 Agent | L1.5 |
|---|---|---|
| `"api"`（默认） | `ClaudeApiAgent`（走代理） | `AsyncAnthropic`（走代理，永久） |
| `"cli"`（回滚） | `ClaudeCodeAgent`（CLI 子进程） | `AsyncAnthropic`（走代理，永久） |

**说明**：L1.5 之前用 `AsyncAnthropic` 直连 Anthropic 公网，本次统一改成走代理。`call_mode` 只控制主 Agent，**不影响 L1.5**。

#### 4.5.2 Settings 页面 UI

`frontend/src/app/settings/page.tsx` 新增一块：

```
┌──────────────────────────────────────────────┐
│ Claude 调用方式                              │
│                                              │
│ ○ API 直连（推荐）                           │
│   通过公司 Vertex 代理直接调用 API           │
│   优点：可观测 / 无 CLI 依赖 / 容器轻量      │
│                                              │
│ ○ CLI 子进程（兼容回滚）                     │
│   保留原 claude CLI 调用方式                 │
│   优点：行为最稳定，与历史一致               │
│                                              │
│             [保存]                           │
└──────────────────────────────────────────────┘
```

后端 `PUT /api/settings/agent` 已存在，扩展 `AgentConfigUpdate` schema 增加 `call_mode` 字段，运行时改 `Settings.agent.call_mode`（已有 in-memory 更新机制）。

### 4.6 L1.5 同步迁移

文件：`app/services/context_condenser.py`

**决策**：L1.5 **永远走公司 Vertex 代理**，不受 `call_mode` 控制。原因：
- 容器内不应直连 Anthropic 公网（违背 Docker 隔离 + VPN-only 假设）
- L1.5 本来就是纯 API（无 CLI 形态），不存在"切回 CLI"语义

改动：

```python
# 旧
client = AsyncAnthropic(api_key=settings.condenser_api_key)

# 新
client = AsyncAnthropic(
    base_url=settings.agent.providers["claude_api"].base_url,
    api_key=settings.condenser_api_key,
    http_client=httpx.AsyncClient(headers={"x-api-key": settings.condenser_api_key}),
)
```

**对齐**：4.5 节示例 yaml 中 `context_condensation.model_api` 和 `model_cli` 两个字段简化为单字段 `context_condensation.model`（保持现状字段名 `model`），不再联动 `call_mode`。

---

## 5. 数据流

### 5.1 一次正常分析的事件流

```
1. Linear webhook → POST /api/linear → 入队
2. Worker pickup task → AgentOrchestrator.run_analysis
3. Orchestrator.select_agent → 读 settings.agent.call_mode="api" → ClaudeApiAgent
4. build_prompt_with_meta（不变）
5. ClaudeApiAgent.analyze:
   a. 初始化 TraceWriter → output/agent_trace.jsonl
   b. Loop:
      Turn 0: messages.create({prompt, tools}) → tool_use [grep,grep]
              → execute grep × 2 → tool_result
              → trace.write({turn:0,...})
              → emit progress(60, "Turn 1: grep×2")
      Turn 1: ... read_file ... emit progress(61)
      ...
      Turn N: tool_use [write_file output/result.json] → end_turn
              → trace.write({turn:N, stop_reason:end_turn})
   c. parse_result(workspace) → AnalysisResult
6. Orchestrator: result.agent_type = "claude_api", agent_model = "claude-sonnet-4-5"
7. 入库 + SSE 推前端
```

### 5.2 失败路径

| 失败位置 | 行为 |
|---|---|
| Spike 验证失败（auth header 不能覆盖） | 降级到裸 httpx 自管协议 |
| Per-turn 超时 | trace 写一条 timeout，break loop，parse_result 返回 partial |
| Rate limit | `RateLimitError` → fallback 到 codex（沿用现有 `_FALLBACK_MAP`） |
| Overloaded | 同一 client 换 fallback_model 重试本轮 |
| Tool 越权 | `ToolError` 进 trace，作为 `tool_result.is_error=true` 反馈给模型，模型自行调整 |
| Model 一直不写 result.json | 整 600s 超时 → parse_result 返回"未知"，trace 完整保留 → 详情页能看到模型一直在 grep 没 write |

---

## 6. 配置改动清单

### 6.1 `config.yaml`
- 新增 `agent.call_mode`
- 新增 `agent.providers.claude_api` 段
- `context_condensation.provider` 改名 + 增加联动逻辑

### 6.2 `.env`
- `ANTHROPIC_API_KEY=sk-xxx`（已有）
- `CLAUDE_API_BASE_URL=http://34.216.169.232:30001/vertex`（新增，可选——也可以只在 config.yaml）

### 6.3 `backend/requirements.txt`
- 确认 `anthropic>=0.40.0`（带 AsyncAnthropicVertex）
- `httpx` 已通过 anthropic 间接引入
- **新增**：`ripgrep` 需要在 Dockerfile 安装：`apt-get install -y ripgrep`

### 6.4 `backend/Dockerfile`
- 可**删除**：`RUN npm install -g @anthropic-ai/claude-code`（CLI 模式回滚时再装回来）
- **不删**：保留 CLI 安装作为回滚后手，仅当 call_mode=cli 时使用
- **新增**：`RUN apt-get install -y ripgrep`

### 6.5 `docker-compose.yml`
- `claude-auth` named volume **暂保留**（CLI 回滚需要）
- 长期可删（call_mode=api 稳定 1 个月后）

### 6.6 DB schema
- **不动**。`agent_type` 字段是 string，新增 `"claude_api"` 取值不需要 migration。

---

## 7. 兼容性 / 回滚

### 7.1 回滚步骤

Settings 页面把"调用方式"切回 **CLI 子进程** → 后端 `Settings.agent.call_mode="cli"` → 下一个 task 自动用 `ClaudeCodeAgent`。**无需重启、无需改代码**。

### 7.2 数据兼容

| 数据 | API 模式 | CLI 模式 |
|---|---|---|
| `analysis.agent_type` 字段 | `"claude_api"` | `"claude_code"` |
| `output/result.json` | 同一 schema | 同一 schema |
| `output/agent_trace.jsonl` | 有 | 无 |
| 前端"执行轨迹"展示 | 显示 | 隐藏 |

### 7.3 灰度策略（可选 Phase 2）

按 `rule_type` 灰度：

```yaml
agent:
  call_mode: cli                    # 默认 CLI
  call_mode_overrides:              # 灰度白名单
    bluetooth: api
    cloud_sync: api
```

本期**不做**，待 API 稳定后再加。

---

## 8. 风险清单（按 P × I 排序）

| # | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| 1 | Vertex proxy 不允许自定义 auth header / SDK 不让 override auth | 中 | 高 | **Spike 1（第一周）** 20 行验证；不行降级裸 httpx |
| 2 | Proxy 不支持 prompt caching（cache_control 头被丢） | 中 | 中 | Spike 时一起验；不行就关 caching，成本翻倍 |
| 3 | 工具循环行为和 CLI 不一致 → 分析准确率回归 | 中 | 高 | 灰度前用 20 个 golden samples A/B；准确率掉 >5% 不上 |
| 4 | ripgrep 在容器内没装 | 低 | 低 | Dockerfile 加一行 |
| 5 | per-turn timeout 120s 不够（大日志 grep 慢） | 低 | 中 | 加 `max_matches=200` 上限；监控调整 |
| 6 | 模型在 API 模式下还是一直不写 result.json | 中 | 中 | Trace 能看到，针对性改 prompt 加强约束 |
| 7 | Trace jsonl 写盘并发冲突 | 低 | 低 | 每个 task workspace 独占，无并发问题 |
| 8 | L1.5 迁移导致 cache miss / 浓缩质量下降 | 低 | 中 | L1.5 模型不变（Haiku），只改 base_url，质量等价 |
| 9 | 前端折叠组件性能（trace 上千行） | 低 | 低 | 默认折叠 + 分页/虚拟滚动（实施阶段定） |

---

## 9. 实施颗粒度（5 个 Sprint Task）

| # | 任务 | 工时 | 交付物 | 拦截条件 |
|---|---|---|---|---|
| 1 | **Spike**：HTTP 层验证 | 0.5d | 一个 20 行 `spike.py`，调通 Vertex proxy + 一次 tool_use 往返 | Spike 失败 → 重新拉通方案 |
| 2 | 工具层 + 单测 | 1d | `app/agents/tools/` 4 个工具 + `tests/agents/test_tools.py` | Spike 1 通过 |
| 3 | `ClaudeApiAgent` + agent loop + trace writer | 1d | `app/agents/claude_api.py` + `tests/agents/test_claude_api.py`（mock client） | Tool 层完成 |
| 4 | Orchestrator 接入 + Settings UI + L1.5 迁移 | 1d | settings page 改造 + `context_condenser.py` 改造 + `/api/tasks/{id}/trace` | Agent 完成 |
| 5 | 前端 Agent Trace 折叠展示 + A/B 回归 | 1d | 详情页新 section + 20 golden sample A/B 报告 | 全部完成 |

**总工时**：4-5 天（一个 sprint）

**关键路径**：Spike → 决定整个方案是否需要重新拉通

---

## 10. 验收标准

### 10.1 功能验收

- [ ] Settings 页面切换 API/CLI，下一个 task 立即生效
- [ ] API 模式下 task 完成后，详情页可展开看到 Agent Trace
- [ ] CLI 模式下 task 完成后，详情页**不**展示 Agent Trace 区块
- [ ] 一个工单同时跑 CLI 和 API，result.json 结构一致（problem_type、root_cause、user_reply 等字段都填了）
- [ ] 限流场景：API 模式下命中 RateLimitError 自动切换到 codex
- [ ] 超时场景：API 模式下 600s 后返回 partial result（result.json 已写过则用，没写过则"未知"），trace 完整保留

### 10.2 性能验收

- [ ] API 模式下单个 task 平均耗时 ≤ CLI 模式 × 1.1（cache 命中后应该更快）
- [ ] 3 并发下后端 RAM 占用 ≤ CLI 模式 × 0.6（无 Node 进程）

### 10.3 质量验收

- [ ] 20 个 golden samples A/B 测试，API 模式 problem_type 一致率 ≥ 90%
- [ ] API 模式 confidence 分布与 CLI 模式偏差 ≤ 10%

### 10.4 可观测性验收

- [ ] 故意失败一个 task（删 rules 文件），打开详情页能从 trace 看出失败 turn
- [ ] Trace 中每个 tool_call 都有 `ok` 字段 + 失败原因
- [ ] Token 使用量（input/output/cache）在 trace summary 中可见

---

## 11. 开放问题（实施阶段决定）

1. Agent Trace 前端组件用 shadcn `<Collapsible>` 还是原生 `<details>`？
2. Spike 验证 base_url 时，proxy 接受 `/v1/messages` 还是 `/anthropic/v1/messages` 路径？
3. `claude_api` provider 的 model 字段填 `claude-sonnet-4-5` 还是 `claude-sonnet-4-6`？（Vertex 代理支持哪个版本，spike 时一起验）
4. 折叠展示要不要支持"复制 trace JSON"按钮？（方便发 bug 报告）

---

## 12. 后续演进（不在本期）

- Phase 2：按 `rule_type` 灰度切换
- Phase 3：CLI agent 完全下线（运行 1 个月稳定后）
- Phase 4：Codex 也走 API（去除 codex CLI 依赖）
- Phase 5：Trace 落 DB 表 + Analytics 页统计平均轮数、失败 turn 分布、token 成本

---

**Status**: Draft. Pending review by sanato.
