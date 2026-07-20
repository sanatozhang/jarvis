# 卡顿(Jank) 接入崩溃自动化流程设计

- 日期：2026-07-20
- 作者：sanato
- 状态：设计已评审，待写实施计划

## 背景与问题

Crashguard 现在只处理"崩溃"（`kind='crash'`）和"ANR/App Hang"（`kind='anr'`），走同一套自动化：Datadog 摄入 → 符号化 → AI 分析 → PR gate 链 → 开 PR → 分发给对应同学。用户提出：Datadog 卡顿看板（`native-page-health-perf-stability-by-page`, dashboard id `yfc-uds-4d5`）上能看到卡顿堆栈，但这些堆栈没有符号化；希望比照崩溃，把卡顿也接入这套自动化。

### 现场核实：看板上的"卡顿"其实是两种完全不同的数据源

直接调用 Datadog API 拉了该看板的 widget 定义（`GET /api/v1/dashboard/yfc-uds-4d5`）逐条核对，结论：

**分支 A — ANR / App Hang**：`data_source: rum`，`@type:error @error.category:(ANR OR "App Hang")`，看板上还有一个 `data_source: "issue_stream"` 的 widget，说明这部分走的是 Error Tracking 的 issue 管道。

现场查了 102 生产库（`crash_issues` 表）验证：**已经在跑**，无需任何改动。

```
total crash_issues: 757
kind distribution: [(305, 'anr'), (332, 'crash'), (3, 'memory'), (117, 'web_warning')]
```

`kind='anr'` 的 305 条里 iOS/Android 都有，标题如 `AppHang @ dart::Utils::VSNPrint(...)`、`com.datadog.android.rum.internal.anr.ANRException @ invokeSuspend`，已经和普通崩溃一样在走摄入→符号化→AI分析→PR全链路（`config.py:157` 的 `datadog_query_fatal` 默认值本来就含 `@error.category:ANR OR @error.category:"App Hang"`）。**本设计不涉及分支 A**。

**分支 B — `jank_watchdog_block`**：`data_source: "logs"` / `"logs_stream"`，`@category:performance jank_watchdog_block`。这是**纯 Datadog Logs 事件**（App 主线程阻塞 >200ms 时客户端自己打的日志），完全不经过 Error Tracking，Datadog 不做任何 issue 分组/去重。这才是用户说的"看到堆栈但没符号化"的那部分。**本设计的全部范围。**

### 现场抓样本：`jank_watchdog_block` 日志字段结构

用 Datadog Logs Events Search API 抓了 40 条真实样本（近14天），关键发现：

- **平台分布**：iOS 12 / Android 28。
- **每条日志自带符号化所需的全部原料**：
  - iOS：`stack_pcs`（逐帧地址）、`stack_modules`（逐帧模块名）、`stack_module_bases`/`stack_module_offsets`（逐帧基址/偏移）、`app_stack_module`（应用自身模块名，如 `Plaud-Global`）、`app_stack_pc`/`app_stack_module_offset`（应用帧的地址/偏移，单帧，非数组）
  - Android：`app_stack_frame`（已是可读文本，如 `ai.plaud.android.plaud.monitoring.DatadogConfig.trackTransaction`）、`stack_trace`（完整 Java/Kotlin 栈，样本里的类名/方法名均未见混淆迹象）
  - 两端通用：`has_app_frame`（布尔，本次事件的调用栈里有没有落到我们自己代码模块的帧）、`os`（platform+version）、`version`/`build_version`（App 版本）、`page`/`view`（页面标识）、`duration_ms`（阻塞时长）
- **`has_app_frame` 分布**：40 条里大多数（`android.os`/`QuartzCore`/`libsystem_kernel.dylib` 等系统框架内部阻塞）没有任何我们自己代码的帧，`has_app_frame=False`——这类卡顿我们的符号表帮不上忙，也不可能产出有意义的 PR。
- **Datadog 自带的 `stack_signature` 太粗，不能当聚合键**：40 条样本只有 15 种取值（如 `android.os`/`QuartzCore`），本质是"顶层框架名"，会把大量不相关的卡顿点合并到同一个桶。
- **事件量级**：近4h 42条，近24h 73条，近7天1020条——4h 一个 tick 拉 50~100 条量级，Logs Search API 分页完全够用，不存在配额风险。

## 目标

1. 把 `jank_watchdog_block` 日志接成 `crash_issues` 里的新 `kind='jank'`，具备和崩溃一样的生命周期（长期挂号、`total_events`/按天 `CrashSnapshot.events_count` 累加、`first_seen_at`/`last_seen_at`）。
2. 用平台已有的符号表资产（dSYM / ProGuard mapping，复用现有 `github_symbols.py` + `symbolication.py` 下载/缓存机制）符号化卡顿事件里"应用自身模块"那一帧。
3. 建立"值不值得自动分析/开 PR"的准入判断：没有应用自身帧的卡顿（`has_app_frame=False`）永久排除在 AI 分析/PR 之外；有应用帧但符号化质量不够（复用现有 `_stack_quality_label`）的也先不进分析。
4. 复用现有崩溃 PR 全套 gate 链（feasibility 阈值、confidence/feasibility gate、平台校验、路径校验……）和 PR 分发（reviewer 分配、飞书通知），不新增判断逻辑。
5. 崩溃看板新增"卡顿" type 筛选；早/晚报新增"卡顿"板块，严格遵循"没有异常就不显示整段"的既有约定（不新增"总是显示"的板块）。

## 非目标

- 不做分支 A（ANR/App Hang）——已经在跑，本设计不改动 `categorizer.py`/`datadog_query_fatal` 相关代码。
- 不做二期的"卡顿全自动"里超出"接入现有崩溃自动化流程"范围的部分（比如新的分发策略、新的通知渠道）——分发本来就是崩溃流程自带的，接入后自动获得，不需要专门设计。
- 不解决 Android 混淆(ProGuard)场景的完整验证——现有样本里没见到混淆迹象，设计里预留了走现有 retrace 基础设施的分支，但"是否真的会遇到混淆帧"作为实施阶段的验证项，不在本设计里下定论。
- 不改动 `stack_signature`（Datadog 自带字段）本身，只是设计里不采用它做聚合键。

---

## §1 数据摄入 — 新模块 `services/jank_ingester.py`

挂在现有 `workers/pipeline.py` 的每 4h tick 里新增一步（不新建独立 cron）：

- 调 Datadog Logs Search API，`query = "@category:performance jank_watchdog_block"`
- 时间窗口：`cursor(上次成功摄入的 to 时间) → now`，cursor 持久化（沿用现有 `CrashAuditLog` 或类似机制记一条 `op="jank_ingest"` 的最近成功时间，而非固定"过去4小时"窗口，避免 tick 延迟/失败导致漏抓)
- 分页拉取，逐条处理

### 聚合键（合成 issue 身份）

Datadog 不给这类日志分组，需要自己算一个**符号化前**的聚合键（选择理由：不用等符号化完成就能正确分桶，同一处卡顿反复出现能立刻累加到同一个 issue，不会因为符号化排队而临时炸出一堆重复 issue）：

| 场景 | 聚合键计算 |
|---|---|
| iOS，`has_app_frame=True` | `sha1(f"{platform}:{app_stack_module}:{app_stack_pc}")` |
| Android，`has_app_frame=True` | `sha1(f"{platform}:{app_stack_frame}")`（该字段本身已是可读文本，天然稳定） |
| `has_app_frame=False`（任意平台） | `sha1(f"{platform}:{stack_top_module}:{stack_top_symbol}")`（仅用于统计可见性分桶，不追求精确） |

用这个哈希的前 16 位加前缀构造 `datadog_issue_id = f"jank:{hash16}"`（前缀避免和真实 Datadog Error Tracking 的 issue id 撞车——`datadog_issue_id` 目前是 `CrashIssue` 的唯一业务键）。

### upsert 逻辑

现状核实：`CrashIssue` 上并没有 `events_count` 字段，累计数是 `total_events`；真正驱动早报"当日新增/激增"判断的 attention pool（`daily_report.py`）是按天 join `CrashSnapshot`（`events_count` 字段实际长在这张表上，`snapshot_date` 为维度）来选的。所以每条日志摄入时要同时维护两张表，否则卡顿 issue 摄入了也永远进不了 attention pool：

- **`CrashIssue`**：命中已有同 `datadog_issue_id` → `total_events += 1`、刷新 `last_seen_at`；未命中 → 新建一行：
  - `kind = "jank"`
  - `fatality = "jank"`（见 §5，不走现有 fatal/non_fatal 判定）
  - `fixable = has_app_frame`（新字段，见 §2）
  - `title`：仿照现有 `"AppHang @ ..."` 惯例，写成 `f"Jank @ {app_stack_frame or f'{stack_top_module}::{stack_top_symbol}'}"`
  - `platform`、`representative_stack`（原始 `stack_trace` 文本，符号化前占位）
  - 若 `fixable=True`：立即尝试符号化（见 §3），成功则覆盖 `representative_stack`
- **`CrashSnapshot`**：按 `(datadog_issue_id, snapshot_date=today)` upsert，`events_count += 1`（当天第一条则新建，`app_version` 取事件的 `version` 字段）——这一步是 §4 阈值判断能生效的前提

## §2 新字段：`CrashIssue.fixable`

`models.py` 里 `CrashIssue` 新增：

```python
fixable = Column(Boolean, default=True)  # False = 没有应用自身代码帧，永久不进 AI 分析/PR
```

- 现有所有行（crash/anr/memory/web_warning）迁移时默认 `True`，行为不变。
- 只有 `kind='jank'` 且 `has_app_frame=False` 的新行会被显式设为 `False`。
- 迁移方式：本仓库自研轻量迁移（无 Alembic），在 `migrations.py::_REQUIRED_COLUMNS` 追加一行 `("crash_issues", "fixable", "BOOLEAN", "1")`，`ensure_columns()` 启动时自动 `ALTER TABLE` 补列。

## §3 符号化 — 新函数，新建 issue 时同步跑一次（不做惰性 prewarmer）

因为每条 jank 日志只给"应用自身模块"**单帧**地址（不是整段多帧堆栈），符号化成本很低，设计为**新建 issue 那一刻同步跑一次**，不复用/新建整套惰性重试管道（`distribution_prewarmer.py` 那种"多帧+`get_issue_detail`"模式对这里是杀鸡用牛刀）。

- **iOS**：新增 `symbolication.py::symbolicate_jank_frame(module: str, pc_or_offset: str, platform: str, app_version: str, symbol_profile: str, github_repo: str) -> str`
  - 复用现有 `_get_or_download_ios_dsym()` / `_find_dwarf_in_dsym()` 拿到 dwarf 路径（跟崩溃符号化走同一套下载/缓存/GH_TOKEN 鉴权逻辑，本次会话前半段修的 GH_TOKEN bug 在这里同样生效）
  - 跳过整段堆栈解析（`_symbolicate_ios`），直接调已有的单帧函数 `_atos_lookup(dwarf_path, base, addr)`
- **Android**：`app_stack_frame` 已是可读文本，默认直接使用；若探测到疑似混淆模式（预留分支，具体判定规则留到实施阶段核实是否真的会遇到），走现有 ProGuard retrace 基础设施做单符号查表
- **失败重试**：复用 `CrashIssue` 已有的 `prewarm_attempts`/`prewarm_last_error` 字段（本次会话前半段的 bug 修复后，这两个字段的语义已经理顺），不新造计数字段
- 符号化完成后，用现有 `services/datadog_client.py::DatadogClient._stack_quality_label()` 静态方法判断质量（`symbolicated_native`/`raw`/`aot_pointers_unsymbolicated`/`empty` 等），口径跟本次会话前半段修的 GH_TOKEN/prewarm bug 完全一致，不新增分类标准

## §4 进自动分析的准入门槛

### 4.1 `fixable` 硬性排除

任何 `fixable=False` 的行永久不进入 AI 分析/PR 候选（不管事件量多高）。

### 4.2 卡顿专属阈值（不动现有崩溃/ANR阈值）

现状核实：`daily_report.py` 的 attention pool 构建逻辑**完全不按 `kind` 过滤**，只看 `fatality` + 当日事件数阈值（`attention_min_events` 默认100、`daily_new_issue_min_events` 默认10、`daily_surge_driver_min_events` 默认50）。卡顿量级天生比崩溃分散（14天40条样本分布在15个聚合桶），换算下来单个具体卡顿点日均事件数远够不到这些阈值，直接接入基本一条都进不去。

新增独立配置项（`config.py`）：

```python
jank_attention_min_events: int = 5
jank_daily_new_issue_min_events: int = 3
```

在 `daily_report.py` 的 pool 构建处按 `kind == "jank"` 分支使用这组更低的阈值；同时叠加过滤条件：`fixable=True` 且 `stack_quality` 不在"未符号化"集合（`raw`/`aot_pointers_unsymbolicated`/`empty`）里，才进入候选池。crash/anr 现有逻辑不动。

## §5 崩溃看板 — 新增 `jank` 分类

现状核实：看板"fatal/non_fatal"筛选（前端 `frontend/src/app/crashguard/page.tsx` 的 `fatalityFilter` 状态）背后是独立的 `CrashIssue.fatality` 字段（`String(16)`，现有 `fatal`/`non_fatal`/`unknown` 三态，非严格枚举），跟 `kind` 是两套独立分类；`fatality` 的赋值来自 Datadog 双路查询（`datadog_query_fatal`/`datadog_query_nonfatal`）命中哪路。

因为卡顿摄入本来就是全新的独立通道（不走这套双路查询），最省事的做法是：**摄入卡顿时直接把 `fatality` 赋值为 `"jank"`**（新增第三个字符串值），不改动现有 fatal/non_fatal 判定链路。

- 前端改动：`page.tsx` 的 `fatalityFilter` 类型联合加 `"jank"`；KPI 卡片/筛选新增一个"🟠 卡顿"选项，样式仿照现有"🔴 严重崩溃"/"⚠️ 业务失败"
- 后端改动：核对现有按 `fatality='fatal'`/`'non_fatal'` 做汇总统计的代码点（KPI 总数等），确认没有硬编码"只有两种取值"导致 jank 行被汇总时静默漏掉——这是实施阶段要过一遍代码确认的点，改动量应该不大

## §6 早/晚报 — 新增"卡顿"板块，遵循"空则不出"约定

现状核实：`daily_report.py::compose_report` 里现有的"🆕 4.0 Native 崩溃板块"就是"无 native fatal 崩溃则整段不出"（源码注释明写"受报告只显示异常约束"）——这是既有约定（团队铁律：reporting 出口有问题才显示，没问题不显示，不新增总是显示的板块）。

新增"卡顿"板块严格仿照同一模式：

- 只统计 `kind='jank'` 的行：今日新增的卡顿 issue + 事件数激增的已有卡顿 issue
- 两类都没有 → `jank_lines = []`，模块完全不出现，不留孤零零的标题
- 有内容才输出 `## 🟠 卡顿` 标题 + 简要列表（issue 标题、符号化后的应用帧、事件数），插入位置紧跟现有 Native 崩溃板块之后
- 拼接方式复用现有 `lines[insert_at:insert_at] = [...]` 写法，跟其余模块保持一致

## §7 分析 + PR + 分发 — 完全复用，零改动

一旦通过 §4 的准入门槛进入 attention pool，走的就是和崩溃/ANR完全一样的链路：`analyzer.start_analysis` → feasibility 打分 → 现有全套 PR gate 链（confidence/feasibility gate、平台强制路由校验、路径存在性校验、版本号保护、关键词命中、语法速检……）→ `pr_drafter` 开 PR → 现有 reviewer 分配 + 飞书通知。**这条链路本身不改一行代码**——这是"把卡顿当崩溃处理"这个设计的核心价值：绝大部分自动化能力是白拿的，本设计的全部工作量集中在"怎么把卡顿变成一个合格的 `CrashIssue` 喂进去"。

---

## 风险与后续验证项

1. **Android 混淆场景未验证**：现有样本里的 `app_stack_frame`/`stack_trace` 均未见混淆迹象（完整包名+方法名+行号），但样本量小（14天40条，Android 28条）。实施阶段需要扩大样本核实是否存在混淆帧，若存在需要补充 ProGuard retrace 单符号查表的具体实现。
2. **卡顿专属阈值的初始值（5/3）是经验估计，不是精确计算**——上线后应该观察1~2周实际进入分析的 issue 数量，按需调整。
3. **`fatality` 字段的下游硬编码点**未逐一穷举，需要在实施阶段过一遍代码确认。
4. **聚合键的跨版本稳定性**：iOS 用原始地址(`app_stack_pc`)做键，App 升级后同一处代码的地址可能变化，导致同一个卡顿点跨版本被当成新 issue（这跟崩溃处理里"版本升级后地址失效"是同一类已知限制，`stack_fingerprint`/`compute_fingerprint` 机制目前也是靠符号化后的文本做跨版本匹配）——本设计 v1 不解决这个问题，符号化完成后如果需要跨版本合并，可以后续再补一层"基于符号化后函数名的二次归并"，不在本次范围内。
