# Token 消耗问题全面分析报告

> 分析时间：2026-05-25  
> 分析范围：最近 8 天工单分析任务（切换到 API key 模式后）

---

## 一、问题全貌

上周切换到 API key 直接调用方式后，**最近 8 天可追踪到的 82 个工单分析任务共消耗 7100 万 input tokens，平均每个任务 86.6 万 tokens。** 而理论上一次完整的工单分析只需约 3–5 万 tokens，实际消耗是理想值的 **17 倍以上**。

```
理想单次消耗:     30,000–50,000 tokens
实际平均消耗:        866,495 tokens   (17x)
实际最高单次:      6,907,896 tokens   (138x)
总计（8天）:      71,052,565 tokens
```

---

## 二、根本原因：API 模式下 messages 数组无限累积，CLI 模式不存在此问题

### CLI 模式（切换前）

```
Jarvis → subprocess("claude -p --max-turns 30") → Anthropic API
```

Jarvis 只是启动一个 `claude` 子进程，把 prompt 传进去，然后等结果。**整个 30 轮的 tool loop 完全在 Claude CLI 进程内部执行**，Jarvis 不参与 messages 数组的管理。

Claude CLI 内置了**自动 Context 压缩（Compact）机制**：当对话历史超过一定体积，CLI 会自动把旧的 tool results 压缩成摘要文本，释放 context 空间。对话轮数越多，每轮实际发送的 tokens 不会无限增长——CLI 在悄悄地做"瘦身"。

此外 CLI 模式使用 `claude-sonnet-4-6[1m]`，拥有 100 万 token 的 context window，即便有大文件也不容易触顶。

### API 模式（切换后）

```
Jarvis (claude_api.py) → httpx → Vertex 代理 → Anthropic API
```

现在是**我们自己的代码**（`claude_api.py`）负责维护 messages 数组，并在每一轮把**完整的 messages 数组**发给 API：

```python
# claude_api.py — 每轮追加，从不裁剪
messages.append({"role": "assistant", "content": content_blocks})
messages.append({"role": "user", "content": tool_results})
# ← 没有任何裁剪、压缩、摘要
```

**一旦某一轮工具调用读入了大文件，这个文件内容就永远存在于 messages 数组里，后续每一轮都会重新发送它。**

这导致 token 消耗随轮数呈 **O(N²) 增长**。

### 对比总结

| 对比维度 | CLI 模式 | API 模式（现在） |
|----------|---------|----------------|
| **messages 管理者** | Claude CLI 进程（Anthropic 实现） | 我们自己的 `claude_api.py` |
| **context 压缩** | ✅ CLI 自动压缩历史 | ❌ 无，全部永久追加 |
| **大文件读入的代价** | 仅当轮消耗，后续被压缩 | **永久进入 messages，每轮重发** |
| **context window** | 100 万 tokens（`[1m]` 版本） | 20 万 tokens（Vertex 不支持 `[1m]`） |
| **token 增长模式** | 大致线性（CLI 控制） | **O(N²)，随轮数平方增长** |

**一句话：CLI 一直在帮你免费做 context 压缩。切换到自己实现的 API tool loop 后，这个责任转移给了代码，但代码没有实现它。**

---

## 三、具体上涨原因拆解

### 原因 #1：`extraction_full.json` 被 agent 重复读取（贡献 45%）

系统在构建 prompt 时，已经把 extraction 的精简摘要（25KB）内嵌在 prompt 里了。但 agent 执行时，会在 turn 1 通过 `read_file` 工具额外读取 `context/extraction_full.json`（完整版，最大 2.1MB）。

这 2.1MB 的文件一旦作为 tool result 进入 messages 数组，**后续的每一轮 API 调用都会携带它**。

**数据佐证（task_2436a70278dc，6.5M tokens）：**

| Turn | 本轮 Input Tokens | 累计 | 备注 |
|------|-------------------|------|------|
| 0 | 3 | 3 | 读 rules + issue context |
| 1 | 357 | 360 | 读 extraction_full.json（2.1MB 入 context） |
| 2 | **639,230** | 639,590 | ← 爆炸，每轮都带着 640K |
| 3 | 639,534 | 1,279,124 | |
| … | ~640K | … | |
| 11 | 671,323 | **6,459,590** | |

turn 1 → turn 2，**每轮 input 从 357 跳到 639,230（增加 1791 倍）**。

**涉及任务：** 12 个任务读取了超 100KB 大文件，合计 **31.9M tokens（45%）**。

---

### 原因 #2：同一工单被重复分析多次（浪费 33.6M tokens）

26 个工单被触发了 2–7 次分析。

| Issue | 次数 | 总 Tokens | 说明 |
|-------|------|----------|------|
| fb_9f347bbc90 | **7 次** | 7,588,803 | 同一天 01:59–03:37 |
| fb_4b079a8dd4 | 2 次 | 7,617,334 | 同一天 01:46–01:57 |
| fb_5df8bb858c | 4 次 | 4,163,208 | |
| fb_e801a4bf8c | 4 次 | 3,157,066 | |
| fb_e81b1bd48b | 4 次 | 2,286,069 | |

**可判定为浪费的 token：33,637,275（占总量 47%）**

---

### 原因 #3：agent 触达 30 轮上限时仍在 grep 循环（贡献 16%）

9 个任务在第 30 轮结束时 stop_reason 仍是 `tool_use`，agent 没有写出 result.json 就被强制截断。这些任务全部"白烧"。

**9 个卡死任务合计：11,236,272 tokens（16%）**

---

### 原因 #4：agent 读取自身的 prompt.md 文件

至少 1 个任务（task_2436a70278dc turn 9）读取了 `prompt.md`（82KB），把整个 prompt 又追加进 messages 历史。工具层没有对文件读取路径做黑名单限制。

---

## 四、修复方案

### Fix 1（P0）：claude_api.py — messages 裁剪 + 循环检测

**位置：** `backend/app/agents/claude_api.py`

每轮调用前估算 messages 总体积，超过阈值时对旧 tool results 做截断，保持 context 可控。同时检测连续相同 tool pattern，强制插入收尾指令。

### Fix 2（P0）：read_file — 黑名单 + 大小上限

**位置：** `backend/app/agents/tools/read_file.py`

- 禁止读取 `context/extraction_full.json`（已在 prompt 中）
- 禁止读取 `prompt.md`、`fixup_prompt.md`
- 单次返回上限从 2MB 降到 200KB

### Fix 3（P1）：analysis_worker — 同 issue 节流

**位置：** `backend/app/workers/analysis_worker.py`

在任务创建时检测同 issue 近 10 分钟内是否已有 analyzing/done 任务，阻断重复触发。

---

## 五、修复效果预估

| 修复项 | 预期节省 |
|--------|---------|
| Fix 1（messages 裁剪） | 60–70% |
| Fix 2（黑名单 + 大小限制） | 额外 20% |
| Fix 3（重复分析节流） | 额外 15% |
| **合计** | **约 90%** |

```
修复前：866K tokens/任务
修复后：目标 50–80K tokens/任务
```
