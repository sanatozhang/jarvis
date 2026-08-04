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
  "warnings": []
}
```

`changed` 是符号化前后字符串直接比对的结果——`symbolicate_stack` 内部失败时会静默
原样返回输入串，所以只有这个字段能诚实反映符号化是否真的生效，`stack_quality_*`
只是辅助佐证（有可能碰巧文本里本来就带 `.swift:` 之类的字样）。

`warnings` 常见的三种情形（都不是接口 bug，只是"这次调用注定符号化不了"）：

- `app_version` 没传 → Plan B/C 无法命中，只有 Plan A 生效
- 内部按 `(platform, app_version)` 自动路由（`repo_router.resolve`）失败 → `routing_confidence` 会是 `"unresolved"`，说明用了兼容回退（`symbol_profile`/`github_repo` 会是空字符串，符号化基本不会生效）
- `available_symbol_packages` 为空 → 该版本没有人工上传过符号包，可能压根不是线上包（Jenkins 只在 `IS_ONLINE_PACKAGE=true` 时才会调 `/symbols/upload`）

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
