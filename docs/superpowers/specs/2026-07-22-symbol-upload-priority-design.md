# 打包机上传符号表 + 符号化优先级改造

日期: 2026-07-22
状态: 待用户审阅

## 背景

排查 crashguard 两条 iOS native 卡顿/ANR issue 时发现:

1. `_symbolicate_ios_with_dir()` 之前对每一帧都无条件套用下载到的 App dSYM,不检查该帧
   module 是否真的属于这个 dSYM——系统库帧被错误套用 App 自己的符号,产出看似合理实则
   完全无关的堆栈(已修复,commit `c179490`,与本设计无关但是触发本次调查的起点)。
2. 修复后又在实测中发现:真正阻塞符号化的常见原因是 **GitHub Release dSYM 下载经 VPN
   链路不稳定**——同一个 91MB 的 `Plaud-Global.dSYMs.zip`,两次下载分别卡在 3MB 和
   90.1MB,断点位置随机,是链路问题不是代码 bug(`project_vpn_bulk_transfer_stall_102`
   已有记录)。
3. 调查中发现 `POST /api/crash/symbols/upload`(+ `GET /symbols` + `DELETE /symbols/{id}`
   + `GET/PATCH /settings/symbols`)**已经存在且已部署**,代码注释里甚至写着"Jenkins 可
   通过 `?keep_versions=N` 覆盖",但生产环境 `GET /api/crash/symbols` 返回空——从未被
   实际调用过。当前的符号化查找路径(Plan B 用户上传)也只在给 **Flutter engine 自身**
   的 dSYM(按 UUID 匹配)时才会用到,对 native app 自己的模块完全不生效,始终依赖那条
   会被 VPN 卡住的 GitHub 下载路径。

## 目标

让打包机(plaud-native-app2 的 Jenkins 脚本)在打包完成后,把 iOS dSYM / Android
ProGuard mapping 直接上传到 jarvis(同机内网,不经 GitHub/VPN),符号化时优先使用这些
已上传的包,查不到精确版本才回退现有 GitHub 下载逻辑。同时管住符号包数量(沿用现有
`symbol_upload_keep_versions` 配置和设置页,不新增)。

## 范围

**这次做:**
- jarvis 后端:`get_ios_dsyms_dir()` / `get_android_mapping()` / `get_dart_symbols_dir()` /
  `get_android_native_symbols_dir()` 四个 getter 改为"先查本地已上传包(精确 app_version
  匹配),查不到再回退 GitHub"。iOS 和 Android 完整对等接入,不留平台差异。
- upload API 新增 `symbol_type=native_symbols`(Android 带 debug 符号的 `.so`,tar.gz 格式),
  和已有的 dsym/dart_symbols/proguard_mapping 并列。
- 上传时校验文件是合法压缩包(`symbol_type` 需要解压的类型:dsym/dart_symbols/
  native_symbols),拒绝损坏文件。
- 解压已上传包时按 (platform, symbol_type, app_version) 加锁,防止并发解压写坏文件。
- jank issue 回填:定期扫一遍 `fixable=True` 但堆栈仍是占位符的 jank issue,重新尝试符号化。
- plaud-native-app2:新建分支,在 `jenkins/plaud-native-app-publish-global.sh` 里新增
  `upload_jarvis_symbols()`,和现有 `upload_sentry_symbols`/`upload_datadog_symbols` 并列
  调用,上传 iOS dSYMs zip + Android mapping.txt + Android native_symbols tar.gz(该目录
  `archive_android_native_symbols()` 已经产出,直接复用,不需要新增打包逻辑)。

**这次不做(已跟用户确认,明确排除):**
- 不接入 CN flavor(`plaud-native-app-publish-cn.sh` 不改)。原因:CN/Global 通常共用同一
  个版本号,而上传/查找 key 只有 `(platform, app_version)`,不分 flavor——两边都接会导致
  CN 包和 Global 包互相覆盖/串用,复现"错误 dSYM 硬套"同类 bug。现有 GitHub Plan C 本来就
  写死"只处理 global flavor",这次维持同样的限定,零新增风险。CN native 崩溃的符号化留作
  后续单独调查(需要先确认崩溃数据能否识别 flavor)。
- 不做"强制重置已错误符号化的历史 crash/ANR issue"的入口。原因:crash/ANR 的
  `representative_stack` 一旦被(错误)符号化覆写,原始地址信息就从 DB 里消失了,现有
  4 小时一轮的 pipeline 重跑只能在"仍是原始地址"的帧上生效,救不回已被覆写的帧。这类
  历史坏数据(比如本次触发调查的那条 AppHang issue)留作已知遗留问题,后续单独处理。
- 不加上传接口鉴权——沿用 jarvis 现有"内网工具、无逐接口鉴权"的整体风格。

## 架构

```
打包机 (plaud-native-app2, jenkins/plaud-native-app-publish-global.sh)
  └─ 打包产出 DSYM_ROOT_DIR / ANDROID_MAPPING_PATH 后
       upload_jarvis_symbols()
         ├─ app_version = ${staging_version_tag#v} 里的 "+" 换成 "-"
         │    (v4.0.201+941 → 4.0.201-941，对齐 Datadog @application.version 格式)
         ├─ iOS: zip 打包 dSYMs 目录 → curl -F file=@xxx.zip
         │    POST http://localhost:8000/api/crash/symbols/upload
         │         ?platform=ios&symbol_type=dsym&app_version=4.0.201-941
         ├─ Android mapping: curl -F file=@mapping.txt
         │    POST .../symbols/upload?platform=android&symbol_type=proguard_mapping&app_version=...
         └─ Android native .so: tar.gz 打包 $global_native_symbols_dir(archive_android_native_symbols
              已产出的目录) → curl -F file=@native_symbols.tar.gz
              POST .../symbols/upload?platform=android&symbol_type=native_symbols&app_version=...
              (三条都失败只打日志，不中断构建，和 Sentry/Datadog 两个上传函数同风格)

jarvis 后端 (backend/app/crashguard/)
  ├─ /api/crash/symbols/upload  (已存在，这次加：zip 完整性校验)
  │    └─ 落盘 /data/symbols/<platform>/<symbol_type>/<app_version>/<原始文件名>
  │       + CrashSymbolPackage 记录 + 按 keep_versions 清理旧版本 (已存在，不用改)
  ├─ /api/crash/settings/symbols + 设置页 keep_versions 配置 (已存在，不用改)
  └─ services/github_symbols.py
       get_ios_dsyms_dir() / get_android_mapping() / get_dart_symbols_dir()
         1. 查 /data/symbols/<platform>/<symbol_type>/<app_version>/ 精确匹配
            - 有 → iOS/dart_symbols: 首次解压(带锁 + .extracted marker)，Android mapping: 直接返回文件路径
            - 没有 → 继续走原有 GitHub 下载逻辑(含它自己的版本 fallback)，不变
       ↓ 供 symbolication.py 的 4 个现有调用点自动受益，无需改调用方
```

## 数据流细节

### 上传侧 (packaging script → API)

- 触发点：`jenkins/plaud-native-app-publish-global.sh` 里紧挨着现有
  `upload_sentry_symbols "$DSYM_ROOT_DIR" "$ANDROID_MAPPING_PATH"` /
  `upload_datadog_symbols ...` 那两行(约 2514-2518 行)。
- `app_version` 计算：脚本里已有 `staging_version_tag`（形如 `v4.0.201+941`，来自
  `read_staging_version_tag()`）。转换：去掉前缀 `v`，把 `+` 替换成 `-` → `4.0.201-941`，
  与 crash 数据里 `@application.version` / `CrashIssue.last_seen_version` 的格式完全对齐
  （这是能否被正确查到的关键，务必保证两边格式一致）。
- 目标地址：`http://localhost:8000`（Jenkins 和 jarvis backend 部署在同一台服务器
  10.0.52.102，同机直连，不经 VPN/公网），可用环境变量覆盖以应对未来 Jenkins 迁移。
- 失败处理：curl 失败/非 2xx 只打日志，不 `set -e` 中断整个构建 pipeline（与
  `upload_sentry_symbols`/`upload_datadog_symbols` 现有的"失败不中断"风格一致）。

### 读取侧 (symbolication.py 的 4 个调用点，无需改动)

`get_ios_dsyms_dir()` / `get_android_mapping()` / `get_dart_symbols_dir()` 内部改造：

```
def get_ios_dsyms_dir(app_version, repo, asset_name):
    uploaded = _find_uploaded_ios_dsyms_dir(app_version)  # 新增
    if uploaded:
        return uploaded
    # ... 原有 GitHub 下载逻辑，原样保留
```

`_find_uploaded_ios_dsyms_dir(app_version)`：
- 查 `/data/symbols/ios/dsym/<app_version>/` 目录是否存在且非空（精确字符串匹配，
  不做模糊/最近版本回退——避免重蹈"错误 dSYM 硬套"的覆辙）。
- 目录下如果是原始 zip（上传时未解压，保留原始文件用于审计/下载），首次使用时解压到
  `<该目录>/.extracted/`，用 marker 文件标记，之后直接返回缓存目录，不重复解压。
- 解压过程按 `(platform, symbol_type, app_version)` 加锁（复用/仿照
  `github_symbols.py::_get_download_lock` 的模式），避免多个 task 并发解压同一个包时
  互相踩踏。

`get_android_mapping()` 同理但更简单：上传的就是原始 `.txt`，找到该目录下第一个 `.txt`
文件直接返回路径，不需要解压。

`get_dart_symbols_dir()` 比照 iOS 的 tar.gz 解压模式。

`get_android_native_symbols_dir()` 同样先查本地上传的 `native_symbols.tar.gz`（按
(platform=android, symbol_type=native_symbols, app_version) 精确匹配），解压逻辑和现有
GitHub 那份一致（只保留 arm64-v8a merged_native_libs 下的 libflutter.so/libapp.so，其余
丢弃，沿用 github_symbols.py 现有注释里的体积决策），查不到再回退 GitHub 下载。

### 上传完整性校验

`symbol_type in {"dsym", "dart_symbols", "native_symbols"}` 时（这三种上传的是压缩包），保存后用
`zipfile.is_zipfile(dest_path)` 校验；不合法则删除已写入的文件、返回 `400`，让 Jenkins
构建日志里能立刻看到上传失败，而不是几个月后线上堆栈还是查不到符号才发现传的是个坏文件。
`proguard_mapping` 是纯文本，不做格式校验。

### jank 回填

新增一个轻量扫描（挂在现有 `analyze_tick`（每 5 分钟）或 `pipeline`（每 4 小时）cron 里，
实现时二选一，倾向 `analyze_tick`——更快感知到符号包补传）：

```
查询 crash_issues WHERE kind='jank' AND fixable=1 AND (堆栈仍是占位符)
对每条：重新走一遍 _symbolicate_new_jank_issue() 同款逻辑（module/pc/base 从最近一次
Datadog 事件里重新取，而不是复用旧的 parsed 数据）
```

"堆栈仍是占位符"的判定复用 `jank_ingester.py::_jank_frame_looks_symbolized()` 现有的
启发式（结果等于原文本，或含 `" + 0x"` 视为未命中）。

范围限定为 **jank only**——crash/ANR 的 `representative_stack` 一旦被覆写，原始地址信息
已丢失，无法用同样的"重试"方式回填（见"这次不做"一节）。

## 错误处理

- 上传接口：非法 platform/symbol_type → 400（已有）；文件不是合法压缩包（dsym/dart_symbols/
  native_symbols 类型）→ 400（新增）。
- 读取侧：本地包检查、解压、加锁任何一步异常 → 按现有"容错优先"风格捕获异常、原样回退到
  GitHub 逻辑，不向上抛异常影响主符号化流程。
- 打包脚本：上传失败只打日志，不影响构建产物的 App Store/Google Play 上传等后续步骤。

## 测试计划

- jarvis 单测（`backend/tests/crashguard/`）：
  - `get_ios_dsyms_dir`/`get_android_mapping`/`get_dart_symbols_dir`/
    `get_android_native_symbols_dir`：本地有精确匹配的已上传包时不发起 GitHub 请求；没有时
    照常回退 GitHub（mock 文件系统 + mock GitHub 调用）。
  - 上传接口：非法压缩包内容返回 400 且不留残留文件；合法压缩包正常入库 + 清理旧版本（现有
    逻辑的补测,之前完全没有覆盖）；新增 `symbol_type=native_symbols` 的合法性校验。
  - jank 回填扫描逻辑：命中"占位符"的 issue 被重新处理，已正确符号化的 issue 不被打扰。
- plaud-native-app2：shell 脚本无单元测试框架，验证方式是真实触发一次 Jenkins 构建（或
  手动 curl 模拟同样的调用），确认文件落到 102 的 `/data/symbols/...` 且能在
  `GET /api/crash/symbols` / 设置页看到。

## 已知遗留问题（本设计明确不解决，供后续参考）

1. CN flavor 的 native 崩溃暂时依旧没有可靠符号化（现状延续，不是本次引入的回归）。
2. 已经被错误符号覆写的历史 crash/ANR issue（如本次调查触发点的那条 AppHang
   `524e25c6-59a4-11f1-bd6b-da7ad0900002`）不会被本设计自动修复，需要单独的"强制重置+
   重新符号化"工具，留作后续 ticket。
