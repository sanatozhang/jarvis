# Crashguard 4.0 native 迁移设计

日期：2026-07-10
状态：待实施计划（writing-plans）

## 背景

App 正在从 Flutter（3.x）迁移到 native（4.x），4.0 即将上线。`repo_router.py` 已经按
(platform, version) 路由源码/PR 目标仓/符号化/Datadog service filter（切换线
`4.0.0`，见 `docs/superpowers/specs/2026-06-26-repo-routing-by-type-version-design.md`），
已合并 main 并部署到测试机 100 + 生产机 102。

本设计要把 crashguard（崩溃自动化分析 + 自动开 PR 模块）和相关的早报 / PR 提醒的**运营重点**
迁移到 4.0，同时明确：

- 客服工单模块继续同时支持 3.x 和 4.x（现阶段仍以 3.x 为主），不受本设计影响。
- web / desktop / mcp 未来会需要工单处理支持，但**不属于**本设计范围（本设计只覆盖
  crashguard 崩溃监控 + 自动 PR，不涉及给这些平台做崩溃分析/自动开 PR）。

## 现状盘点（已经做过，不需要重做）

| 能力 | 现状 | 位置 |
|---|---|---|
| 按版本路由源码/PR目标仓 | 已合并 main，已部署 | `app/services/repo_router.py` |
| crashguard 自动 PR 支持 4.0 native | 已支持（`_select_candidates` 对非 flutter family 直接走 repo_router 解析出的单一 submodule 路径） | `app/crashguard/services/pr_drafter.py:65-86` |
| 早报代际角标（🆕4.0/🦋3.x）+ 置顶专属段 | 已合并 main | `app/crashguard/services/daily_report.py:36-58,1584-1619`；`feishu_card.py:601` |
| 工单模块代际角标 | 已合并 main（`code_routing` 字段 + `CodeRoutingBadge` 组件） | `app/workers/analysis_worker.py:69-97`；`frontend/src/components/AnalysisResultView.tsx:24-58` |

## 关键实测数据（2026-07-10，用生产 Datadog API key 直接查证，非估算）

**订正**：设计初稿一度错误地认为 4.0 native 的 RUM 事件不带 `env` tag，改用
`application.id`/`application.name`（两个独立注册的 RUM Application）来区分测试/正式。
复核时发现遗漏——事件确实带标准的顶层 `env:*` tag（在事件 JSON 的 `tags` 数组里，不在
`attributes.attributes` 内层，之前只翻了内层字段所以漏看了）。**正确的过滤依据是 `env:production`
这个 Datadog 保留 tag，不是 `application.name`。**

- Android/iOS 的 `env` 取值只有两种：`env:production` / `env:development`（抽样近30天
  100条事件未见第三种值，如 staging/qa）。
- 30 天窗口下 Android fatal crash issue（`@error.is_crash:true`）：`env:production` 15 个，
  `env:development` 37 个，不过滤 50 个（`application.name` 那套只查出 14 个 prod，比
  `env:production` 少 1 个——`application.name` 和 `env` 是两个不完全重合的维度，`env` 才是
  更权威、更贴合"只对 production 环境分析"这个诉求的信号，采用 `env:production`）。
- iOS 30 天：`env:production` 11 个，`env:development` 31 个，不过滤 37 个。
- 语法核实：`(service:plaud_android AND env:production) @error.is_crash:true` = 15、
  `(service:plaud_ios AND env:production) @error.is_crash:true` = 11，和分别单独按 `env:`
  过滤的结果完全一致，确认嵌套括号 AND/OR 优先级在这个 facet 上工作正常。
- flutter（3.x）抽样近30天100条事件、以及 fatal issue 查询，`env` **只出现过 `production`
  一种取值**（`env:development` = 0），说明 3.x 现有监控不存在这个噪声问题，维持不加限制。
- 已知的 Datadog Error Tracking issues search 返回上限约 100 条/次查询（项目既有记忆里
  `plaud-flutter 102(封顶)` 已经记录过），排查过程中一度看到"过滤前后总数相同"的假象就是这个
  封顶造成的，与过滤逻辑本身无关；已用远低于封顶量级的单服务查询隔离验证过滤逻辑本身正确。

## 设计

### A. Datadog 正式环境过滤（新增能力，纯配置改动）

`_inject_service()`（`datadog_client.py:71-86`）把 `self.service_filter` 原样前置拼接进
每一次查询，全模块 ~15 处调用点（fatal/nonfatal issue search、hourly_alert、core_metric、
RUM 会话计数、版本分布预热、`top_user_version` 等）自动统一生效，**不需要改代码**。

改 `config.yaml` 的 `crashguard.datadog_service_filter`（同步改 `config.py:142` 默认值），从：

```
(service:plaud-flutter OR service:plaud_android OR service:plaud_ios)
```

改成：

```
(service:plaud-flutter OR (service:plaud_android AND env:production) OR (service:plaud_ios AND env:production))
```

配置旁边加注释，记录：
- `env` 目前只有 `production`/`development` 两种取值（核实于 2026-07-10）；
- 核实用的 curl 命令（查 RUM events / error-tracking issues search，见上面"关键实测数据"）；
- **设计取舍**：用白名单精确匹配 `env:production`，不用黑名单排除 `env:development`——白名单
  失败即排除（以后出现新的 env 取值，比如 `staging`/`qa`，会被自动挡在外面，符合"只关注正式
  环境"的初衷），黑名单失败即放行（新出现的、不叫 `development` 的非正式环境会被误当成正式环境
  漏进来）。不要"优化"成黑名单。

flutter（3.x）不加任何限制，维持现状。

**已知副作用（预期行为，非 bug）**：上线后 4.0 板块数据量会骤降（正式环境用户量目前还小）；
历史上由测试 App 产生的 `crash_issues` 记录不做清理，任其自然过时退出 Top N / 关注池。

### B. 早报展示权重转移（调整展示，不改告警逻辑）

`hourly_alert`/`core_metric` 的阈值、触发条件完全不变。现有"🆕 4.0 Native 崩溃"置顶段
（`daily_report.py:1584-1619`）+ 默认展开（`feishu_card.py:601` `EXPANDED_KEYWORDS` 含
"Native"）已经满足"4.0 置顶 + 默认展开"，不用动。只调整一点：

混合的"关注/新增/突增/下降"列表里，同紧急度下 4.0 条目排序权重高于 3.x（次级排序 key，
主排序仍是 events/涨跌幅）。

**订正（写计划时核对代码发现）**：设计初稿还提议改"折叠面板默认展开判定"（从看标题关键字改成
看面板内容是否含 4.0），经核实这是在解决一个不存在的问题——`feishu_card.py:633` 的
`EXPANDED_KEYWORDS` 只匹配"✨ 今日关注点"/"🆕 4.0 Native 崩溃"/TL;DR 这三类大段的标题，
真正的平台明细段落（`PLATFORM_DISPLAY` 定义的"🍎 iOS"/"📱 Android"）标题里从来不含这些
关键字，本来就是默认折叠，不存在"因为标题碰巧含新增/突增被误展开"的场景。已有机制（Native
专属置顶段 + 关注点行内 badge）已经完整覆盖"让 4.0 内容显眼"的诉求，这一点从设计中移除，
不需要改 `feishu_card.py`。

因为 A 部分上线后短期内正式环境 4.0 崩溃数据会很稀疏，大部分面板短期内会以 3.x 内容为主、
默认收起，4.0 的可见性主要靠置顶专属段落撑着——这是预期中的效果，不需要额外调整。

### C. 10点 PR 待审核汇总加代际角标 + 置顶（`pr_pending_review_alert.py`）

复用 `version_util.classify_generation`。把 `daily_report.py` 里的 `_GEN_BADGE` 映射表
（`🆕4.0`/`🦋3.x`）挪到 `version_util.py` 作为共享常量，避免两处重复定义。

- 每条 PR 前加代际 emoji 角标。
- 4.0 的 PR 整体置顶，组内维持原有排序规则不变。

### D. crashguard 前端详情页角标（新增，前后端各一小块）

- 后端：`api/crash.py` 的 issue 序列化（`:1193` 附近）新增一个 `generation` 字段，调用
  `classify_generation(service, top_app_version 或版本范围)`，值为 `"native"`/`"flutter"`。
- 前端：`frontend/src/app/crashguard/page.tsx` 的 `DetailDrawer`（`:2157` 附近渲染
  `detail.service` 原始文本的地方）新增彩色 badge，视觉对齐工单模块的 `CodeRoutingBadge`
  （`frontend/src/components/AnalysisResultView.tsx:24-58`）。因为 crashguard 前端目录独立
  于工单模块（符合隔离原则），建一个轻量等价组件而不是跨目录直接复用。

### E. auto-PR for native — 上线前验证清单（非代码改动，人工核对）

机制上 crashguard 自动 PR 已经支持 4.0（`pr_drafter.py` 通过 `repo_router` 按版本路由到
`plaud-native-android`/`plaud-native-ios` 子仓，走和 flutter 完全相同的
checkout→commit→push→`gh pr create --draft` 流程，无 native 专属分支或缺口）。但以下三点
需要人工核实/验证，不是代码工作：

1. ~~checkout 新鲜度~~ → **订正（实施阶段发现）**：F 部分原计划新建独立同步任务，实施时
   发现 `app/services/repo_updater.py::repo_update_loop()` 早就是一个已经在 `main.py`
   启动时注册运行的、独立于 crashguard 的夜间仓库同步机制（2-6点随机窗口，覆盖全部
   platform，遇到 mt workspace/submodule 壳还会先 `mt reset --hard`/递归 `git reset`）。
   这个既有机制本来就在持续保证 checkout 新鲜度，F 的新建任务反而是重复建设——已废弃，
   详见 F 节。真正需要修的是 `pr_drafter` 和这个既有任务之间从未协调过锁，见 F 节。
2. **GitHub push/PR 权限**需要覆盖 `plaud-native-android`/`plaud-native-ios` 这两个仓——
   核实 `gh` CLI 认证账号的授权范围是否包含它们（之前只在 flutter 仓上验证过）。
3. **端到端从未真正跑通过一次**（过滤前的数据是测试噪声主导，真实正式环境 crash 极少）。
   现在已经有具体候选：过去 30 天 `env:production` 下 Android 有 15 个、iOS 有 11 个真实 crash issue。
   A 部分上线后，从这批里挑一个手动触发 `/api/crash/analyze/{id}`，观察 AI 生成的 fix_diff
   在 Kotlin/Swift 上的质量，再决定是否需要为 native 补充专门的 prompt/上下文调优（调优本身
   不在本设计范围内，先观察效果）。

### F. pr_drafter 与 repo_updater 共享跨进程仓库锁（订正后的最终方案，替代原"每日仓库同步任务"）

> **2026-07-10 实施阶段订正**：本节原计划新建独立的 `repo_sync` cron job（job #8，含
> `repo_sync_enabled` 开关、`POST /api/crash/repo-sync/run-now` 手动触发接口）。已实现并
> 通过 review（commit `654c4ad`），但随后发现 `app/services/repo_updater.py::repo_update_loop()`
> 早就是一个已经在 `main.py:168-169` 启动时注册运行的、独立于 crashguard 的夜间仓库同步机制
> （2-6点随机窗口，覆盖全部 `repo_routing` platform，遇到 mt workspace/submodule 壳还会先
> 做 `mt reset --hard`/递归 `git reset`）。原方案不仅与这个既有机制部分重复，更关键的是
> **暴露了一个更早就存在、与本次迁移无关的老问题**：这个既有任务用的是跨进程文件锁
> （`workspace_lock`，基于 `fcntl.flock`，锁文件 `$wrapper/.jarvis.lock`），而 `pr_drafter`
> 开自动 PR 时的 git 操作只用自己进程内的 `asyncio.Lock`，两者从未协调过，存在竞态。
>
> 已 `git revert 654c4ad`，改为下面的方案：不新建同步任务，而是让 `pr_drafter` 也去拿
> `workspace_lock`。方向不能反过来（让 `repo_updater` 去拿 `pr_drafter` 的 `asyncio.Lock`）——
> `repo_updater._update_repo` 跑在线程池 executor 里（`repo_update_loop` 用
> `run_in_executor` 卸载阻塞的 git 调用），`asyncio.Lock` 不是跨线程安全的东西。

**实现**：`app/services/mt_runner.py` 提取出 `_flock_acquire`/`_flock_release` 内部 helper，
供既有的 `workspace_lock`（同步 contextmanager，外部行为不变，`repo_updater.py` 和 Jenkins
release API 两个既有调用方不受影响）和新增的 `acquire_workspace_lock_async`/
`release_workspace_lock_async`（异步安全的獲取/释放对，阻塞的 flock 等待通过
`asyncio.to_thread` 跑，不卡事件循环）共用同一套逻辑。`pr_drafter.py` 在每次调用
`_create_one_draft_pr`（父仓 PR + 每个 submodule PR，各自独立獲取/释放，因为 flock 是
per-fd 语义，同进程重复獲取同一把锁会自锁超时）前后用 `try/finally` 套上这把锁，key 用
`repo_router` 解析出的 `wrapper_path`；`repo_override`/静态兜底路径没有 wrapper 概念时
跳过，不额外加锁。

**已知的残留缺口（记录，非本次范围）**：`repo_updater._update_repo` 只在 mt-workspace 和
submodule-壳（native/flutter 的 wrapper）两个分支里拿 `workspace_lock`；web/desktop 的
wrapper（`plaud-web`/`fe-nexus`）是"纯 git 仓"分支，本来就没加这把锁——所以这次修复对
android/ios（flutter+native）完整生效，但 web/desktop 这两个平台上 `pr_drafter` 和
`repo_updater` 仍然没有协调（`pr_drafter` 目前也不会给 web/desktop 开自动 PR，实际风险
很低，留作已知缺口不在本次修）。

### G. 明确不动的范围

- 客服工单模块（`app/api/{issues,feedback}.py` + `analysis_worker.py`）已经支持 3.x/4.x
  双版本，代际角标（`code_routing`/`CodeRoutingBadge`）已经存在，维持"以 3.x 为主"的现状，
  本设计不改动。
- web / desktop / mcp 的工单处理支持是未来独立需求，不在本设计范围内。

## 测试

- A：config 改动无需新增单测（`_inject_service` 是纯字符串拼接，已有测试覆盖该函数本身）；
  上线前人工用 curl 对比过滤前后的 issue 数量做一次 sanity check（本设计已经做过一轮，
  上线后按同样方法复核）。
- B：`daily_report.py`/`feishu_card.py` 现有测试基础上补：`has_native` 标志驱动的展开判定、
  混合列表按代际权重排序的用例。
- C：`pr_pending_review_alert.py` 补代际角标渲染 + 置顶排序的用例；`version_util.py` 补
  `_GEN_BADGE` 迁移后的引用测试（daily_report 和 pr_pending_review_alert 都要能正确取到）。
- D：后端 `api/crash.py` 序列化补 `generation` 字段的单测；前端新增 badge 组件的渲染测试
  （若前端有对应测试基础设施）。
- F：新增 `repo_sync` job 的单测（mock git 命令，覆盖正常路径 + 强制重置路径 + 锁获取）；
  `/api/crash/repo-sync/run-now` 的接口测试。
- 全量回归：`pytest tests/crashguard/ -v` + `lint-imports`。

## 部署与回滚

- A/B/C/D 是纯配置或加法性改动（新字段、新展示逻辑），风险低，随正常发布节奏走。
- F 因为涉及无人值守的 `git reset --hard`，遵循上面"上线安全策略"：先部署代码但
  `repo_sync_enabled=false`，人工在 100 用手动触发接口验证过行为，再在 102 打开开关。
- 回滚：A 部分如果发现正式环境过滤过严（比如以后出现了未预料到的第三种 `env` 取值），
  直接把 `datadog_service_filter` 配置改回不带 `env:production` 限制的版本即可，无需回滚
  代码。F 如果出问题，直接把 `repo_sync_enabled` 改回 `false` 即可停用，不影响其它 6 个已有
  job。
