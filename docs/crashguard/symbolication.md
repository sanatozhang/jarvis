# Crashguard 符号化配置指南

本文档说明如何为 Crashguard 配置崩溃堆栈符号化，供 Jenkins CI/CD 集成参考。

## 背景

Datadog Events API 返回的 iOS/Android 崩溃堆栈是原始二进制地址（未符号化），AI 无法从中分析根因。符号化分两个阶段：

| 阶段 | 内容 | 触发方式 |
|------|------|---------|
| **Plan A（自动）** | Flutter engine 帧（`libflutter.so` / `Flutter.framework`） | Pipeline 运行时自动从 Flutter 公开存储下载，无需配置 |
| **Plan B（手动上传）** | App 本体帧 + Dart 混淆代码 | 每次发版后由 Jenkins 调用上传 API |

---

## Plan A：Flutter Engine 自动符号化

**无需任何配置**，Pipeline 自动处理：

1. 从 Datadog 事件的 `binary_images` 字段提取 Flutter engine UUID（iOS）或 BuildId（Android）
2. 从 Flutter 公开存储下载对应版本的 debug symbols
3. 用 `atos`（iOS）或 `addr2line`/`llvm-symbolizer`（Android）将地址替换为函数名
4. 符号化结果缓存到容器内 `/data/symbols/flutter_engine_cache/`，不重复下载

**覆盖范围**：`FlutterPlatformPlugin`、`FlutterEngine`、Flutter framework 内所有帧。

---

## Plan B：App 符号包上传

### 支持的符号包类型

| `symbol_type` 值 | 文件内容 | 适用平台 |
|-----------------|---------|---------|
| `dsym` | Xcode Archive 产物（`App.dSYM.zip`） | iOS |
| `dart_symbols` | Flutter `--split-debug-info` 产物（zip） | iOS & Android |
| `proguard_mapping` | ProGuard/R8 混淆映射（`mapping.txt.zip`） | Android |

### API 接口

**上传符号包**
```
POST http://<server>:8000/api/crash/symbols/upload?platform=<platform>&app_version=<app_version>&symbol_type=<symbol_type>
Content-Type: multipart/form-data
```

⚠️ `platform` / `app_version` / `symbol_type` 是 **query 参数**（handler 签名里没有 `Form()` 标注），不是 multipart 字段；只有 `file` 走 `-F`/`multipart/form-data`。全部塞进 `-F` 会因为 FastAPI 拿不到必填的 query 参数而返回 422。

| 参数 | 位置 | 类型 | 说明 |
|------|------|------|------|
| `platform` | query | string | `ios` / `android` / `flutter` |
| `app_version` | query | string | 与 Datadog `@application.version` 一致，如 `3.18.0-708` |
| `symbol_type` | query | string | 见上表 |
| `file` | multipart | file | zip 格式的符号包文件 |

**响应示例**
```json
{
  "id": "a1b2c3d4-...",
  "platform": "ios",
  "app_version": "3.18.0-708",
  "symbol_type": "dsym",
  "size_bytes": 12345678,
  "created_at": "2026-05-18T10:00:00"
}
```

**查询已上传列表**
```
GET http://<server>:8000/api/crash/symbols?platform=ios&app_version=3.18.0-708
```

**删除**
```
DELETE http://<server>:8000/api/crash/symbols/{id}
```

---

## 手工符号化任意堆栈

`POST /api/crash/symbolicate` —— 拿到一段原始堆栈文本（比如用户反馈里贴的、或者从
Datadog 手动复制出来的），不经过完整的 issue/analysis 流程，直接同步跑一遍符号化，
方便排查一个具体崩溃时快速验证符号包是否生效。

**同步返回，无 `task_id`**：首次遇到某个 `(platform, app_version)` 组合时会现场下载
符号包（iOS dSYM 可达 90MB），可能耗时数十秒到数分钟，**务必加 `curl --max-time 300`**，
否则客户端会先超时断开（服务端仍会继续跑完并缓存，下次调用会很快）。

只读：不写任何 `crash_*` 表，纯粹是"符号化 + 查一下已上传符号包清单"的组合查询。

### 请求

```
POST http://<server>:8000/api/crash/symbolicate
Content-Type: application/json
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `stack` | string | ✅ | 原始堆栈文本。空 → 400；超过 200000 字符 → 413 |
| `platform` | string | ✅ | `ios` / `android` / `flutter`，其余值 → 400（本接口不做猜测，平台判断放在调用方） |
| `app_version` | string | ❌ | 如 `4.0.201-941`。缺失时 Plan B/C（用户上传符号包 + GitHub release 符号）全部失效，仅 Plan A（Flutter engine）生效，会体现在 `warnings` 里 |
| `binary_images` | array | ❌ | 默认 `[]`；来自 Datadog RUM `@error.binary_images`，手工调用通常留空即可 |
| `symbol_profile` | string | ❌ | 覆盖自动路由结果，可选 `flutter_ios` / `native_ios` / `flutter_android` / `native_android` / `none` |
| `github_repo` | string | ❌ | 覆盖自动路由解析出的源码/符号仓 |

### 响应

```json
{
  "symbolicated_stack": "...",
  "changed": true,
  "stack_quality_before": "raw",
  "stack_quality_after": "symbolicated_native",
  "platform": "ios",
  "app_version": "4.0.201-941",
  "symbol_profile": "native_ios",
  "github_repo": "Plaud-AI/plaud-native-app",
  "routing_confidence": "high",
  "available_symbol_packages": [
    {"symbol_type": "dsym", "app_version": "4.0.201-941", "file_name": "Plaud-Global.dSYMs.zip", "created_at": "2026-08-03T10:00:00"}
  ],
  "duration_ms": 8421,
  "warnings": [],
  "frame_stats": {
    "total_frames": 42,
    "symbolicated": 17,
    "unresolved": 25,
    "unparsed_lines": 3,
    "app_module": "PLAUD",
    "app_frames": 18,
    "app_symbolicated": 17,
    "non_app_frames": 24
  },
  "preflight": {
    "status": "cached",
    "eta_hint": "符号包已就绪，预计秒级返回",
    "symbol_sources": [{"source": "cached", "detail": "github_cache 已有符号文件"}],
    "suggestions": {},
    "warnings": []
  }
}
```

`frame_stats`（2026-09-22 新增）分两层，**不要混用**：

- **粗粒度**（`total_frames` / `symbolicated` / `unresolved` / `unparsed_lines`）总是准确，
  纯文本逐行比对得出。
- **细分**（`app_frames` / `app_symbolicated` / `non_app_frames`）**只在能推断出
  `app_module` 时才有值，否则是 `null`**。端点层拿不到 dSYM 里到底有哪些 module
  （那在 `symbolication` 内部），`app_module` 靠 `_infer_app_module` 推断
  （优先 `.ips` 的 `source=="P"` 主可执行镜像，退而取堆栈里最高频的 module）。
  推断不到就诚实返回 `null`，不编造区分。消费方**不能把 `null` 当 0**，
  那会让人误以为 App 帧一个都没解出来。

为什么要这个细分：`_symbolicate_ios_with_dir` 是**故意**跳过系统库帧的（见该函数
docstring 记录的生产实测：不做 module gating 会给每一帧都凑出一个看似合理实则
完全无关的 Plaud 符号，比原样保留地址更具误导性）。所以一次**正常成功**的符号化里
也会有一半帧是裸地址。只看 `unresolved`，会把正常成功误判为失败。

`changed` 是符号化前后字符串直接比对的结果——`symbolicate_stack` 内部失败时会静默
原样返回输入串，所以只有这个字段能诚实反映符号化是否真的生效，`stack_quality_*`
只是辅助佐证（有可能碰巧文本里本来就带 `.swift:` 之类的字样）。

`warnings` 常见的三种情形（都不是接口 bug，只是"这次调用注定符号化不了"）：

- `app_version` 没传 → Plan B/C 无法命中，只有 Plan A 生效
- 内部按 `(platform, app_version)` 自动路由（`repo_router.resolve`）失败 → `routing_confidence` 会是 `"unresolved"`，说明用了兼容回退（`symbol_profile`/`github_repo` 会是空字符串，符号化基本不会生效）
- `preflight.status == "missing"` → 三处（本地缓存 / 已上传包 / 两仓 GitHub Release）都没有该版本的符号表，这次符号化注定无效

> **2026-09-22 变更**：此前这里无条件输出 `"该 (platform, app_version) 无已上传符号包"`。
> 那条 warning 只查 `CrashSymbolPackage`（本地上传表）、**完全不看 GitHub Release**，
> 所以 Plan C 下载成功、符号化完美时照样会吐，是纯噪音。现已改为基于
> `preflight` 三档判定的准确措辞。`available_symbol_packages` 字段本身保留不变
> （它如实反映"已上传包"，只是不该被当成"有没有符号表"的判据）。

### 示例

```bash
curl --max-time 300 -X POST http://10.0.52.102:8000/api/crash/symbolicate \
  -H "Content-Type: application/json" \
  -d '{
    "stack": "0  Plaud  0x0000000100123456 0x100000000 + 1193046\n1  Plaud  0x0000000100223456 0x100000000 + 2242134",
    "platform": "ios",
    "app_version": "4.0.201-941"
  }' | jq
```

### 认证

与 `/symbols/upload` 一致：`.env` 里 `ENABLE_SSO=false` 时无需任何 header。若目标服务器
开启了 SSO，全局 `AuthMiddleware` 会要求 `jarvis_session` cookie，未带 cookie 调用会收到
401——先登录网页版拿到 cookie，或找已登录会话代为调用。

---

## 符号化工作台（前端页面 + 三个探测端点）

2026-09-22 新增。页面在 `/crashguard/symbolicate`（挂在 crashguard 下，不占 Sidebar
顶级位）。给研发自查零散堆栈、QA 反馈灰度包崩溃用：粘贴堆栈 → 自动预填平台/版本
→ 预检符号可用性 → 符号化 + 诊断。

三个端点**全部只读，不下载任何符号字节**，所以秒级返回。

### `POST /api/crash/symbolicate/inspect`

裸堆栈文本 → 元数据（`body: {stack}`）。由纯函数模块
`services/stack_inspector.py` 实现（零 IO / 零网络 / 零 DB，可被 pipeline 复用）。

识别 6 种格式，**版本号可得性因格式而异**：

| 格式 | app 版本号 | UUID / BuildId |
|---|---|---|
| `apple_ips_json`（iOS 15+，两段式 JSON） | ✅ `app_version`-`build_version` | ✅ `usedImages[].uuid` |
| `apple_crash_text`（旧文本） | ✅ `Version: 4.0.201 (941)` | ✅ `Binary Images:` 段 |
| `datadog_rum`（从 Datadog 复制的帧） | ❌ | ❌ |
| `android_java`（ProGuard 混淆栈） | ❌ **物理不可能** | ❌ |
| `android_logcat` | ⚠️ 偶有 `versionName` | ❌ |
| `android_tombstone` | ❌ | ✅ `BuildId:` |

最高频的 Android 输入就是一段 `at a.b.c(Unknown Source:12)`，里面没有任何版本
信息——**所以「手动指定版本号」必须保留为主路径**，本端点只做预填不做保证。
识别不出时降级为 `stack_format: "unknown"` 且 `platform: ""`，**不报错**。

`apple_ips_json` 的 `normalized_stack` 会把结构化帧摊平成
`<idx> <module> <addr> <base> + <off>` 形状——`_symbolicate_ios_with_dir` 的正则只认
这个形状，不摊平的话 `.ips` 根本没法符号化。

### `GET /api/crash/symbolicate/versions?platform=ios|android`

版本候选（前端下拉用），合并三源并按版本倒序，`source` 取最优
（`cached` > `uploaded` > `release`）。进程内 5 分钟 TTL 缓存（GH API 有 rate limit）。

两个塑造这块设计的硬约束：

- **候选必须跨两个仓合并**：`repo_routing` 以 4.0.0 为界（`< 4.0.0` → `Plaud-AI/Plaud-App`，
  `>= 4.0.0` → `Plaud-AI/plaud-native-app`）。"查哪个仓"取决于版本号，而版本号正是
  要列的东西 → 只能两仓都列。
- **Release tag 里的 `+NNN` 不是真实 build 号**：真实值在 dSYM 的 `Info.plist` /
  APK 的 `assets/datadog.buildId`。本端点**只读 `_build_index` 缓存，绝不现场发起
  Range 请求**（30 release × 2 仓 = 60 次请求，太慢）。未解析的标 `verified: false`，
  前端显示「⚠ tag 未校验」——不能静默假装 tag 是真版本号，否则用户选了个假 build 号
  会静默符号化失败。

任一源失败都降级为 `warnings`，不让整个下拉不可用。

### `POST /api/crash/symbolicate/preflight`

**本次改进的核心**：把「没有符号表」从事后 warning 改成**提交前**的三档预检。

此前 `POST /symbolicate` 是"先干活再抱怨"——花几十秒到几分钟尝试下载 dSYM，
跑完符号化后才在 warnings 里说无符号包，用户白等。

| `status` | 判定 | `eta_hint` |
|---|---|---|
| `cached` | `github_cache/{version}/` 已有符号文件，或有该版本上传包 | 符号包已就绪，预计秒级返回 |
| `available` | 上述都没有，但目标仓该 Release 有符号 asset | 需下载约 N MB，预计 1–3 分钟 |
| `missing` | 三处都没有 | 该版本无符号表，符号化不会有任何效果 |

`missing` 时 `suggestions` 给三条出路：

- `nearby_versions`：最近有符号包的版本（同 minor 优先、build 号最接近）。
  **QA 记错 build 号是高频事件，这条能直接救场**，前端渲染成可点击按钮。
- `reason`：有 Release 但无符号 asset → 提示 `IS_ONLINE_PACKAGE`，大概率不是线上包
- `upload_hint`：指向页面底部的手动上传框

体积从 Release API 的 `assets[].size` 读，**不下载**。

## 符号包预热

`POST /api/crash/symbols/prewarm`（手动）+ cron `symbol_prewarm_cron`（默认 `*/30`，
`symbol_prewarm_enabled` 默认 `true`）。

按 **graygate 人工指定的「主要版本」**（`get_all_focus_versions()`）提前把符号包拉到
本地，让 QA 查新灰度包时永远命中 `cached`。

- **依赖方向 crashguard → graygate（单向）**：graygate 是独立子模块，不应知道
  crashguard 存在，所以由 crashguard 主动读，**graygate 零改动**。
- **必须走 job queue**：`_tick_once()` 是 60s 单线程顺序 await，下载 90MB 直接
  await 会拖垮整个 loop。cron 块用 `_enqueue_job("symbol_prewarm", ...)`。
- **幂等**：preflight 判 `cached` 就跳过，否则每 30 分钟重下 90MB。
- **按 `symbol_profile` 只调该调的 getter**：`native_android` 没有 Dart，
  `flutter_ios` 用 `PLAUD.dSYMs.zip` 而 `native_ios` 用 `Plaud-Global.dSYMs.zip`
  （2026-07-14 实测确认资产名不同，套错会下到错误的 dSYM）。
- `missing` 只记日志**不告警**——灰度包未上传符号包是常态，告警会变噪音。

### 与 `keep_versions` 的相互作用

预热会主动占满 `github_cache` 名额。这正是 2026-09-22 把
`symbol_upload_keep_versions` / `github_cache_keep_versions` 从 10 提到 20 的原因：
预热占掉的名额需要更大的 keep，才不会挤掉研发正在回查的老版本。两个改动是互补的。

磁盘影响：`github_cache` 单版本约 200MB（+2GB），上传包单版本 dSYM ~90MB /
mapping / native_so（+4.5GB），合计约 **+6.5GB**。

> **部署时必读**：这两项在 `/settings` 页面可在线改（范围 1–50），改动写入
> `config.local.yaml`，而**`config.local.yaml` 优先级高于代码默认值**。
> 若服务器上有人动过，改代码默认值会被静默覆盖——部署后必须
> `cat config.local.yaml` 确认没有残留的 10。

---

## Jenkins 配置示例

### iOS Pipeline（Fastlane + Jenkins）

在 `archive` 步骤之后，加入以下 shell 步骤：

```sh
#!/bin/bash
set -e

APP_VERSION="${FLUTTER_VERSION}-${BUILD_NUMBER}"   # 与 pubspec.yaml 版本一致
JARVIS_URL="http://10.0.52.102:8000"

# 1. 上传 dSYM（来自 Xcode Archive）
# 注意：platform/app_version/symbol_type 是 query 参数，不能塞进 -F，否则 422
DSYM_ZIP="build/ios/archive/Runner.xcarchive/dSYMs/Runner.app.dSYM"
if [ -d "$DSYM_ZIP" ]; then
  zip -r /tmp/Runner.dSYM.zip "$DSYM_ZIP"
  curl -f -X POST "$JARVIS_URL/api/crash/symbols/upload?platform=ios&app_version=$APP_VERSION&symbol_type=dsym" \
    -F "file=@/tmp/Runner.dSYM.zip"
fi

# 2. 上传 Dart symbols（需要 flutter build 时加 --split-debug-info=build/debug-info）
if [ -d "build/debug-info" ]; then
  zip -r /tmp/dart-symbols.zip build/debug-info/
  curl -f -X POST "$JARVIS_URL/api/crash/symbols/upload?platform=ios&app_version=$APP_VERSION&symbol_type=dart_symbols" \
    -F "file=@/tmp/dart-symbols.zip"
fi

echo "Symbol upload done for $APP_VERSION"
```

### Android Pipeline

```sh
#!/bin/bash
set -e

APP_VERSION="${FLUTTER_VERSION}-${BUILD_NUMBER}"
JARVIS_URL="http://10.0.52.102:8000"

# 1. 上传 Dart symbols（需要 flutter build apk --split-debug-info=build/debug-info）
# 注意：platform/app_version/symbol_type 是 query 参数，不能塞进 -F，否则 422
if [ -d "build/debug-info" ]; then
  zip -r /tmp/dart-symbols-android.zip build/debug-info/
  curl -f -X POST "$JARVIS_URL/api/crash/symbols/upload?platform=android&app_version=$APP_VERSION&symbol_type=dart_symbols" \
    -F "file=@/tmp/dart-symbols-android.zip"
fi

# 2. 上传 ProGuard mapping（如有）
MAPPING="android/app/build/outputs/mapping/release/mapping.txt"
if [ -f "$MAPPING" ]; then
  zip -r /tmp/mapping.zip "$MAPPING"
  curl -f -X POST "$JARVIS_URL/api/crash/symbols/upload?platform=android&app_version=$APP_VERSION&symbol_type=proguard_mapping" \
    -F "file=@/tmp/mapping.zip"
fi

echo "Symbol upload done for $APP_VERSION"
```

### Flutter build 必须加的编译参数

```sh
# iOS Release
flutter build ipa --release \
  --split-debug-info=build/debug-info \
  --obfuscate

# Android Release
flutter build apk --release \
  --split-debug-info=build/debug-info \
  --obfuscate
```

> ⚠️ `--obfuscate` 开启后必须同时加 `--split-debug-info`，否则 Dart 帧永远无法符号化。

---

## app_version 对齐说明

`app_version` 参数必须与 Datadog 里 `@application.version` 字段**完全一致**，否则 Pipeline 无法匹配到对应符号包。

查看当前 Datadog 版本格式：
```
GET http://10.0.52.102:8000/api/crash/latest-release
```

通常格式为 `{semver}-{build_number}`，例如 `3.18.0-708`。可在 `pubspec.yaml` 中配置：
```yaml
version: 3.18.0+708   # → Datadog 上报为 3.18.0-708
```

---

## 符号包存储位置

容器内路径：`/data/symbols/<platform>/<symbol_type>/<app_version>/`

| 类型 | 示例路径 |
|------|---------|
| iOS dSYM | `/data/symbols/ios/dsym/3.18.0-708/Runner.dSYM.zip` |
| Flutter dart symbols (iOS) | `/data/symbols/ios/dart_symbols/3.18.0-708/dart-symbols.zip` |
| Android dart symbols | `/data/symbols/android/dart_symbols/3.18.0-708/dart-symbols-android.zip` |
| Flutter engine cache (自动) | `/data/symbols/flutter_engine_cache/<uuid>/Flutter.dSYM.zip` |

宿主机对应挂载点：`./data/symbols/`（同 `./data/` volume）。

---

## 验证上传是否生效

```bash
# 查看已上传符号包
curl http://10.0.52.102:8000/api/crash/symbols

# 触发一次手动 pipeline 拉取新事件（symbols 在下次 pipeline 执行时生效）
curl -X POST http://10.0.52.102:8000/api/crash/trigger

# 查看某个 issue 的堆栈是否已符号化
curl http://10.0.52.102:8000/api/crash/issues/<issue_id> | jq '.representative_stack'
```
