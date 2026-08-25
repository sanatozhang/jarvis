# 多端接入指南（Web / Desktop / Backend）

> **面向读者**：AI（Claude Code / 其他 agent）+ 要接入 Apollo 的各端工程师。
>
> **本文定位**：讲**怎么把新端接进自动分析流程**。业务背景、数据结论与规划优先级见《[Apollo 平台化升级方案](../Apollo全平台支持.md)》。
>
> **与既有文档的分工**：
> - [`ticket-analysis.md`](./ticket-analysis.md) —— 现有分析模块的速查（入口、API、字段契约）
> - [`ticket-analysis-internals.md`](./ticket-analysis-internals.md) —— L1/L1.5/L2/L3 分层的设计动机与踩坑史
> - [`web-desktop-ticket-analysis.md`](./web-desktop-ticket-analysis.md) —— web/desktop 工单**今天**怎么靠人工补位（现状描述）
> - **本文** —— 怎么把上面那份文档描述的人工流程**自动化**（设计与实施）
>
> 行号基于 `main` @ `6aed227`。

---

## 1. 一句话现状

**平台（`platform`）这个维度目前只影响两件事：① 日志解密的分发 ② 源码仓的路由。** 规则选择、L1 抽取、L1.5 浓缩、L2 agent 编排、L3 校验、结果解析、转交流程**全部与平台无关**。

这既是好消息（复用率高）也是坏消息（平台信息没有真正参与问题识别，见 §6.1）。

| 平台 | 平台值 | 日志 | 源码仓 | 专属规则 | 成熟度 |
|---|---|---|---|---|---|
| app | `app` → 按 os 细分 `android`/`ios` | `.plaud` ChaCha20 解密 → 窗口化 → LLM 浓缩 | Flutter monorepo（<4.0.0）或 native 仓（≥4.0.0） | 10 个规则文件 | ✅ 生产主力 |
| mcp | `mcp` | 通常无日志（设计如此） | `plaud-devkits` | ✅ `rules/mcp.md` | 🟢 已通，量小 |
| web | `web` | ❌ 占位符透传 | `plaud-web` | ❌ 无 | 🟡 仅源码路由 |
| desktop | `desktop` | ❌ 占位符透传 | `fe-nexus` | ❌ 无 | 🟡 仅源码路由 |
| **backend** | **未注册** | 待定 | 未配置 | ❌ 无 | 🔴 不存在 |

---

## 2. 现有管线解剖

入口：`backend/app/workers/analysis_worker.py::run_analysis_pipeline()`（`:290-751`）

| Step | 行号 | 做什么 | 平台相关？ |
|---|---|---|---|
| 1 Fetch issue | `:320-357` | 按 ID 前缀路由：飞书走 API、本地/Linear 读 DB | 否 |
| （追问重裁） | `:359-388` | `_followup_window_params(depth)` → 窗口 ×2 / ×4 / 全量 | 否 |
| 2 Download logs | `:390-461` | 落 `workspace/<task_id>/raw/` + `_cache/<issue_id>/raw/`（上限 500 issue） | 否 |
| **3 Decrypt** | `:471-553` | `process_log_file_for_platform(fp, processed_dir, platform)`（`:520`）→ **唯一的平台分发点** | **✅ 是** |
| 4 Match rules | `:570-585` | `engine.match_rules(routing_text)` + `classify()` | 否（见 §6.1） |
| 4.5 日志时效预检 | `:587-610` | 日志最新事件比问题时间早 >30 天 → 直接出 `needs_user_retry`，跳过最贵的 agent | 否 |
| **5 L1 抽取** | `:612-619` | `extract_for_rules(rules, log_paths, problem_date)` | 否 |
| **5.5 L1.5 浓缩** | `:621-638` | `_run_context_condensation(...)`（`:873-`） | 否 |
| **6 Workspace** | `:643-684` | `repo_router.resolve()` 选 code_repo → `engine.prepare_workspace()` | **✅ 是** |
| **7 L2 Agent** | `:686-720` | `orchestrator.run_analysis(...)` | 否 |
| 8 计量 | `:730-746` | `cost.build_usage_record()` 聚合 agent + condenser | 否 |

### 分层职责速记

- **L1（确定性抽取）** `services/extractor.py::extract_for_rules()`（`:361`）—— 对每条规则的每个 `pre_extract` pattern 跑 `grep -E "<pattern>" file | tail -n 200`。注意是 `tail` 不是 `head`（`extractor.py:34-37` 记录了真实故障 `fb_08344bb236`：取最前 200 条被历史事故占满）。
- **L1.5（窗口化 + LLM 浓缩）**
  - Step A `services/log_windower.py::window_log_file()` —— 锚点 = `problem_date`（`issue.occurred_at` 优先，其次从描述正则抠，见 `issue_text.py::guess_problem_date`），前 4h / 后 2h（`config.py:236-237`）
  - Step B `services/context_condenser.py` —— L1 高信号行 **verbatim 注入 prompt 开头**（`_build_signal_block`，`:98-122`），采样正文只作背景
  - Guard 1（文件级）：折叠后仍触顶 20 万行 → 回退全量 + `metadata["complete"]=False`
  - Guard 2（覆盖率级）：`window_coverage_ratio < 0.5` → `rewindow_on_signal_lines()` 围绕信号行时间戳中位数重新定锚
- **L2（主分析）** `services/agent_orchestrator.py` —— `select_agent()` 优先级：`override` → 超大 prompt 强制 CLI（`cli_route_above_chars` 默认 500K）→ `call_mode` 概率分流 → `agent.routing[rule_type]` → `agent.default`。含配额熔断 `_FALLBACK_MAP`（`:43`）
- **L3（Stop Hook）** 写进 workspace 的 `.claude/settings.json`，校验 `output/result.json`：不存在 / 非法 JSON / 命中"分析中"话术 / `root_cause < 40` 字符 → `{"decision":"block"}` 强制重来，block 上限 2 次

---

## 3. 复用矩阵（接入新端前必读）

| 组件 | 位置 | 可复用性 |
|---|---|---|
| `.plaud` 容器解密（ChaCha20） | `decrypt.py:21-22`（密钥）、`:135`（cipher）、`:291`（`process_log_file`） | ❌ **app 专有** |
| magic 偏移自愈 | `decrypt.py:141-163` `_strip_pollution_prefix()` | ❌ app 专有 |
| 日志 era 解析（版本/OS/机型） | `extractor.py:128-224` `_select_log_era()` | ❌ **app 专有**（靠 `AppBuildInfo:` / `DatadogConfig initialized` / `DeviceInfoManager` 等 Plaud App 特有日志行） |
| 时间戳正则 | `log_windower.py:20` `_TS_PATTERN` | ⚠️ **需改**：只认 iOS `2026-02-01 03:52:53689` 和 Android `INFO: 2026-03-13 18:14:24.926329:` 两种。**接新端必须做成 per-platform 可插拔** |
| 窗口切割 + 模板折叠 | `log_windower.py`（`DEFAULT_MAX_PER_TEMPLATE=200` @ `:26`；`normalize_line_template` @ `:32-42`） | ✅ 只依赖「行 + 可解析时间戳」，换正则即可 |
| LLM 浓缩 | `context_condenser.py` | ✅ 输入是纯文本行 + 信号行列表，零平台假设 |
| L1 grep 抽取 | `extractor.py::grep_log` / `extract_for_rules` | ✅ 靠规则 pattern，换规则即换平台 |
| ZIP 解包 | `decrypt.py::_process_zip` | ✅ web/desktop 占位符已在复用 |
| 日志时效预检 | `analysis_worker.py:118-159` `_check_log_coverage` | ✅ 只需 `get_log_time_range()` 能解析时间戳 |
| 规则引擎 / workspace 组织 / agent 编排 / `parse_result` / Stop Hook / escalation | 多处 | ✅ **全部平台无关** |

> **结论**：desktop 与「有落盘日志的 backend」复用率极高——只需 ① 定义日志格式 ② 加时间戳正则 ③ 写规则文件。web 与「只有服务端可观测数据的 backend」需要走 §7 的查询通道。

---

## 4. 接入四要素

每个新端都要交付这四样，缺一不可：

### ① 平台注册

| 位置 | 要改什么 |
|---|---|
| `backend/app/platforms.py:15` | `PLATFORMS = ["app", "web", "desktop", "mcp"]` → 加新值（如 `"backend"`） |
| `backend/app/config.py:393-395` | `support_web` / `support_desktop` / `support_mcp: bool = False` → 加对应开关（默认 `False`，管理员在 `/settings` 开启） |
| `config.yaml:123-140` `repo_routing` | 加该端的 band（`wrapper` / `sub` / `github_repo` / `symbol_profile`） |
| `frontend/src/app/feedback/page.tsx:203-208` | 平台下拉框加选项 + 开关门控 |

`normalize_platform()`（`platforms.py:20-34`）的兜底策略：None / 空 / **未知值**一律归一为 `"app"`。加了新平台值但没同步这里，脏数据会静默变成 app 工单。

### ② 证据源接入

两条路，二选一或都要：

- **有落盘日志** → 在 `decrypt.py:221-241` 的 `process_log_file_for_platform()` 加分支 + 实现真实处理器（参考现有 `_process_log_web` @ `:243` / `_process_log_desktop` @ `:267` 的占位符结构）+ 在 `log_windower.py:20` 注册该端的时间戳正则
- **无落盘日志** → 走 §7 的服务端可观测数据查询通道

### ③ 规则文件

`backend/rules/<platform>-*.md`，YAML frontmatter + Markdown 正文。现有 11 个文件可参考：

| 文件 | id | priority | needs_code | 覆盖场景 |
|---|---|---|---|---|
| `flutter-crash.md` | flutter-crash | 9 | false | 灰屏/白屏/崩溃/闪退/ANR |
| `mcp.md` | mcp | 9 | **true** | MCP 平台开发者集成问题 |
| `bluetooth-connection.md` | bluetooth | 8 | — | 蓝牙/BLE/配对/断连/TokenNotMatch |
| `file-transfer.md` | file-transfer | 8 | — | 传输失败/转写失败/音频损坏 |
| `cloud-sync.md` | cloud-sync | 7 | — | 同步/上传下载失败 |
| `membership-payment.md` | membership-payment | 7 | — | 会员/支付/扣款/退款/订阅 |
| `hardware-firmware.md` | hardware-firmware | 6 | false | 幽灵录音/固件/OTA/锁区 |
| `recording-missing.md` | recording-missing | — | — | 录音丢失/文件消失 |
| `speaker-cloud.md` | — | — | — | 声纹上云 |
| `timestamp-drift.md` | — | — | — | 时间戳偏移 |
| `general.md` | general | **0** | false | 兜底，keywords 为空 |

**`mcp.md` 是「非 app 平台、大概率无日志」场景的唯一先例**，接 web/desktop/backend 时优先参考它：`pre_extract: []`、`needs_code: true`、第 51-52 行原则"无日志是正常情况，不要因为没有日志就判定 `system_failure`"。

改完规则**必须** `POST /api/rules/reload`（`rule_engine.py` 走内存 cache，不 reload 不生效）。

### ④ 工单字段

新端的专属定位信息（web 的 url/browser/session、backend 的 service/trace_id）应落 `pt_tickets.payload_json`（`backend/app/platform_tickets/models.py`），**但这条路径当前是空的**——见 §6.2。

---

## 5. Per-platform 接入手册

### 5.1 App（现状基准，作为参照系）

完整链路：飞书/表单/Linear 入口 → `.plaud` 下载 → ChaCha20 解密（`decrypt.py:291`）→ era 解析定版本（`extractor.py:128-224`）→ `repo_router.resolve()` 选仓 → 窗口化 → L1.5 浓缩 → agent 分析 → L3 校验。

关键前置：`analysis_worker.py:644-650` 的注释说明——工单没填版本号时**用"日志最新 era"解析出的 `app_version` 兜底**，否则 `select_band` 会默认取最高 band（native）把 Flutter 工单路由错。

### 5.2 MCP（已通，但有两个已知缺陷）

- **定位**：`rules/mcp.md:30` 明确——"Plaud MCP 服务器的技术支持专家，负责解答第三方开发者/内部团队在集成 `list_files`/`get_file`/`get_note`/`get_transcript` 等 MCP 工具时遇到的问题"。仓库 `Plaud-AI/plaud-devkits`。**不是** jarvis 自己暴露 MCP server（全仓 `grep -rn "fastmcp|FastMCP|mcp_server"` 零命中）。
- **缺陷 1**：`decrypt.py:221-241` 的平台分发**没有 `mcp` 分支** → mcp 工单若真上传文件会走 `.plaud` 解密分支 → 大概率 `logs_corrupted=True`。目前无害（mcp 工单基本不传日志），但是隐患。
- **缺陷 2**：见 §6.1——`[mcp]` 前缀在规则匹配前被剥离，规则实际只能靠用户在正文写 `mcp` / `list_files` 等词才命中。

### 5.3 Web

**证据源**：无本地日志。浏览器不产出 `.plaud`，`decrypt.py:243` 的 `_process_log_web` 是占位符（docstring 原文"Extend with web-specific decryption when the format is defined"），逻辑只有"识别 ZIP 就解压，否则原样透传"。

必须走 §7 的服务端可观测数据查询通道（RUM）。

**要采集的专属字段**：出问题的页面 URL、浏览器与版本、RUM session id、用户标识、精确时间点。当前表单**完全没有这些输入框**（`feedback/page.tsx:42-45` 与 app 工单同构），只能靠用户写在 `description` 自由文本里。

**已知能力缺口**：现有 Datadog 封装只有"按 issue 聚合"和"平台/版本分布统计"，**没有"按用户 email + 时间窗找 session"**，也**没有 Session Replay**。这两块要新写，不是开个权限就行。

### 5.4 Desktop（复用度最高，建议第一个接）

**证据源**：有本地日志。改动面极小：

1. 实现 `decrypt.py:267` 的 `_process_log_desktop`（现为占位符）
2. 在 `log_windower.py:20` 注册 desktop 的时间戳格式
3. 写 `backend/rules/desktop-*.md`
4. `repo_routing.desktop` 已配好（`config.yaml` → `fe-nexus` / `Plaud-AI/fe-nexus`）

**需 Desktop 团队交付**（阻塞项）：
- [ ] 日志样本（至少 3 份，覆盖 macOS / Windows）
- [ ] 时间戳格式说明（含时区处理）
- [ ] 打包格式（zip / tar / 明文）
- [ ] 版本与 OS 标识行的格式（用于替代 app 的 era 解析）
- [ ] 崩溃报告（Crash Report）格式，若与运行日志分离

### 5.5 Backend（价值最大，前置依赖最重）

**⚠️ 前置阻塞**：BE 服务端日志形态**尚未确认**。本节按「Datadog 为主 + 导出文件为辅」的假设编写，需 BE 团队确认后修订。

**注册工作**：`platforms.py:15` 加 `"backend"`；`config.yaml::repo_routing` 加 BE 仓 band；`config.py` 加 `support_backend` 开关。

**双路径设计**：
- 路径 A（Datadog Logs / APM）：走 §7 通道，但用 Logs Search API + APM trace 而非 RUM
- 路径 B（导出日志文件）：走复用管线，只需加时间戳正则

**跨端联合分析（本端的核心价值）**

这是接 BE 的真正目的。对齐三要素：

| 关联键 | 来源 | 现状 |
|---|---|---|
| 用户标识 `uid` | app 日志已能抽取（`extractor.py:242,255,295-321`） | ✅ 已有 |
| 时间窗 | `problem_date` 锚点（`issue.occurred_at` 或从描述抠） | ✅ 已有 |
| 调用链 `trace_id` | 需 BE 提供，app 侧目前不埋 | ❌ 待建 |

典型场景（对应《商业化方案》§3.2 的 8,992 单客诉）：
- 「付了钱但会员没生效」→ app 日志只到 `makingPurchase`（`rules/membership-payment.md` 的 `pre_extract` pattern），需 BE 侧的订单状态、支付回调、权益发放记录
- 「设备/会员绑到错账号」→ 需 BE 侧的设备绑定变更历史
- 「买了但权益/邮件没到」→ 需 BE 侧的履约与邮件投递记录

**要写的规则**：`backend/rules/be-membership.md` / `be-account-binding.md` / `be-fulfillment.md`

---

## 6. 必须先修的基础设施缺陷

不修这些，多端接入等于空转。

### 6.1 🔴 平台不参与规则匹配（最关键）

三处代码共同造成一个断点：

1. `api/feedback.py:130-131` 把平台拼成描述前缀：`[MCP][分类] 用户原文`
2. `services/issue_text.py:11-13` 的 `_LEADING_TAG_RE` 匹配 `^\s*(?:(?:\[[^\]]+\])|(?:【[^】]+】)|...)`，`strip_leading_metadata()`（`:16-25`）**循环剥掉所有前导 `[...]` / 【...】 / (...) 标签**
3. `analysis_worker.py:575-577` 只把 `normalize_description_for_matching(issue.description)` 传给 rule engine，**`platform` 变量从未传入**

**后果**：`rules/mcp.md` 里的 `[mcp]` 和 `mcp` 两个关键词，**无法通过"用户在下拉框选了 MCP"命中**——平台前缀在匹配前已被剥离。web/desktop 同理，因为没有专属规则 + 平台不参与匹配，必然落 `general.md`（priority 0）。

**修法**：给 `RuleEngine.classify()` / `match_rules()` 增加 `platform` 参数；规则 frontmatter 增加 `platforms: [web, desktop]` 字段做硬过滤或加权。

### 6.2 `pt_tickets` 表已建但零写入路径

`backend/app/platform_tickets/` 只有存储层骨架（models + migrations + config）。其 `CLAUDE.md`「当前阶段」明确列出尚未做的：

- [ ] id→存储路由 / 跨表统一读取层
- [ ] `/api/platform-tickets` 录入 API（**不存在**）
- [ ] analysis_worker 接入
- [ ] analytics 平台维度、per-platform 分类体系
- [ ] 前端 tracking / oncall / analytics 平台展示

生产库实测 `pt_tickets` **0 行**。今天的 web/desktop 工单实际走的是 `/api/feedback`（老表 `IssueRecord`，`fb_` 前缀）。

> 设计要点（勿套错 crashguard 模板）：`platform_tickets` **故意不写 `.importlinter` forbidden 合约**——因为新端工单要走 app 同一套 `analysis_worker`，import 墙会挡住融入路径。隔离靠「独立表 + `payload_json` + id 前缀路由」保证。

### 6.3 其他

| 缺陷 | 位置 | 后果 |
|---|---|---|
| Linear webhook 入口不带 platform | `api/linear_webhook.py`（全文无 `platform`） | 一律归一为 `app` |
| OpenAPI 入口不带 platform | `api/v1_analyze.py` | 同上 |
| `get_repo_routing()` backfill 缺 mcp 分支 | `config.py:620-627` | env-only 部署拿不到 mcp 仓 |
| `decrypt.py` 无 mcp 分支 | `decrypt.py:221-241` | 上传文件的 mcp 工单误判 corrupted |
| 工单表单版本号字段基本为空 | 生产实测 `issues.app_version` 334/337 为空 | 版本维度只能靠 `analyses.log_metadata_json` 从日志反解（493/493 有值）。**无日志的端将完全拿不到版本信息** |
| `issue_recurrences` 表 0 行 | — | 复发检测建了表但无数据，不要当能力宣传 |

---

## 7. 服务端可观测数据查询通道设计

web 与 backend 的关键依赖。

### 7.1 现状：三层硬阻断（不是配置问题）

1. **工具白名单**：Claude CLI 在分析 workspace 里只有 `Read/Write/Grep/Glob` + 只读 shell（`grep/wc/head/tail/sort/awk/sed/date`），见 `config.yaml:40-60`。**无 WebFetch、无 MCP server 挂载**，进程物理上发不出网络请求。
2. **凭证隔离**：`CRASHGUARD_DATADOG_API_KEY` / `CRASHGUARD_DATADOG_APP_KEY` 只存在于 `env_prefix="CRASHGUARD_"` 的 `app/crashguard/config.py`，主流程 `app/config.py::Settings` 里**根本没有这两个 key**。
3. **隔离合约**：`app/crashguard/CLAUDE.md` 列出"允许的对外耦合点仅 6 个"（feishu_cli / repo_updater / agent_orchestrator / get_session / repo_router / mt_runner 锁），**`datadog_client` 不在其中**；`backend/.importlinter` + `scripts/check_crash_decoupling.py` 启动时硬阻断。

工单侧唯一命中 "Datadog" 字符串的两处都不是查询：`extractor.py:262-268` 是从**本地日志文本**匹配 `DatadogConfig initialized .. version: 4.0.100+813` 猜 app 版本；`api/settings.py` 只是把 crashguard 配置项透到管理页。

### 7.2 ✅ 但已有可复制的成熟范式

**crashguard 的 deep_analyzer 已经实现了「让 agent 自己查 Datadog」**：

- `crashguard/services/deep_analyzer.py:13-14` 的 docstring：「诊断 prompt 会引导 AI 通过 Bash 调用 workspace/tools/ 下的 5 个 Python 脚本（datadog_query / git_blame / git_pickaxe / find_similar / get_session）」
- `crashguard/services/diagnosis_tools/datadog_query.py` —— `python tools/datadog_query.py --dql "<DQL>" --limit 50`，直打 `POST /api/v2/rum/events/search`
- `crashguard/services/diagnosis_tools/get_session.py` —— **`--session-id <id>` 拉整条 RUM session 事件流**（正是 web 工单最需要的单点下钻能力）
- 工具复制进 workspace：`deep_analyzer.py:182-197`

**即：技术形态是现成的，不需要从零发明。**

### 7.3 改造方案

| 步骤 | 内容 | 注意 |
|---|---|---|
| 1 | Datadog 凭证提到共享配置段，主流程 `Settings` 可读 | 不要直接跨 crashguard 隔离墙调 `datadog_client`——那会违反 `.importlinter` 合约导致启动失败 |
| 2 | `diagnosis_tools/` 抽成共享包（建议 `app/services/observability_tools/`），crashguard 与工单流程各自引用 | 抽包而非跨界 import，是绕过隔离合约的正确姿势 |
| 3 | **新增查询能力**：按 user email / uid + 时间窗定位 session | 现有只有"按 issue 聚合"（`get_issue_detail` @ `datadog_client.py:345`）和"按 session_id 拉流"，缺"按用户找 session"这一跳 |
| 4 | 工单 workspace 工具白名单放行 `Bash(python tools/*.py)` | **⚠️ 安全评估必做**：这是给 agent 开网络出口。必须配合收口 §8 的注入面（尤其 `extractor.py` 的 `shell=True`），否则等于把 RCE 链的落点从"读本地日志"扩大到"能发外网请求" |
| 5 | prompt 里引导 agent 用 tools（参考 `deep_analyzer` 的写法） | 无日志时的 prompt 分支在 `agents/base.py:283-300` |

---

## 8. 源码访问面清单（《商业化方案》§4 的技术支撑）

### 8.1 Apollo 能访问哪些业务源码

`docker-compose.yml:47-56` 以**同名绝对路径** bind-mount 宿主 checkout（不是 clone），未配置的变量回落 `/dev/null`：

| 环境变量 | 对应仓库 |
|---|---|
| `CODE_REPO_APP` / `CRASHGUARD_REPO_PATH_FLUTTER` | `Plaud-AI/Plaud-App`（Flutter monorepo，含 `plaud-android` / `plaud-ios` 子仓） |
| `CODE_REPO_ANDROID` / `CODE_REPO_IOS` / `CRASHGUARD_REPO_PATH_{ANDROID,IOS}` | `Plaud-AI/plaud-native-app`（含 `plaud-native-android` / `plaud-native-ios`） |
| `CODE_REPO_MCP` | `Plaud-AI/plaud-devkits` |
| （`config.yaml::repo_routing`） | `Plaud-AI/plaud-web`、`Plaud-AI/fe-nexus` |

**即：公司几乎全部客户端源码。**

### 8.2 权限面

| 项 | 事实 | 位置 |
|---|---|---|
| 挂载模式 | **读写**（无 `:ro`） | `docker-compose.yml:47-56` |
| workspace 内暴露 | `workspace/code` symlink 指向业务仓 | `rule_engine.py:320-323` |
| agent 授权 | `--add-dir <业务仓绝对路径>` | `agent_orchestrator.py:284-287` |
| agent 工具 | 白名单含 **`Write`**，以及 `Bash(sed:*)`（`sed -i` 也是写原语）——2026-08-25 修正：这里一直写的是 `Shell(sed:*)`，claude CLI 认的工具名是 `Bash`（`claude --help` 官方示例 `Bash(git *) Edit`），旧语法大概率从未生效，改成 `Bash(...)` 后才是真的授权；实际约束力仍取决于目标机器 `~/.claude/settings.json` 有没有更宽松的 `permissions.allow`/`defaultMode` 覆盖，上线前要在目标环境里做一次真实验证，不能只看这份 yaml | `config.yaml:50-60` |
| 容器身份 | **root**（Dockerfile 无 `USER`；`:72` 还 `git config --system --add safe.directory '*'` 关掉 git 所有权防御） | `backend/Dockerfile:70-75` |
| SSH 私钥 | 宿主 `~/.ssh` 挂进容器（`:ro`，但容器内 root 可读全部私钥） | `docker-compose.yml:58` |
| GitHub 身份 | `gh` OAuth 持久化在 named volume，代码刻意剥掉 `GH_TOKEN`/`GITHUB_TOKEN` 强制走 OAuth → **推送身份是真人的 GitHub 账号** | `docker-compose.yml:40`；`pr_drafter.py:620,686` |
| 自动 PR | crashguard 能改源码 → `git add/commit/push` → `gh pr create --draft` | `pr_drafter.py:1042,1973` |

**综合风险**：一旦该主机被入侵 = 全部客户端源码泄漏 + 可用真人开发者身份推代码。当前唯一屏障是内网 + VPN，上云前必须收口。

### 8.3 已有护栏（正面项，别推翻重做）

- **命令黑名单**（`pr_drafter.py:180-183,662-678`）：拒 `git merge` / `git rebase` / `--merge` / `--squash`；拒 `gh merge` / **`gh ready`** → **PR 永远停在 draft，agent 无法自己转正**；巧妙跳过 `-m/--body/--title` 后的实参避免误伤 PR 正文（`:665-670`）
- 分支名强制 `fix/crashguard/<...>/<short>-...`（`:614`）+ GitHub 去重查询（`:622-631`）
- PR 质量门 `pr_quality_gates.py`、冲突自愈 `pr_conflict_resync.py`、审查迭代记录表 `crash_pr_review_iterations`
- review-responder 输出契约校验：`verdict ≠ addressed` 时禁止改文件（`pr_review_responder.py:551`）
- `_run_git` 统一剥 `GH_TOKEN`/`GITHUB_TOKEN`（`:679-688`）
- **`services/claude_headless.py:46-56` 是全仓沙箱最佳实践模板**：`--tools ""`（零工具）+ `--no-session-persistence` + `--setting-sources ""` + `--strict-mcp-config` + 临时 scratch cwd。收口时以它为目标形态

---

## 9. 安全合规清单（上云前置）

| # | 问题 | 位置 |
|---|---|---|
| S1 | SSO 有完整实现但生产关闭（`ENABLE_SSO=false`）→ 中间件直接放行，全站零认证 | `middleware/auth.py:81-82` |
| S2 | CORS `allow_origins=["*"]` + `allow_credentials=True`（反模式，注释自己写着 "tighten in production"） | `main.py:327-334` |
| S3 | **admin 校验靠 URL query `?username=`，任意人可提权**；admin 名单还能从无鉴权的 `GET /api/users` 白拿（docstring 自曝 "admin only in practice, no enforcement yet"） | `env_settings.py:93,127`（读写 `.env`！）/ `oncall.py:201,238,259` / `graygate.py:47` / `users.py:66` |
| S4 | `/api/rules/*`、`/api/settings/*`、`/api/crash/approve-pr`（approver 是自填字符串→可推业务仓 PR）、`/api/local/*` **完全无鉴权** | `rules.py:48,62,80,88`；`settings.py:68,246,355,430,485`；`crash.py:1797` |
| S5 | **未鉴权 rule 写入 → shell 注入 → RCE 完整链**，落点是持有 SSH 私钥 + gh OAuth 的 root 容器 | `rules.py:62` → `extractor.py:383,42-49`（f-string 拼 `shell=True`） |
| S6 | 路径遍历任意文件读（含 `/.env`）：`filename:path` 无 `.resolve()` + `is_relative_to()` | `local.py:239-242`；同文件 `:220,:265` 未鉴权下载解密后用户日志 |
| S7 | 日志加密密钥**硬编码进 git** + **固定 nonce**（ChaCha20 流密码，keystream 对每个文件相同 → 任意两份日志异或即破） | `decrypt.py:21-22` |
| S8 | Zip Slip：密钥公开 + `extractall` 无 `..` 校验 → 工单附件可任意路径写文件 | `decrypt.py:203,385` |
| S9 | **`.env.bak` 未被 gitignore**（规则是 `.env.bak.*` 带点，恰好漏掉本体），内含 34 键完整凭证快照 | `.gitignore:5,26` |
| S10 | workspaces **无 TTL / 无清理 / 无加密**，落盘解密后明文日志（**含录音 transcript**，`log_windower.py:24-26` 注释自证）+ 用户截图 + uid/file_id；**零 PII 脱敏** | `rule_engine.py:292-328`；`extractor.py:242,295-331` |
| S11 | 无 actor 审计：唯一 audit 表 `crash_audit_logs` **没有 who 字段**；`events.username` 客户端自填不可采信；`rules` 表无 `updated_by` 无变更历史 | `crashguard/models.py:370-382`；`db/database.py:197-210,245-257` |
| S12 | cookie 有效期 **365 天** + 无服务端吊销 → 离职员工 token 一年有效 | `config.py:165`；`api/auth.py:316-325` |
| S13 | 凭证运行期扩散：Web UI 可写 `.env`（为此把 `.env` 挂成可写）；飞书 secret 明文落 `~/.lark-cli/config.json` 并持久化；100 号裸机用**明文文件存 macOS 钥匙串密码** | `docker-compose.yml:23`；`feishu_cli.py:105-122`；`deploy-bare.sh:129-141` |
| S14 | `/api/v1` API key **fail-open**（未配 key = 完全开放），且在 SSO 豁免名单；用 `!=` 比对有时序侧信道 | `v1_analyze.py:44,49-50,54`；`config.py:172` |
| S15 | 无 CI（`.github/` 只有 PR 模板）；Dockerfile 里测试**默认跳过** `RUN_TESTS=0`；依赖全 `>=` 无 lockfile 无 SBOM | `backend/Dockerfile:5,22-27`；`requirements.txt:2-25` |

**正面项**：Feishu OAuth 全流程完整（signed state 防 CSRF + `plaud.ai` 域白名单 + httpOnly/SameSite cookie，`api/auth.py:66-203`）；启动 fail-fast（SSO 开启但密钥缺失或 JWT secret <32 字符则拒绝启动，`main.py:29-43,97`）；Linear webhook HMAC 验签（`linear_webhook.py:56-60`）；agent 子进程剥离 `ANTHROPIC_API_KEY`（`claude_code.py:21-31`）；`data/`+`workspaces/`+`*.db` 已 gitignore；**git 历史未发现真实高危凭证泄露**（已用 `git log --all -S` 逐项核查 `sk-ant` / `ghp_` / `lin_api_` 等）。

---

## 10. 分析质量：置信度下降的技术归因

生产实测：high confidence 占比 6月 42.6% → 7月 26.3% → 8月 18.0%（近 6 周排除 `system_failure`/`needs_user_retry` 的净值 22.1%）。

**归因 1：模型换代未做适配**。7 月同月新旧模型并存，可直接对比：

| `agent_model` | 7月单量 | high 占比 |
|---|---|---|
| `claude-sonnet-4-6[1m]`（旧） | 26 | **53.8%** |
| `claude-sonnet-5[1m]`（6月底切换） | 335 | **24.2%** |

**归因 2：deep 模式占比暴涨但工具未同步放宽**。

`is_deep_analysis` 占比：6月 0.6% → 7月 19.4% → 8月 28.8%。

| 维度 | deep | 非 deep |
|---|---|---|
| high 占比（8月） | 13.6% | 20.0% |
| `system_failure` 率（8月） | 28.4% | 17.1% |

`agent_orchestrator.py:135,143`（及 `:174,182`）给 deep 模式放宽了 `max_turns=40` 和 `log_read_cap=30`，**但工具白名单没有同步放宽**。`confidence_reason` 字段原文印证：

> "本次运行未能进入实际日志 grep 验证阶段，全部 41 轮对话中大量时间耗费在被权限拒绝的 python3 探测命令上"
> "没有采集到任何日志证据；会话在完成任何 grep 验证前就因达到最大轮次而被终止"
> "源材料是分析进程的错误/遥测输出（达到最大轮次上限），不包含任何真实的日志证据……强行编造会违反不得虚构结论的原则"

**即：AI 在 deep 模式下尝试用更强手段交叉验证，权限不允许，反复碰壁到轮次耗尽，最后诚实标低置信度而非编结论。** L3 Stop Hook 的"不许写模糊结论"约束在正确工作。

**修复方向**：① 针对 sonnet-5 重调 prompt 与 L3 校验阈值 ② 给 deep 模式配套只读工具（与 §7.3 步骤 4 是同一件事——放行 `Bash(python tools/*.py)` 同时收口注入面）。

---

## 11. 终局链路：Linear 工单作为核心载体

### 11.1 目标架构

Linear issue 是一张客诉的**唯一主线**——Apollo 分析结论、oncall 交接、跨端流转、最终根因全部挂在同一个 issue 上。

```
┌─────────────┐
│   Zendesk   │
│   ticket    │
└──────┬──────┘
       │ ① Trigger/Webhook → 建 Linear issue（携带 zendesk_id 回链）
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        Linear Issue（核心载体）                          │
│                                                                         │
│  workflowState:  Todo ─▶ Analyzing ─▶ Triage ─▶ In Progress ─▶ Done    │
│                            │            │            │                 │
│  assignee:                 │        App oncall   BE oncall              │
│                            │            │            │                 │
│  label:               platform:app  needs:be    root-cause:payment     │
│                            │            │            │                 │
│  comment:            ②AI分析结论   ③人工判断    ④修复结论+版本          │
└──────┬─────────────────────────────────────────────────────────────────┘
       │ 每次 state / assignee 变化
       ▼
┌─────────────┐
│    Slack    │  #app-oncall / #be-oncall / #csc  按 assignee 团队路由
└─────────────┘
```

### 11.2 各环节实现状态与缺口

| 环节 | 现状 | 缺口 / 实现要点 |
|---|---|---|
| Zendesk → 建 Linear issue | ❌ `services/zendesk.py` 仅 108 行，**纯只读**（`fetch_ticket` @ `:46`、`fetch_ticket_comments` @ `:59`、`fetch_ticket_with_comments` @ `:94-108`），零写操作 | **优先评估 Zendesk-Linear 原生集成**，避免自研。若自研需新增 Zendesk Trigger + `/api/zendesk/webhook` |
| Linear → Apollo 分析 | ✅ `api/linear_webhook.py:43` `POST /api/linear/webhook`，HMAC 验签（`:56-60`），`@ai-agent` 触发（`:38-40`） | ⚠️ 两个隐患：① 该入口**不带 platform**（全文无 `platform`）→ 一律归一 `app`；② 去重用内存 `_active_issues: set`（`:36`），**多副本部署失效**，上云前必须换 Redis/DB |
| 分析结论写回 issue | ⚠️ 部分 | ✅ `create_comment`（`linear.py:185-207`，`commentCreate`）+ `add_label_by_name`（`:209-244`） |
| **改 issue 状态** | ❌ **做不到** | 全仓**零 `stateId`**、零 `workflowStates`。`issueUpdate` mutation 全仓仅出现 1 次（`linear.py:236`），input **只有 `labelIds`**：<br>`issueUpdate(id: $issueId, input: { labelIds: $labelIds })`<br>需新增：`workflowStates` query 拿 team 的状态列表 + `issueUpdate(input:{stateId})` |
| 改 assignee（跨端交接） | ❌ 做不到 | 同上，需 `issueUpdate(input:{assigneeId})` |
| 跨端 oncall 交接 | ⚠️ 走飞书，与 Linear 脱节 | 现状：`feishu_cli.py:946` `create_escalation_group()` 建群 + `:1189` `notify_oncall()`，状态记在 `issues.escalation_status`（`in_progress`/`resolved`），**与 Linear issue 无联动** |
| Slack 通知 | ❌ **零集成** | 全仓大小写不敏感搜 `slack` 唯一命中是 `services/categories.py:4` 一句注释。无 SDK / webhook / token / `SLACK_*` env |
| 结论结构化沉淀 | ⚠️ 双份不通 | Apollo 侧有 `issues.fix_target`/`fix_version`/`resolve_reason`/`resolved_at`/`resolved_by`（`db/database.py:65-69`），但与 Linear issue 无统一数据源 |

### 11.3 实现要点

**① Linear 状态机映射**

需要在配置里建立 Apollo 内部状态 → Linear `workflowState` 的映射表。`workflowStates` 是 per-team 的，必须先查询：

```graphql
query TeamStates($teamId: String!) {
  team(id: $teamId) { states { nodes { id name type position } } }
}
```

`type` 枚举为 `triage` / `backlog` / `unstarted` / `started` / `completed` / `canceled`——**映射时用 `type` 而非 `name`**（name 各团队自定义，会漂移）。

**② 跨端交接的 assignee 解析**

App oncall → BE oncall 的交接需要：
1. Apollo 判定该问题归属哪个端（依赖 §6.1 的平台维度修复 + BE 规则命中）
2. 查该端当周 oncall（现有 `db.get_current_oncall()`，见 `api/oncall.py:325`）
3. 邮箱 → Linear userId 解析（需新增 `users` query 缓存）
4. `issueUpdate(input:{assigneeId, stateId})` 一次改完

**③ Slack 集成最小形态**

按 assignee 所属团队路由到不同频道。建议先做 Incoming Webhook（只发不收），不要一上来做 Bot：

| 事件 | 目标 | 内容 |
|---|---|---|
| 分析完成 | 对应端 oncall 频道 | 问题类型 + 根因摘要 + Linear 深链 |
| 跨端流转 | 新接手端的频道 | 前一手的结论 + 为什么转过来 |
| 已解决 | CSC 频道 | 结论 + 可以回复用户的话术 |

凭证走 `SLACK_WEBHOOK_URL_*` env，遵循 `config.py` 的四层配置约定。

**④ 统一沉淀**

Linear issue 关闭时把结构化结论同步回 `issues` 表（`fix_target`/`fix_version`/`resolve_reason`），保证趋势分析（`analytics.py`）和复发检测（`issue_recurrences`，当前 0 行）有数据可用。

---

## 12. 接入 checklist

新端接入时逐项勾选：

**平台注册**
- [ ] `platforms.py:15` `PLATFORMS` 加值
- [ ] `config.py` 加 `support_<platform>` 开关（默认 `False`）
- [ ] `config.yaml::repo_routing` 加 band（wrapper / sub / github_repo / symbol_profile）
- [ ] `docker-compose.yml` 加源码仓挂载变量（**按 §8 的收口方向，新端优先用只读挂载**）
- [ ] `feedback/page.tsx` 下拉框加选项 + 开关门控
- [ ] `api/auth.py` 透出新开关给前端

**证据源**
- [ ] 有日志：`decrypt.py:221-241` 加分支 + 实现真实处理器
- [ ] 有日志：`log_windower.py:20` 注册时间戳正则（做成 per-platform 可插拔）
- [ ] 无日志：接 §7 查询通道
- [ ] 验证 `_check_log_coverage`（`analysis_worker.py:118-159`）能正确解析该端时间戳

**规则**
- [ ] 写 `backend/rules/<platform>-*.md`（参考 `mcp.md` 的无日志范式）
- [ ] frontmatter 加 `platforms:` 字段（依赖 §6.1 修复）
- [ ] `POST /api/rules/reload` 生效验证

**工单字段**
- [ ] `pt_tickets` 录入路径打通（依赖 §6.2）
- [ ] 前端表单采集该端专属字段
- [ ] analytics 加平台维度

**联调验证**
- [ ] 造一张该端工单，端到端跑通到 `output/result.json`
- [ ] 确认 `rule_type` 命中专属规则而非 `general`
- [ ] 确认 workspace 的 `code/` symlink 指向正确的仓
- [ ] 确认 `analyses.platform` 落值正确（注意 `analyses` 默认 `"app"`，`issues` 默认 `""`，按平台 join 会漏）
- [ ] 跑 `pytest backend/tests/ -v` + `lint-imports`

---

## 13. 附录

### 13.1 生产数据复现 SQL

只读连接（**必须带 `mode=ro`**，不要直接 `sqlite3 <path>`，会创建 `-wal`/`-shm` 并可能拿写锁）：

```bash
DB="file:/Users/mac/jarvis/data/appllo.db?mode=ro&immutable=1"

# 周工单量（按来源拆）
sqlite3 -header "$DB" "
SELECT strftime('%Y-%m-%d', created_at, 'weekday 0','-6 days') wk,
       COUNT(*) total, SUM(source='feishu') feishu, SUM(source='local') local_form
FROM issues WHERE deleted=0 GROUP BY 1 ORDER BY 1 DESC LIMIT 12;"

# 真实转交工程师量（escalated_at 非空）
sqlite3 -header "$DB" "
SELECT strftime('%Y-%m-%d', escalated_at,'weekday 0','-6 days') wk,
       COUNT(*) escalated, SUM(COALESCE(escalation_chat_id,'')<>'') with_group
FROM issues WHERE deleted=0 AND escalated_at IS NOT NULL AND escalated_at<>''
GROUP BY 1 ORDER BY 1 DESC LIMIT 12;"

# 置信度归因：月度 high 占比 vs 兜底率 vs deep 占比 vs 系统失败率
sqlite3 -header "$DB" "
SELECT strftime('%Y-%m',created_at) m, COUNT(*) n,
 ROUND(100.0*SUM(confidence='high')/COUNT(*),1) hi_pct,
 ROUND(100.0*SUM(rule_type='general')/COUNT(*),1) general_pct,
 ROUND(100.0*SUM(is_deep_analysis=1)/COUNT(*),1) deep_pct,
 ROUND(100.0*SUM(system_failure=1)/COUNT(*),1) sysfail_pct
FROM analyses GROUP BY 1 ORDER BY 1 DESC LIMIT 7;"

# 模型换代对比
sqlite3 -header "$DB" "
SELECT strftime('%Y-%m',created_at) m, agent_model, COUNT(*) n,
       ROUND(100.0*SUM(confidence='high')/COUNT(*),1) hi
FROM analyses WHERE created_at>=date('2026-06-01') GROUP BY 1,2 ORDER BY 1,3 DESC;"

# 转交积压年龄分桶
sqlite3 -header "$DB" "
SELECT CASE WHEN d<7 THEN 'a_<7天' WHEN d<30 THEN 'b_7-30天'
            WHEN d<60 THEN 'c_30-60天' ELSE 'd_>60天' END bucket, COUNT(*)
FROM (SELECT julianday('now')-julianday(escalated_at) d FROM issues
      WHERE deleted=0 AND escalation_status='in_progress') GROUP BY 1 ORDER BY 1;"
```

> **口径陷阱**（做报表前必读）：
> - `analyses` 一单多行（追问、重跑各一行），`COUNT(*)` ≠ 工单数，须 `COUNT(DISTINCT issue_id)` 或取每单最新一条
> - `issues.created_at` 是**落库时间**不是工单产生时间；批量回捞会造出假高峰。工单真实产生时间用 `created_at_ms`（飞书「创建日期」）
> - `issues.status` 是 **AI 分析状态**不是业务状态，`pending` 占比高不代表"没处理"
> - `issues` 表只是飞书的子集（`api/issues.py:47-50` 只 upsert pending 且未 analyzing/done 的）
> - oncall 用**非自然周**（值班起始日 + 7 天偏移），analytics 用自然周，两者不可混算
> - `analyses.platform` 默认 `"app"`，`issues.platform` 默认 `""`，按平台 join 会漏

### 13.2 版本解析脚本

版本号**不在** `issues.app_version`（生产实测 334/337 为空），在 `analyses.log_metadata_json`（493/493 有值）：

```python
import sqlite3, json, re
from collections import Counter, defaultdict

con = sqlite3.connect("file:/Users/mac/jarvis/data/appllo.db?mode=ro&immutable=1", uri=True)
rows = con.execute("""
 SELECT a.issue_id, a.rule_type, a.log_metadata_json
 FROM analyses a
 JOIN (SELECT issue_id, MAX(created_at) mx FROM analyses
       WHERE created_at>=? AND created_at<? GROUP BY 1) t
   ON a.issue_id=t.issue_id AND a.created_at=t.mx
""", ("2026-07-13", "2026-08-24")).fetchall()

def ver(meta):
    if not meta: return None
    m = json.loads(meta)
    v = (m.get("app_version") or "").strip()
    if v and (g := re.match(r"(\d+\.\d+\.\d+)", v)): return g.group(1)
    g = re.search(r"(\d+\.\d+\.\d+)", m.get("build_info") or "")
    return g.group(1) if g else None

vc, vp = Counter(), defaultdict(Counter)
for _, rt, meta in rows:
    if v := ver(meta):
        vc[v] += 1
        vp[v][rt or "?"] += 1

for v, n in vc.most_common(10):
    print(f"{v:10s} {n:4d}  " + ", ".join(f"{k}:{x}" for k, x in vp[v].most_common(4)))
```

`log_metadata_json` 可用字段：`app_version` / `build_info` / `os_version` / `platform` / `engine` / `device_model` / `uid` / `locale` / `file_ids` / `code_routing`。
