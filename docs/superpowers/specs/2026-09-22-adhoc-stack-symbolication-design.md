# Ad-hoc 堆栈符号化工作台 — 设计

- 日期：2026-09-22
- 状态：已评审（用户批准范围，含符号包预热）
- 模块：crashguard（前后端）+ 只读依赖 graygate `focus_version`

## 1. 背景

jarvis 的符号化引擎（`app/crashguard/services/symbolication.py`，1300 行）已经很完整：
Plan A（Flutter engine 公开符号）/ Plan B（用户上传包，按 UUID/BuildId 匹配）/
Plan C（GitHub Release 按版本下载）三级回退，iOS `atos` + Android `addr2line` + ProGuard retrace。

`POST /api/crash/symbolicate`（`api/crash.py:3724`）已经把这套能力暴露成 ad-hoc 端点：
收 `stack` + `platform` + `app_version`，同步返回符号化结果。

**缺口是：前端一个入口都没有**（`frontend/src/` 内 `symbolicate` 零引用），这个端点目前只能 curl。
研发拿到一段零散堆栈、QA 拿到灰度包崩溃，都没有地方可用。

### 1.1 现有端点的三个实际问题

1. **先干活再抱怨**：会先花几十秒到几分钟尝试下载 dSYM（~90MB），跑完符号化后才在
   `warnings` 里说「无符号包」。用户白等。
2. **误导性 warning**：`available_symbol_packages` 只查 `CrashSymbolPackage`（本地上传表），
   完全不看 GitHub Release。所以 Plan C 下载成功、符号化完美时，**照样**吐出
   「该 (platform, app_version) 无已上传符号包」。这条 warning 是纯噪音。
3. **成功/失败不可分**：只返回 `changed: bool`。但 `_symbolicate_ios_with_dir` 是**故意**跳过
   系统库帧的（见该函数 docstring：不做 module gating 会「每一帧都成功吐出看似合理实则
   完全无关的 Plaud 符号，比原样保留地址更具误导性」）。于是一次正常成功的符号化里也有
   一半帧是裸地址，用户会误判为失败。

## 2. 核心结论：版本号能否从堆栈自动解析

**分场景，而且最高频的场景解析不出来。**

| 堆栈来源 | app 版本号 | UUID / BuildId | 能否自动 |
|---|---|---|---|
| iOS Apple `.ips`（iOS 15+，JSON header） | ✅ `app_version` + `build_version` | ✅ `usedImages[].uuid` | **全自动** |
| iOS Apple `.crash`（旧文本） | ✅ `Version: 3.18.0 (708)` | ✅ `Binary Images:` 段 | **全自动** |
| Datadog RUM 复制的 `@error.stack` | ❌ | ❌（`binary_images` 是旁边的独立字段） | 需手填 |
| **Android Java/Kotlin ProGuard 混淆栈** | ❌ 零元数据 | ❌ | **物理不可能** |
| Android logcat 完整段 | ⚠️ 偶有 `versionName` | ❌ | 尽力 |
| Android tombstone / native | ❌ 无 app 版本 | ✅ `BuildId:` | 半自动 |

最典型的 Android 输入就是：

```
java.lang.NullPointerException
	at a.b.c(Unknown Source:12)
```

里面没有任何版本信息。且 Plan C 是**按版本号取 GitHub Release 资产**的，UUID/BuildId 只能
命中已上传到本地的 Plan B 包。

**结论：手动指定版本号必须保留为主路径。** 自动解析只在 iOS Apple 崩溃报告上能全自动。
但通过「版本下拉候选 + 解析预填」，用户的实际操作从"手打版本号"降级为"从下拉里挑"或
"确认预填值"，出错率大幅下降。

## 3. 范围

### 做

1. 后端堆栈解析器 `stack_inspector.py`（纯函数，6 种格式）
2. `POST /api/crash/symbolicate/inspect` — 解析元数据 + repo 路由预览
3. `GET /api/crash/symbolicate/versions` — 版本候选（已上传包 + 两仓 GitHub Release）
4. `POST /api/crash/symbolicate/preflight` — 三档符号可用性预检 + 缺失时的出路
5. 改造 `POST /api/crash/symbolicate` — 逐帧统计 + 修掉误导性 warning
6. 前端新页面 `/crashguard/symbolicate`
7. 两个 `keep_versions` 配置 10 → 20
8. 符号包预热（按 graygate 主要版本，走 crashguard job queue）

### 不做（YAGNI）

- 符号化结果存库 + 可分享链接（先让用户复制粘贴）
- 崩溃**文件**拖拽上传（`.ips`/`.crash` 文件）——用户明确不要；仅支持粘贴文本
- 异步任务表 + 轮询（见 §4.3 决策）

## 4. 三个架构决策

### 4.1 堆栈解析器放后端

前端 TS 解析器能做到贴进去瞬间预填、零往返。但项目铁律是
**「不在本地起后端服务手动测，验证一律靠 pytest」**（`feedback_no_local_server_run`）。
解析器是本功能唯一有真实算法复杂度的部分（6 种格式、iOS 15+ `.ips` 是 JSON、
arm64e PAC 掩码、Apple `Binary Images:` 段解析），放前端等于放弃可测性。

放后端 → 纯函数 + 表驱动 pytest，且可被 crashguard pipeline 复用。
代价是一次 debounce 往返，对研发/QA 无感。

### 4.2 版本候选必须带 GitHub Release

只列已上传符号包（`GET /api/crash/symbols` 现成、零成本）看着省事，但
`api/crash.py` 注释写明「Jenkins 仅在 `IS_ONLINE_PACKAGE=true` 时上传」——
**QA 手上的灰度包大概率没上传过**，下拉会经常是空的，功能等于没做。

两个硬约束塑造了这块设计：

- **约束 A：候选必须跨两个仓合并。** `config.yaml` 的 `repo_routing` 以 4.0.0 为界：
  `< 4.0.0` → `Plaud-AI/Plaud-App`（Flutter），`≥ 4.0.0` → `Plaud-AI/plaud-native-app`（native）。
  「查哪个仓」取决于版本号，而版本号正是要列的东西 → 只能两仓都列、合并后标 `family`。
- **约束 B：Release tag 里的 `+NNN` 不是真实 build 号。** 真实 build 号要从 dSYM 的
  `Info.plist` 或 APK 的 `assets/datadog.buildId` 读。`github_symbols.py` 的
  `_resolve_release_build()` / `_read_dsym_build_via_range()`（HTTP Range 读远端 zip）就是
  干这个的，结果缓存在 `_build_index`。**下拉不能把 tag 当版本号直接显示**，否则用户选了
  个假 build 号，符号化静默失败——正是本功能要消除的坑。

处理：候选项带 `verified: bool`。`_build_index` 已解析的 → `verified=true`，显示真实 build 号；
未解析的 → `verified=false`，显示 tag 并明确标注「tag 未校验」。不静默假装。

### 4.3 同步 + 长 timeout，不建异步任务

`github_cache` 有磁盘缓存（`/data/symbols/github_cache/{app_version}/`，按 mtime 淘汰），
**同版本第二次就是秒级**，只有首次未命中才下 ~90MB。`api.ts` 已有 330s 同步先例
（`api.ts:1211` VoC digest）。项目后端也没有任何 SSE/StreamingResponse 基础设施。

建异步任务表 + 轮询，是为一个已被磁盘缓存 + 预热（§5.8）双重消化掉的边缘情况
付永久复杂度成本。→ 同步 + `timeoutMs: 300_000` + 前端进度提示。

## 5. 组件设计

> **API 前缀**：crashguard router 是 `APIRouter(prefix="/api/crash")`
> （`api/crash.py:45`，见 `backend/CLAUDE.md` 前缀总表）。前端 `api.ts` 的
> `BASE = "/api"`，所以前端调用写 `/crash/symbolicate/...`。
> 本文档统一用完整路径 `/api/crash/...` 表述。


### 5.1 `services/stack_inspector.py`（新增）

**纯函数，零 IO、零网络、零 DB。** 这是可测性的前提。

```python
def inspect_stack(raw: str) -> StackInsight
```

`StackInsight`（dataclass）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `stack_format` | str | `apple_ips_json` / `apple_crash_text` / `datadog_rum` / `android_java` / `android_tombstone` / `android_logcat` / `unknown` |
| `platform` | str | `ios` / `android` / `""`（无法判定） |
| `app_version` | str | 解析到的版本号，含 build 号（如 `4.0.201-941`）；解析不到为 `""` |
| `uuids` | list[str] | iOS 镜像 UUID（规范化：去 `-`、小写） |
| `build_ids` | list[str] | Android native BuildId |
| `binary_images` | list[dict] | 归一成 `symbolicate_stack()` 期望的形状 |
| `normalized_stack` | str | `.ips` JSON → 文本帧格式；其他格式原样返回 |
| `frame_count` | int | 识别到的帧数 |
| `confidence` | str | `high`（格式明确且含版本）/ `medium`（格式明确无版本）/ `low`（靠猜） |
| `notes` | list[str] | 人类可读说明，前端直接展示 |

**格式识别顺序**（先严格后宽松，避免误判）：

1. `raw.lstrip()` 以 `{` 开头且 JSON 可解析且含 `app_version`/`usedImages` → `apple_ips_json`
2. 含 `Incident Identifier:` 或 `^Binary Images:$` → `apple_crash_text`
3. 含 `#\d\d pc ` 或 `backtrace:` → `android_tombstone`
4. 含 `^\s*\d+\s+\S+\s+0x[0-9a-f]+\s+0x[0-9a-f]+\s+\+\s+\d+` → `datadog_rum`（iOS 帧形状）
5. 含 `\tat \S+\(` 或 `^\s*at \S+\(` → `android_java`
6. 含 `E AndroidRuntime:` 或 `Build fingerprint:` → `android_logcat`
7. 否则 `unknown`，`platform=""`，要求用户手选

**关键实现点**：

- `apple_ips_json`：Apple 的 `.ips` 是**两段式** —— 第一行是 header JSON，之后是 payload JSON。
  必须按第一个 `\n` 切开分别 parse。版本号从 header 的 `app_version` + `build_version`
  组合成 `{app_version}-{build_version}`（对齐 Datadog `@application.version` 的写法）。
- 归一化到 `datadog_rum` 的帧文本格式（`<idx> <module> <addr> <base> + <off>`），
  因为 `_symbolicate_ios_with_dir` 的正则就认这个形状。
- `binary_images` 每项形如 `{"uuid":..., "name":..., "load_address":..., "max_address":...}`，
  与 `symbolication.py` 现有消费方式一致。
- **复用 `_strip_ptr_auth` 的 PAC 掩码语义**：解析出的地址若高位被 arm64e 指针认证污染，
  沿用 `_PTR_AUTH_MASK = 0xFFFFFFFFFF`。不要重新发明。
- Android Java 栈：`platform="android"`、`app_version=""`、`confidence="medium"`，
  `notes` 明确写「ProGuard 混淆栈不含版本信息，必须手动指定版本号」。

### 5.2 `POST /api/crash/symbolicate/inspect`

请求：`{stack: str}`（复用现有 200_000 字符上限）

响应：`StackInsight` 全部字段 + 路由预览：

```
routing: { symbol_profile, github_repo, family, confidence }
```

路由预览调 `repo_router.resolve(platform, app_version, get_repo_routing(), path_exists=lambda _p: True)`。
`path_exists` 这个 lambda **不是可选的**——现有 `/symbolicate` 端点的注释解释了原因：
`resolve()` 默认用 `os.path.exists` 校验源码 wrapper 目录，但 `config.yaml` 里配的是裸机路径，
容器内不存在会返回 `None`，进而 `symbol_profile` 丢失、iOS 会去下错误的 dSYM 资产。
符号化本身不需要源码目录，跳过存在性校验是正确且零副作用的。

`app_version` 为空时跳过路由预览（返回 `routing: null`），不报错。

### 5.3 `GET /api/crash/symbolicate/versions?platform=ios|android`

合并三个来源，按版本号倒序：

| source | 取法 | 成本 |
|---|---|---|
| `uploaded` | `CrashSymbolPackage` 表按 platform 查 | DB，毫秒 |
| `cached` | `github_cache/` 目录 listdir + 判断是否含符号文件 | 本地 stat，毫秒 |
| `release` | 两仓 `GET /repos/{repo}/releases` + `_build_index` 查真实 build 号 | GH API ×2，秒级 |

每项：`{app_version, source, family, verified, symbol_types: [...], tag}`

- 同一个 `app_version` 多来源命中 → 合并成一条，`source` 取最优（`cached` > `uploaded` > `release`）
- `release` 源里 `_build_index` 未解析的 → `verified=false`、`app_version` 填 tag、
  前端显示「⚠ tag 未校验」
- **`verified=false` 项被选中时的行为**（实施时容易踩）：该值会以 tag 形态流入
  `app_version`。现有 `github_symbols.find_release_tag()` 同时支持「按 build 号反查 tag」
  （`_find_release_tag_by_build`）和「tag 直接匹配」两条路径，所以 tag 形态仍可能命中。
  但**不保证**——preflight 对 `verified=false` 的输入必须照常跑三档判定，
  判为 `missing` 就老实报 `missing`，不因为"它来自 Release 列表"就假定可用。
- **进程内 5 分钟 TTL 缓存**（GH API 有 rate limit，且下拉可能被反复打开）。
  缓存 key 为 `platform`。实现用模块级 dict + 时间戳，不引依赖。
- GH 调用失败时**降级而非报错**：只返回 `uploaded` + `cached`，响应带
  `warnings: ["GitHub Release 列表拉取失败，仅显示本地已有版本"]`

### 5.4 `POST /api/crash/symbolicate/preflight`

这是本次改进的核心。请求：`{platform, app_version, symbol_profile?, github_repo?}`

响应：

```
status: "cached" | "available" | "missing"
eta_hint: str            # 人话预估
symbol_sources: [...]    # 命中的来源明细
suggestions: {...}       # 仅 missing 时
warnings: [...]
```

三档判定（**全部走本地 stat + 已缓存索引 + 最多一次 GH API，不下载任何字节**）：

| status | 判定 | `eta_hint` |
|---|---|---|
| `cached` | `github_cache/{app_version}/` 已存在符号文件，或 `CrashSymbolPackage` 有该版本上传包 | 「符号包已就绪，预计秒级返回」 |
| `available` | 上述都没有，但目标仓该版本的 Release 存在对应符号 asset | 「需下载约 {size}MB，预计 1–3 分钟」 |
| `missing` | 三处都没有 | 「**该版本无符号表，符号化不会有任何效果**」 |

`missing` 时 `suggestions` 给三条可执行出路：

1. `nearby_versions`: 该 platform 最近有符号包的版本（最多 5 个）。
   **QA 记错 build 号是高频事件，这一条能直接救场。**
2. `reason`: 若该版本有 Release 但无符号 asset →
   「Jenkins 仅在 `IS_ONLINE_PACKAGE=true` 时上传符号包，该包可能不是线上包」
3. `upload_hint`: 指向本页的符号包上传框（见 §5.7）

asset 体积从 GH Release API 的 `assets[].size` 读，无需下载。

### 5.5 改造 `POST /api/crash/symbolicate`

**保持向后兼容**（现有 curl 调用方不能坏）：不删除任何已有字段，不改变
`symbolicated_stack` / `changed` / `stack_quality_*` / `routing_confidence` 的语义。
唯一例外是 `warnings` —— 它是**人读的诊断文本**，不是机器契约，措辞会按下文改进。

新增 `frame_stats`：

```
{
  total_frames: int,
  app_frames: int,          # module 命中某个 dSYM/so 的帧
  app_symbolicated: int,
  system_frames: int,       # module 不属于任何符号包的帧（按设计跳过）
  unparsed_lines: int
}
```

前端据此渲染「总 42 帧 · App 帧 18（符号化 17 / 失败 1）· 系统库帧 24（按设计跳过）」。
不做这个区分，诊断面板基本是废的。

实现：`symbolicate_stack()` 不改签名（它被 pipeline 多处调用）。在 ad-hoc 端点内
对 before/after 两段文本各跑一次**只读的帧分类**（复用 `stack_inspector` 的帧正则 +
`_symbolicate_ios_with_dir` 的 `dwarf_by_module` 判定思路），逐行比对得出统计。
即：统计逻辑在端点层，不侵入符号化引擎。

**修掉误导性 warning**：现在无条件说「无已上传符号包」。改成基于 preflight 三档的准确措辞：
`cached`/`available` 命中时不再输出这条；仅 `missing` 时输出，且措辞包含 Release 也没有的事实。

### 5.6 配置：两个 `keep_versions` 10 → 20

`backend/app/crashguard/config.py:517-519`：

```python
symbol_upload_keep_versions: int = 20   # was 10
github_cache_keep_versions: int = 20    # was 10
```

| 配置 | 管什么 | 单版本量级 | 增量 |
|---|---|---|---|
| `github_cache_keep_versions` | GitHub Release 自动下载缓存，按 mtime 淘汰 | ~200MB | +~2GB |
| `symbol_upload_keep_versions` | 手动上传包，按 `platform+symbol_type` 各留 N 个 | dSYM ~90MB / mapping / native_so | +~4.5GB |

合计增量约 **+6.5GB**（约 6.5GB → 13GB）。

**两个坑必须在部署时处理**：

1. **`config.local.yaml` 优先级高于代码默认值。** 这两项在 `/settings` 页面可在线改
   （范围 1–50），改动写入 `config.local.yaml`。若 102 上有人动过，改代码默认值会被
   静默覆盖。→ 部署后必须读一遍 102 的 `config.local.yaml` 确认没有残留的 10。
2. **磁盘余量未实测。** 本机 `data/symbols/` 几乎是空的（只有 `_build_index` + 两个 4K
   tag 缓存），真实占用在 102。写 spec 时 102 SSH 连不上（VPN 未连）。
   → 实施第一步必须先实测 102 磁盘余量，不足则不提或少提。

**一个诚实的边界**：`keep` 数只决定「已下过的包不被淘汰」，解决的是研发回查老版本时
不用重下。QA 手上的新灰度包是**从没下过**的，10→20 对它是 no-op。QA 场景靠 §5.8 预热。

### 5.7 前端 `/crashguard/symbolicate`

挂在 crashguard 下（不占 Sidebar 顶级位——它是崩溃排查的工具而非独立看板），
从 `/crashguard` 页面加入口链接。

交互链路：

1. 大 textarea 贴堆栈 → debounce 600ms 调 `inspect`
2. **解析结果条**：`识别为 iOS · Apple .ips · 版本 4.0.201-941（从堆栈解析）`
   —— 每个字段标来源，且**全部可改**。解析只预填不锁定。
3. 平台 radio + 版本 combobox（下拉候选来自 `versions`，**允许手填**，
   `verified=false` 项带 ⚠ 标注）
4. 平台/版本确定后自动调 `preflight` → 三档状态条：
   - 🟢 `cached`：「符号包已就绪，预计秒级返回」
   - 🟡 `available`：「需下载约 90MB，预计 1–3 分钟」
   - 🔴 `missing`：红字「该版本无符号表，符号化不会有任何效果」+ 提交按钮改为**二次确认**
     + 展示 `suggestions`（邻近版本可点击直接切换）
5. 提交 → `timeoutMs: 300_000` + 进度提示（`available` 档额外提示"首次下载符号包，请勿关闭页面"）
6. **结果区**：原始/符号化左右对比（等宽字体，行号对齐）+ 诊断面板：
   - `frame_stats` 渲染成人话
   - `stack_quality_before → after`
   - `routing_confidence` + 实际使用的 `symbol_profile` / `github_repo`
   - `warnings` 列表
7. **符号包上传框**：后端已有 `POST /api/crash/symbols/upload`，但前端零入口。
   在本页底部补一个（`missing` 档时从 `suggestions.upload_hint` 高亮指向它）。

i18n：新增中文 key 走 `frontend/src/lib/i18n.ts` 现有机制。

### 5.8 符号包预热（按 graygate 主要版本）

**目标**：让 QA 查刚发的灰度包时永远命中 `cached`，而不是等 1–3 分钟下载。

**依赖方向：crashguard → graygate（单向，graygate 零改动）。**
graygate 是独立子模块，不应该知道 crashguard 存在。crashguard 主动读
`app.graygate.services.focus_version.get_all_focus_versions()`
→ `{"ios": "4.0.201-941", "android": "4.0.200-938"}`。

> 注：根 `CLAUDE.md` 提到 `cd backend && lint-imports`，但 `import-linter` 实际**未安装**
> （`requirements.txt` 无此项、`rules/custom/` 为空，仅剩 `.import_linter_cache` 历史残留），
> 所以没有工具强制约束。仍按单向依赖设计，保持模块边界清晰。

**挂载点：crashguard job queue，不是主 tick。**
`workers/scheduler.py` 的 `_tick_once()` 是 60s 单线程顺序 await（见该文件 line 161 注释：
一个长任务会拖垮整个 loop）。预热要下 90MB，**必须**走 `_enqueue_job(job_name, coro_factory)`。

新增配置（`crashguard/config.py`）：

```python
symbol_prewarm_enabled: bool = True
symbol_prewarm_cron: str = "*/30 * * * *"   # 每 30 分钟检查一次
```

新增 `services/symbol_prewarmer.py`：

```python
async def prewarm_focus_versions() -> dict
```

流程：

1. 读 `get_all_focus_versions()`
2. 对每个非空 `(platform, version)`：
   a. 跑 §5.4 的 preflight 判定
   b. `cached` → skip（**幂等的关键**，不重复下载）
   c. `available` → 调对应的 `github_symbols` getter 触发下载
      （iOS: `get_ios_dsyms_dir`；Android: `get_android_native_symbols_dir` +
      `get_android_mapping` + `get_dart_symbols_dir`，按 `symbol_profile` 的
      `_profile_strategy` 决定调哪些，不要盲调）
   d. `missing` → 记日志，不告警（灰度包未上传符号包是常态，告警会变噪音）
3. 返回 `{prewarmed: [...], skipped: [...], missing: [...], duration_ms}`

手动触发端点：`POST /api/crash/symbols/prewarm`（复用现有 `/api/crash/prewarm-distributions` 的形状），
便于发版后立即预热而不等 cron。

**预热与 `keep_versions` 的相互作用**：预热会主动占满 `github_cache`。
`keep=20` 时最坏情况约 4GB。`_cleanup_github_cache` 按 mtime 淘汰，预热刚下的是最新的，
不会被自己挤掉。但**预热会加速老版本淘汰** —— 这正是 10→20 的实际价值所在
（预热占掉的名额需要更大的 keep 才不挤掉研发在查的老版本）。两个改动是互补的。

## 6. 测试策略

铁律：**不在本地起后端服务手动测，一律 pytest**（`feedback_no_local_server_run`）。
新增测试全部放 `backend/tests/crashguard/`。

| 测试文件 | 覆盖 |
|---|---|
| `test_stack_inspector.py` | **表驱动，最重要的一个。** 6 种格式各准备真实样本（含一份 iOS 15+ `.ips` 两段式 JSON、一份 arm64e PAC 污染地址的 jank 栈、一份纯 ProGuard 混淆栈）。断言 `platform`/`app_version`/`uuids`/`build_ids`/`confidence`/`normalized_stack`。含 `unknown` 兜底用例 |
| `test_symbolicate_preflight.py` | 三档判定。mock 磁盘 + `CrashSymbolPackage` + GH API，断 `status`/`eta_hint`/`suggestions.nearby_versions` |
| `test_symbolicate_versions.py` | 三源合并去重、`verified` 标记、`cached>uploaded>release` 优先级、GH 失败时降级、5min TTL 缓存命中 |
| `test_symbolicate_frame_stats.py` | 逐帧统计。构造「App 帧全解 + 系统库帧全跳过」的样本，断言 `system_frames` 不计入失败 |
| `test_symbol_prewarmer.py` | `cached` 跳过的幂等性、按 `symbol_profile` 只调该调的 getter、`missing` 不抛异常 |

现有测试不能坏：`test_symbolicate_api.py`（向后兼容）、`test_symbol_profile.py`、
`test_symbolicate_ios_module_gating.py`（§5.5 的帧分类复用了它的 gating 思路）。

前端无自动化测试基础设施，靠 `npm run build` 过类型检查。

## 7. 部署

按铁律：**不擅自部署/重启/push**（`feedback_no_auto_deploy`），
**高峰期禁止部署**（`feedback_peak_hours_no_deploy`），
**禁止向生产 DB 写测试数据**（`feedback_no_prod_test_writes`）。

部署前置（必须按序）：

1. **实测 102 磁盘余量**（`df -h /` + `du -sh data/symbols/*`）。不足 15GB 余量则
   `keep_versions` 只提到 15 或不提，并向用户报告。
2. **读 102 `config.local.yaml`**，确认 `symbol_upload_keep_versions` /
   `github_cache_keep_versions` 没有残留的 10 覆盖掉新默认值。
3. 部署用 `./deploy-all.sh`（`feedback_deploy_script`），不手动 SSH + docker compose。
4. 部署后验证**只做只读**：调 `inspect` / `versions` / `preflight` 三个只读端点，
   不触发真实符号化下载（那会占磁盘）。

## 8. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 102 磁盘不足，`keep=20` 塞满盘 | 生产事故 | 部署前置 §7.1 实测；有 `mac-disk-cleanup` skill 兜底 |
| `config.local.yaml` 残留值让配置改动静默失效 | 改了没生效，误判 | 部署前置 §7.2 明确核查 |
| GH API rate limit（预热 + 下拉 + preflight 三处调） | 列表拉不到 | 5min TTL 缓存 + `_build_index` 复用 + 失败降级不报错 |
| 预热经 VPN 下载 90MB 卡住 | job queue 堵塞 | 已知问题（`project_vpn_bulk_transfer_stall_102`：>5MB 经 VPN 会卡）。预热走 job queue 不阻塞主 tick；getter 自带超时 |
| `.ips` 格式随 iOS 版本变化 | 解析失效 | `stack_inspector` 解析失败**降级为 `unknown` 而非抛异常**，用户仍可手选平台/版本走通 |
| 错误符号化（module 不匹配却强凑符号） | 比不符号化更具误导性 | 不改动 `_symbolicate_ios_with_dir` 的 module gating；`frame_stats` 把 `system_frames` 单列，让用户能判断可信度 |
