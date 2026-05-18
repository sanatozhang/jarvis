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
POST http://<server>:8000/api/crash/symbols/upload
Content-Type: multipart/form-data
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `platform` | string | `ios` / `android` / `flutter` |
| `app_version` | string | 与 Datadog `@application.version` 一致，如 `3.18.0-708` |
| `symbol_type` | string | 见上表 |
| `file` | file | zip 格式的符号包文件 |

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

## Jenkins 配置示例

### iOS Pipeline（Fastlane + Jenkins）

在 `archive` 步骤之后，加入以下 shell 步骤：

```sh
#!/bin/bash
set -e

APP_VERSION="${FLUTTER_VERSION}-${BUILD_NUMBER}"   # 与 pubspec.yaml 版本一致
JARVIS_URL="http://10.0.52.102:8000"

# 1. 上传 dSYM（来自 Xcode Archive）
DSYM_ZIP="build/ios/archive/Runner.xcarchive/dSYMs/Runner.app.dSYM"
if [ -d "$DSYM_ZIP" ]; then
  zip -r /tmp/Runner.dSYM.zip "$DSYM_ZIP"
  curl -f -X POST "$JARVIS_URL/api/crash/symbols/upload" \
    -F "platform=ios" \
    -F "app_version=$APP_VERSION" \
    -F "symbol_type=dsym" \
    -F "file=@/tmp/Runner.dSYM.zip"
fi

# 2. 上传 Dart symbols（需要 flutter build 时加 --split-debug-info=build/debug-info）
if [ -d "build/debug-info" ]; then
  zip -r /tmp/dart-symbols.zip build/debug-info/
  curl -f -X POST "$JARVIS_URL/api/crash/symbols/upload" \
    -F "platform=ios" \
    -F "app_version=$APP_VERSION" \
    -F "symbol_type=dart_symbols" \
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
if [ -d "build/debug-info" ]; then
  zip -r /tmp/dart-symbols-android.zip build/debug-info/
  curl -f -X POST "$JARVIS_URL/api/crash/symbols/upload" \
    -F "platform=android" \
    -F "app_version=$APP_VERSION" \
    -F "symbol_type=dart_symbols" \
    -F "file=@/tmp/dart-symbols-android.zip"
fi

# 2. 上传 ProGuard mapping（如有）
MAPPING="android/app/build/outputs/mapping/release/mapping.txt"
if [ -f "$MAPPING" ]; then
  zip -r /tmp/mapping.zip "$MAPPING"
  curl -f -X POST "$JARVIS_URL/api/crash/symbols/upload" \
    -F "platform=android" \
    -F "app_version=$APP_VERSION" \
    -F "symbol_type=proguard_mapping" \
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
