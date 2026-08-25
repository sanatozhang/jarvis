# Web / Desktop 工单接入与分析指南

> 面向读者：AI（Claude Code / 其他 agent）+ 工程师。目的：讲清楚 web/desktop 平台的工单**现在**是怎么被处理的、跟 App 工单比缺了什么、遇到这类工单时该怎么补位取证。
>
> 一句话结论：**web/desktop 工单今天走的是和 App 一模一样的表单，但拿不到 App 那套本地日志。真正的日志在 Datadog 里，而自动分析 pipeline 完全够不到 Datadog——这一步目前只能靠人工，或者靠一个像本次会话这样、真正挂了 Datadog 工具的交互式 AI 会话去手动查，查到后把证据粘回工单，分析才有东西可看。** 不要假设系统会自动帮你把 Datadog 数据接进去，它不会。

---

## 1. 工单怎么进来的（现状，不是设计意图）

Jarvis 里其实有两条平台相关的代码路径，**只有一条在真实运作**，容易搞混：

| 路径 | 状态 | 说明 |
|------|------|------|
| `/api/feedback`（老表 `IssueRecord`，`fb_` 前缀） | **今天实际在跑的路径** | web/desktop 工单目前就是走这里 |
| `pt_tickets`（`PlatformTicket`，`backend/app/platform_tickets/`） | **骨架已建，业务流程未接线** | 是未来给 web/mcp/desktop 用的独立存储，专属字段（url/browser/session/client/tool）设计上放 `payload_json`，但**目前没有任何代码往里面写数据**，也没有专门的提交 API（`/api/platform-tickets` 不存在）。见 `app/platform_tickets/CLAUDE.md`「当前阶段」一节，那里明确列了尚未做的事：提交 API、analysis_worker 接入、analytics 平台维度、前端 tracking/oncall 展示 |

**所以现在如果你手头有一张 web/desktop 工单，它是通过 `/api/feedback` 提交的**：`backend/app/api/feedback.py:39` 的 `platform` 字段是一个自由文本表单值（`"APP"/"Web"/"Desktop"/"MCP"`），前端页面是 `frontend/src/app/feedback/page.tsx`，平台下拉框选项由 `supportWeb`/`supportDesktop`（`app/config.py:393-395`，默认 `False`，需管理员在设置里开启）门控。

**关键点：选了 Web/Desktop 之后，表单字段和 App 工单完全同构**（`page.tsx:42-45`）——没有 url、没有 browser、没有 session id 这些字段的输入框，只有和 App 一样的 `description/category/device_sn/firmware/app_version/priority/zendesk/occurred_at`。日志文件是可选的，不上传只会弹一个"确认不带日志提交"的二次确认框，不会阻止提交。也就是说：**任何 web/desktop 专属的定位信息（发生页面/浏览器/会话），今天只能靠用户自己写在 `description` 文本里，系统没有专门字段收集它们。**

---

## 2. 日志现状：没有 `.plaud`，也没有自动查 Datadog

- App 工单的日志走 `.plaud` 私有格式，本地 ChaCha20 解密还原成可读 `.log` 文件（详见 [`ticket-analysis-internals.md`](./ticket-analysis-internals.md) 第 1.5 节）。**web/desktop 没有这种格式**——浏览器/桌面客户端根本不产出 `.plaud` 文件。
- `backend/app/services/decrypt.py` 里确实有 `_process_log_web`/`_process_log_desktop` 两个分发函数（`decrypt.py:243-287`），但**都是占位符**：docstring 原文"Web/Desktop logs do not use .plaud encryption... Extend with web/desktop-specific decryption when the format is defined"。逻辑只有"识别 ZIP 就解压，否则原样透传"，没有任何真正的解析——因为 web/desktop 日志格式目前**根本没有被定义过**。
- 更常见的情况是**根本没有日志附件**（表单允许不带日志提交）。这种情况下 `analysis_worker.py:546-550` 判定为 `has_logs=False, logs_corrupted=False`，agent 走的是"凭描述分析"模式：`agents/base.py:283-300` 的 prompt 分支，角色设定是"基于问题描述、代码仓库和产品知识来分析"，workspace 里只给 `images/ rules/ code/ output/`，**没有日志目录、没有任何外部日志源的提示或工具**。

换句话说：**今天一张 web/desktop 工单如果没人额外干预，agent 能看到的证据 = 用户自己写的那段描述文字 + 代码仓库 + 规则文件，仅此而已。** 这和真实故障现场（浏览器控制台报错、具体是哪个 API 超时、哪次 RUM session 里发生的）之间有巨大的证据缺口，而这个缺口目前**没有任何自动化在填**。

---

## 3. 为什么 AI 自己查不到 Datadog（不是没写全，是压根没接）

这条必须讲清楚，否则容易误以为"让 agent 自己去查一下 Datadog"是个简单的 prompt 调整——不是，这是工具权限层面的硬限制：

- Claude CLI 在分析 workspace 里的工具白名单（`config.yaml:40-60`）只有 `Read/Write/Grep/Glob` + 一批本地只读 shell 命令（`grep/wc/head/tail/sort/awk/sed/cat/zcat/gunzip`）。**没有 WebFetch，没有任何 MCP server 挂载**，进程物理上发不出网络请求，更别说调 Datadog API。
- Datadog 访问权限（`CRASHGUARD_DATADOG_API_KEY`/`CRASHGUARD_DATADOG_APP_KEY`）严格限定在 `env_prefix="CRASHGUARD_"` 的独立配置段（`app/crashguard/config.py`），工单分析主流程的 `Settings`（`app/config.py`）里根本没有这两个 key。`app/workers/analysis_worker.py` 全文没有 import 任何 crashguard 模块——`crashguard/CLAUDE.md` 的隔离合约（"允许的对外耦合点仅 6 个"）里也没有把 `datadog_client` 列为可被工单流程调用的耦合点。
- 唯一命中"Datadog"字符串的两处都不是真实查询：`extractor.py` 里是从本地日志文本里模式匹配 `"DatadogConfig initialized"` 这行字符串来猜 Flutter SDK 版本，和调用 Datadog 服务毫无关系；`api/settings.py` 只是把 crashguard 自己的一个配置项透出到管理页展示，不发起任何 API 调用。

**结论：这不是"AI 不知道要去查 Datadog"，而是这条通路今天在架构上根本不存在。** 如果需要，得靠人（或者一个真正连了 Datadog 工具的交互式 AI 会话，比如你现在正在用的这个）去手动查完，把结果作为文本粘回工单，分析 agent 才有东西可用。

---

## 4. 今天该怎么补位：人工 / 交互式 AI 取证流程

在自动化补上之前，遇到 web/desktop 工单，按这个顺序做：

1. **从工单描述里抠线索**：由于没有专属字段，唯一的线索来源是用户自己写的文字——用户邮箱/账号、大致时间（`occurred_at` 或描述里的时间点）、平台（web 用的浏览器、desktop 的操作系统/版本）、具体是哪个功能/页面报错。线索越模糊，Datadog 里越难定位，必要时应该先回问用户补充（比如"能否提供操作时间和使用的浏览器"）。
2. **去 Datadog 里查**：按 web 的 RUM Browser Application（或 desktop 对应的探针，如果已经埋点）搜索，过滤条件用第 1 步抠出的线索（时间窗 + 用户标识 + 平台）。**注意**：本仓库里目前没有任何代码化的"按用户/session 单点查询"工具可以复用——crashguard 的 `datadog_client.py` 虽然是最接近的参考实现，但它的能力边界是：
   - 按 issue（error tracking 分组）聚合最近 N 条 RUM 事件（`get_issue_detail`），**不是**按具体用户/session 查询；
   - 一批平台/版本维度的**计数分布**（session 数、crash-free 率等），全是聚合统计，不支持单点下钻；
   - 有一个通用 `search_logs_page`（Logs Search API 封装）理论上可以手写 query 做单点过滤，但这是内部管线用的原始接口，**不是**产品化的"查某个用户某次操作"功能；
   - **没有 RUM Session Replay 能力**——如果需要看某次会话的完整操作回放，目前代码库里没有对应实现，只能去 Datadog 网页 UI 里手动搜。
   实际操作建议：直接在 Datadog UI 里按 RUM 检索（用户 email/session/时间窗），或者如果你是交互式 AI 会话且连了 Datadog MCP 工具，直接用对应的 RUM/Logs 搜索能力查（不要往 crashguard 的聚合类查询上硬套，那是给崩溃监控大盘用的，不是给单张工单查证据用的）。
3. **查到的证据怎么喂回分析**：把关键的错误信息/请求链路/时间线**粘贴进工单描述或追问框**（走追问流程会触发重新分析，见 `ticket-analysis-internals.md` 4.5 节），让 agent 在"凭描述分析"模式下有真实证据可用，而不是空转猜测。**不要**指望上传一个从 Datadog 导出的 CSV/JSON 文件当"日志"传给 agent——`decrypt.py` 的 web/desktop 处理器只会做基本格式探测，不会理解 Datadog 导出格式的语义。
4. **参考先例**：`backend/rules/mcp.md` 是目前唯一一个为"非 app 平台、大概率没有日志"场景写的规则，第 51-52 行的原则是"无日志是正常情况，不要因为没有日志就判定 system_failure，应基于代码 + 工单描述给出回答"。web/desktop 工单可以照这个思路走，但 mcp 场景有源码可查（`code/` 目录挂载），web/desktop 未必有对应仓库挂载，如果 `needs_code` 规则没命中，agent 连代码都看不到，证据就只剩描述文本，这种情况下更要在第 1-3 步把外部证据补齐。

---

## 5. 给 AI 的速查 checklist

拿到一张 `platform="Web"` 或 `"Desktop"` 的工单时：

- [ ] 有没有附日志文件？如果有，先看 `decrypt.py` 处理后是不是真解析出了有用内容，还是只是原样透传的乱码/未知格式（透传成功不代表内容可读）。
- [ ] 工单描述里有没有时间点、用户标识、具体现象（哪个页面/哪个操作）？没有的话先建议回问用户，不要直接分析。
- [ ] 是否需要人工/交互式 AI 去 Datadog 补证据？如果证据缺口明显（比如描述只有一句"打不开"），应该先说明"当前信息不足以定位根因，建议查 Datadog 或回问用户"，而不是硬给一个低置信度瞎猜结论。
- [ ] 不要假设 `pt_tickets`/`payload_json` 里有结构化的 url/browser/session 数据——目前没有任何数据写在那里，这条路径是空的。
- [ ] 不要假设系统会自动去查 Datadog——第 3 节已确认这条路径不存在，需要显式的人工步骤。

---

## 6. 已知缺口（现状是什么，不是要不要做的设计讨论）

如果之后要真正打通 web/desktop 的自动化分析，以下是当前确认存在的缺口，按这份文档调研到的现状列出，不代表已经排期：

- 没有专门的 web/desktop 工单提交 API（`/api/platform-tickets` 不存在），前端也没有采集 url/browser/session/client/tool 这些平台专属字段的表单。
- `pt_tickets` 表结构已建好但完全没有写入路径，`payload_json` 是空骨架。
- `decrypt.py` 的 web/desktop 处理器是占位符，因为 web/desktop 日志格式本身还没有定义。
- 工单分析主流程（agent 工具白名单 + `Settings` 配置）没有任何 Datadog 访问能力，且按现有隔离合约（crashguard 的耦合点白名单）不能直接绕过去调用 crashguard 的 `datadog_client`。
- 即便打通了访问权限，crashguard 现有的 Datadog 查询能力也只有"按 issue 聚合"和"平台/版本分布统计"，没有"按用户/session 单点查询"和"Session Replay"，这两块如果要支持"自动从 Datadog 取某张工单对应的证据"，需要新写查询能力，不是简单开个权限就行。
- 没有为 web/desktop 单独写规则文件（`backend/rules/`），目前唯一可参考的"非 app 平台"先例只有 `mcp.md`。
