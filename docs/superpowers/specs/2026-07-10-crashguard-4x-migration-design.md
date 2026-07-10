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
"Native"）已经满足"4.0 置顶 + 默认展开"，不用动。只调整两点：

1. 混合的"关注/新增/突增/下降"列表里，同紧急度下 4.0 条目排序权重高于 3.x。
2. 折叠面板默认展开判定从"看标题关键字"（`feishu_card.py:633`
   `is_expanded = any(kw in title for kw in EXPANDED_KEYWORDS)`）改成"看面板内容是否含 4.0
   条目"。**实现上**：由 `daily_report.py` 在构建每个 section 时就带上一个显式的
   `has_native: bool` 标志一起传给 `feishu_card.py`，不要在 `feishu_card.py` 里对渲染好的
   文本做 emoji 字符串匹配（那样是 stringly-typed 耦合，`daily_report.py` 构建列表时本来就
   知道每条的代际）。

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

1. ~~checkout 新鲜度~~ → 由 F 的每日同步任务自动持续保证，不再需要人工盯。
2. **GitHub push/PR 权限**需要覆盖 `plaud-native-android`/`plaud-native-ios` 这两个仓——
   核实 `gh` CLI 认证账号的授权范围是否包含它们（之前只在 flutter 仓上验证过）。
3. **端到端从未真正跑通过一次**（过滤前的数据是测试噪声主导，真实正式环境 crash 极少）。
   现在已经有具体候选：过去 30 天 `env:production` 下 Android 有 15 个、iOS 有 11 个真实 crash issue。
   A 部分上线后，从这批里挑一个手动触发 `/api/crash/analyze/{id}`，观察 AI 生成的 fix_diff
   在 Kotlin/Swift 上的质量，再决定是否需要为 native 补充专门的 prompt/上下文调优（调优本身
   不在本设计范围内，先观察效果）。

### F. 每日仓库同步任务（新增，job #8）

新 cron job `repo_sync`，默认 `0 3 * * *`（凌晨3点，复用容器已有 `TZ=Asia/Shanghai`），新增
独立 kill switch `repo_sync_enabled`（沿用"每个 job 一个开关"的现有惯例）。

**覆盖范围**：只覆盖 crashguard 自己实际监控崩溃、会去开 PR 的 platform（目前 = `android`、
`ios` 这两个 `repo_routing` 配置里的 platform key），取这两个 platform 下**全部 band**
（不管是 flutter 世代还是 native 世代）解析出的 `wrapper_path`/`sub_repo_path`，去重后同步。
不涉及 web/desktop/mcp——那些是工单处理未来要支持的范围，不是 crashguard 崩溃分析/自动 PR
的范围。以后如果 crashguard 本身真的扩展监控到别的平台，F 按 platform key 自动跟上；但不会
因为工单模块支持了新平台就误伤去同步不相关的仓。

**每仓步骤**：

1. 先拿该 repo_path 的锁（复用 `pr_drafter.py` 已有的 `_repo_locks`/`_acquire_repo_lock`
   机制，防止和进行中的 auto-PR git 操作打架）。
2. 正常路径：`git fetch origin` → 用现有 `_default_base_ref`/`_resolve_remote_name` 辅助
   函数判断默认分支（main/master，兼容 detached HEAD 场景）→ `git pull --ff-only`。
3. 失败路径（以上任一步报错）：强制同步——`git fetch origin` → `git checkout -f <默认分支>`
   → `git reset --hard origin/<默认分支>`。
4. 结果记录进 `crash_job_heartbeats`（复用 `record_heartbeat` 包装器），成功/失败状态在
   `/crashguard/jobs` 页面可见，和现有 7 个 job 一致，不单独建新的告警通道。

**新增手动触发接口**：`POST /api/crash/repo-sync/run-now`，和现有 `trigger`/`warmup`/
`reports/run-now` 保持一致的模式，方便上线前在 100 测试机主动验证一次，不用等到凌晨3点。

**上线安全策略**：`repo_sync_enabled` 默认值先设为 `false`。部署后先用手动触发接口在 100
测试机验证行为符合预期，再在 102 打开这个开关——因为这是一个会做 `git reset --hard`、
无人值守碰生产仓库 checkout 的新机制，不应该一部署就默认自动跑。

**已知的良性交互（不需要特殊处理）**：F 和 `pr_drafter` 共用同一把 per-repo 锁，正常情况下
不会冲突。唯一边界情况：如果 AI 正在生成 fix_diff 期间（这一步不持锁）F 恰好跑完了同步，
`pr_drafter` 后续 `git apply` 可能因为代码变了而失败——但这本来就有现成的失败兜底（改写
`.md` 说明文档），不会导致脏状态，顶多这次 PR 尝试失败、下个 cycle 重试。

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
