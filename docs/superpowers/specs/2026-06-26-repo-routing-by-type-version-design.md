# 源码仓库路由（按工单类型 + 版本）设计

- 日期：2026-06-26
- 作者：sanato
- 状态：设计已评审，待落实施计划

## 背景与问题

Jarvis 当前服务的是 Flutter 写的 Plaud App。两件事正在/即将发生：

1. **App 从 Flutter 迁移到原生开发**，代码仓库从旧 Flutter 壳（`Plaud2` / `plaud_ai`，内含 `plaud-flutter-common/global/cn` + `plaud-android/plaud-ios`）切到新原生壳 `plaud-native-app`（含 `plaud-native-android` / `plaud-native-ios`，`plaud-native-harmony` 暂忽略）。**切换按 App 版本号**：`3.x.x` 及以下 = Flutter 仓，`4.x.x` 起 = native 新仓；android / ios 同一条切换线 `4.0.0`。
2. **工单分析后续要支持 web 和 desktop**（crashguard 仍只覆盖 app）。

这导致「源码地址」从一个静态的 `平台 → 路径` 映射，升级成 `(平台/类型, 版本) → 仓库` 的函数。它影响**四个出口**：① 工单分析的源码、② crashguard 自动修复 PR 的目标仓、③ crashguard 崩溃栈符号化的来源、④ crashguard 拉 Datadog 的 service 过滤（原生迁移后 Datadog service/appid 也变）。

### 现状（迁移前）

| 关注点 | 现状 | 代码位置 |
|---|---|---|
| 工单分析选仓 | `get_code_repo_for_platform(platform)`，`app/web/desktop → code_repo_app/web/desktop` 静态路径，无版本维度 | `app/config.py:438`、`app/workers/analysis_worker.py:581`、`app/services/eval_runner.py:227` |
| crashguard 选仓 | 独立的 `_platform_repo_path(platform, sub_hint)`，词汇是 `flutter/android/ios`，含 `global/cn/common` 下钻 + blob 探测 | `app/crashguard/services/pr_drafter.py:339` |
| 多仓更新 | `repo_updater` + `mt_runner`，mt fan-out 旧 Flutter 壳 | `app/services/repo_updater.py`、`app/services/mt_runner.py` |
| 符号化来源 | `_REPO = "Plaud-AI/Plaud-App"` 写死，资产名全 Flutter 形态 | `app/crashguard/services/github_symbols.py:39` |
| Datadog 身份 | 单一字符串 `datadog_service_filter = "service:plaud-flutter"`，注入所有查询 | `app/crashguard/config.py:140`、`datadog_client.py:71` |

**核心痛点**：①②③④ 四个出口各自硬编码、词汇不统一（`app/web/desktop` vs `flutter/android/ios`），且全部缺版本维度。原生上线当天，若只改源码不改符号化，crashguard 会直接哑火（崩溃栈符号化失败 → AI 拿一堆地址 → PR 质量崩）。

## 目标

1. 引入**单一真相源** `repo_router`，输入 `(platform, version)`，输出仓库路径 + 子仓 + GitHub 仓 + 符号化 profile + family，**同时驱动四个出口**。
2. 统一两套词汇，消除重复选仓逻辑。
3. 配置可在 `config.yaml` 维护，并支持 **UI 设置页直接编辑路径 / 切换版本 / service filter**（DB override 持久化）。
4. 新旧仓**长期共存**：旧 Flutter 仓不下线（旧版本崩溃/工单会持续很久），新旧仓都由 `repo_updater` 定时更新。
5. **向后兼容**：现有部署升级不炸；web/desktop 仓未配置时优雅降级（logs-only，不崩、不建 PR）。

## 非目标

- 不改 crashguard 隔离合约的本质（仍只通过白名单的 4 个耦合点对外）。crashguard 调用 `repo_router` 需作为新增「允许耦合点」走 ADR 流程（见「隔离合约影响」）。
- 不支持鸿蒙（`plaud-native-harmony`）——但数据模型留口，未来加一段 band 即可，代码零改。
- 不做 web/desktop 的符号化（`symbol_profile: none`）。
- 不重构 mt_runner 的多仓语义，只新增「submodule 壳更新」分支。

---

## §1 数据模型 + 作用域 + Datadog 身份

### 作用域

| 子系统 | 覆盖平台 | 用 repo_router | 用 Datadog service |
|---|---|---|---|
| Crashguard | 仅 app（android/ios；flutter + native 两代）| ✅ 仅传 android/ios | ✅ 需覆盖新旧两代 |
| 工单分析 | app + web + desktop | ✅ 全平台 | ❌（不走 Datadog）|

### `repo_router` 配置 —「每平台版本带（bands）」

统一了「单一切换点（=2 个 band）」与「web/desktop 无切换（=1 个 band）」两种情况。`min_version` 降序匹配，未来加切换点只是加一段，**不改代码**。

```yaml
repo_routing:
  android:
    bands:
      - {min_version: "0",     family: flutter, wrapper: /Users/mac/Downloads/plaud_ai,        sub: plaud-android,        github_repo: Plaud-AI/Plaud-App,            symbol_profile: flutter_android}
      - {min_version: "4.0.0", family: native,  wrapper: /Users/mac/Downloads/plaud-native-app, sub: plaud-native-android, github_repo: Plaud-AI/plaud-native-android, symbol_profile: native_android}
  ios:
    bands:
      - {min_version: "0",     family: flutter, wrapper: /Users/mac/Downloads/plaud_ai,        sub: plaud-ios,            github_repo: Plaud-AI/Plaud-App,        symbol_profile: flutter_ios}
      - {min_version: "4.0.0", family: native,  wrapper: /Users/mac/Downloads/plaud-native-app, sub: plaud-native-ios,     github_repo: Plaud-AI/plaud-native-ios,    symbol_profile: native_ios}
  web:
    bands:
      - {min_version: "0", family: web,     wrapper: /Users/mac/Downloads/plaud-web, sub: "", github_repo: Plaud-AI/plaud-web, symbol_profile: none}
  desktop:
    bands:
      - {min_version: "0", family: desktop, wrapper: /Users/mac/Downloads/fe-nexus,  sub: "", github_repo: Plaud-AI/fe-nexus,  symbol_profile: none}
```

> 注：本地开发环境旧 Flutter 壳为 `/Users/sanato/Desktop/code/newplaud/Plaud2`，服务器（102/100）为 `/Users/mac/Downloads/plaud_ai`。路径走 env > DB(UI) > yaml，本地与服务器各自覆盖。

### 仓库结构事实（影响更新策略与下钻）

| 仓 | 结构 | 更新方式 | 子仓下钻 |
|---|---|---|---|
| 旧 Flutter 壳 `plaud_ai` | mt 工作区（无顶层 `.git`，子目录各自 `.git`）| `mt` fan-out（现状）| flutter family：common / global / cn，靠 `sub_hint` + blob 探测 |
| `plaud-native-app` | git submodule 壳（**有顶层 `.git`**）| `git submodule update --recursive`（**新增**）| native family：单 submodule（android / ios 各一），**跳过** flutter 下钻 |
| `fe-nexus`（desktop）| git submodule 壳（`packages/Plaud_3A_package` 等）| `git submodule update --recursive`（**新增**）| desktop：壳根即代码，`sub=""` |
| `plaud-web`（web）| 普通单仓（无 `.gitmodules`）| 普通 `git pull` | 无 |

### Datadog service filter（crashguard 专属，独立于 repo_router）

```yaml
crashguard:
  datadog:
    # 共存期：新旧 service 都拉进同一池，再靠 issue 的 (platform, version) 经 repo_router 路由到对应仓
    service_filter: "(service:plaud-flutter OR service:plaud-native-android OR service:plaud-native-ios)"
```

- **Datadog 维度 ≠ repo 维度，分开建模**：service filter 只管「把哪些 app 的崩溃拉进来」；拉进来后每条 issue 仍用 `(platform, version)` 经 repo_router 选仓。
- 共存期用 `OR` 覆盖两代——正是 `datadog_client.py:62` 注释预埋的写法。
- ⚠️ **实施前必须在 Datadog 实测原生真实 service tag**（是 `service:plaud-native-android` 还是别的），不照抄注释。
- web/desktop 不进 crashguard，service filter 不含它们（继续过滤 `plaud-web/plaud-desktop` 污染，`datadog_client.py:208`）。

---

## §2 核心模块 `repo_router`

新建 `backend/app/services/repo_router.py`（jarvis 主流服务；crashguard 通过新增耦合点调用，见隔离合约影响）。

```python
def resolve(platform: str, version: str | None, *,
            sub_hint: str = "", stack_text: str = "") -> RepoResolution | None
```

```python
@dataclass
class RepoResolution:
    family: str          # flutter | native | web | desktop
    platform: str        # android | ios | web | desktop
    wrapper_path: str    # 壳工程绝对路径（分析 / 更新用）
    sub_repo_path: str   # 下钻后真实子仓绝对路径（分析 Read/Grep + PR checkout）
    logical_name: str    # 如 plaud-native-android
    github_repo: str     # 如 Plaud-AI/plaud-native-android —— PR 目标 + 符号 release 源
    symbol_profile: str  # native_android | native_ios | flutter_android | flutter_ios | none
    confidence: str      # high | low（版本缺失回落时为 low）
```

### 解析算法

1. **归一 platform**：把 crashguard 的 `flutter` 与 jarvis 的 `app` 按 `@os.name` / log `app-platform` 细分到 `android` / `ios`；`web` / `desktop` 原样。无法归一 → 返回 `None`（调用方降级）。
2. **解析 version**：`strip` 掉 `-634` 这类 build 后缀后做 semver 解析（`major.minor.patch`）。例：`"3.16.0-634"` → `3.16.0`。
3. **选 band**：在该平台 `bands` 中按 `min_version` 降序，取第一个满足 `version >= min_version` 的 band。
4. **family-specific 下钻**：
   - `flutter`：沿用现有 `_platform_repo_path` 的 `sub_hint`（global/cn/common）+ blob 探测逻辑（迁移进 router 或保留在 pr_drafter 由 family 门控）。
   - `native` / `desktop`：壳下单 submodule（或壳根），直接拼 `wrapper/sub`，跳过 flutter 下钻。
   - `web`：`wrapper` 即仓根。
5. **派生** `github_repo` + `symbol_profile`：取自 band 配置。
6. **校验存在性**：`wrapper`/`sub_repo` 路径不存在或非 git → 返回 `None`（让调用方降级；同时由 repo_updater 健康告警）。

### 版本缺失 / 数据矛盾

- **version 缺失** → 取该平台**最新 band** + `confidence=low` + 记日志（不猜旧仓）。
- **数据矛盾**（如 issue 带 native service 但 `version < 4.0.0`）→ **仍按 version 路由**（版本是用户选定的主信号）；`service` 仅作校验，冲突记 warning 不阻断。

---

## §3 四个出口全部由 router 驱动

| 出口 | 改造点 | 说明 |
|---|---|---|
| **源码** | `analysis_worker.py:581`、`eval_runner.py:227` | `res = repo_router.resolve(platform, version)`；`prepare_workspace(code_repo=res.sub_repo_path)`。`res is None` → logs-only 降级 |
| **PR** | `pr_drafter.py:372` | checkout `res.sub_repo_path`；`gh pr create` 指向 `res.github_repo`；**`res.family` 门控** flutter 专属子仓探测（native/desktop 跳过）；reviewer blame 在 `res.github_repo` 上跑 |
| **符号化** | `github_symbols.py:39`、`symbolication.py` | `_REPO` 改由 `res.github_repo` 注入；按 `res.symbol_profile` 选资产名 + 符号化策略（native_android：纯 R8 mapping + 原生 `.so`，**无 dart 符号**；native_ios：app dSYM，**无 Flutter.dSYM**）。`symbolicate_stack` 接收 `res` |
| **Datadog** | `config.py:140`、`datadog_client.py` | crashguard `service_filter` 独立覆盖两代（§1）；拉进来的 issue 用 `representative_stack.sample_app_version`（单值代表版本，**非** `top_app_version` 分布串）经 router 路由 |

> 一个 crash issue 的 `top_app_version` 是分布字符串（`"3.16.0-634 (60%), 3.15.1-631 (30%)"`），不能用于路由；用 `representative_stack` JSON 里的 `sample_app_version` 单值。

---

## §4 配置 + UI

### 配置分层

复用现有 agent_overrides 模式：**env > DB override(UI) > yaml > defaults**。DB override 写 `oncall_config` 表（或同构表），启动时 merge 回内存；不直接改 yaml（yaml 是带注释模板）。支持热加载。

### UI 设置页 ——「源码仓库路由」卡片

- 每平台 `bands` 表格，可增 / 删 / 改 band：`min_version`、`family`、`wrapper`、`sub`、`github_repo`、`symbol_profile`。
- **路径校验**：保存时校验 `wrapper` 存在、是 git 仓 / submodule 壳、`sub` 子仓存在。
- **解析预览**：输入 `(platform, version)`（如 `android 3.21.0`）即时显示命中的仓与 family。
- crashguard `service_filter` 编辑框（带「在 Datadog 实测确认」提示）。

---

## §5 边界与降级

| 场景 | 行为 |
|---|---|
| 版本缺失 | 取最新 band + `confidence=low` + 日志，不猜旧仓 |
| 数据矛盾（service 与 version 不一致）| 按 version 路由，service 仅校验，冲突记 warning |
| 跨界 issue（版本分布横跨 4.0.0）| 用 `sample_app_version` 单值路由 |
| web/desktop 仓未配置 | `resolve` 返回 `None` → 分析降级 logs-only、不建 PR、不崩 |
| 路径不存在 / 非 git | `resolve` 返回 `None`；repo_updater 跳过 + 健康告警 |
| 鸿蒙 | 配置加一段 platform/band 即可，代码零改 |

---

## §6 迁移兼容 + Provision

### 向后兼容 backfill

- 旧 env `CODE_REPO_APP` / `CODE_REPO_PATH` / `CODE_REPO_WEB` / `CODE_REPO_DESKTOP` 和 crashguard `repo_path_flutter/android/ios` → 自动生成 flutter-family bands（`min_version: "0"`），native bands 待配。**现有部署升级不炸**。
- `get_code_repo_for_platform` 降级为「无版本回落」thin wrapper（标 `deprecated`，内部调 router 取最新 band 或 flutter 默认）；4 个调用点改造为显式传 `version`。

### repo_updater 改造

- 新增 **submodule 壳更新分支**：`plaud-native-app` / `fe-nexus` 有顶层 `.git` → 现有 `_is_mt_workspace` 判为「普通 git」→ `git pull` **不更新 submodule**（bug）。改为检测 `.gitmodules` 存在 → `git fetch && git checkout main && git pull && git submodule sync --recursive && git submodule update --remote --recursive`。
- **每 wrapper 独立 lock**（现共享 `code_repo_app/.jarvis.lock`，多壳会互相阻塞）。
- `get_all_code_repos` 改为从 `repo_routing` 收集所有 distinct `wrapper`。

### 部署 Provision

- 102 / 100 各 `git clone --recursive` `plaud-native-app`、`plaud-web`、`fe-nexus` 到 `/Users/mac/Downloads/`。
- 评估磁盘（多出 3 个壳 + submodule）。

### 隔离合约影响（crashguard）

crashguard 调用 `app.services.repo_router.resolve` 是**新增对外耦合点**，需走 ADR 流程：
1. 更新 `docs/adr/0001-crashguard-isolation.md` 记录决策；
2. 加白名单到 `backend/.importlinter`；
3. PR 描述说明耦合点必要性；
4. CI `lint-imports` 通过。

---

## §7 测试

- `repo_router` 单测：band 选择 / 带 build 后缀的 semver（`3.16.0-634`）/ `4.0.0` 边界（`3.99.0` → flutter，`4.0.0` → native）/ 版本缺失回落 / 未配置平台返回 None / family 派生正确。
- 符号化 profile 选择单测：native_android 不走 dart 符号、native_ios 不走 Flutter.dSYM。
- 更新 `pr_drafter` 现有测试：family 门控（native 跳过 flutter 子仓探测）、`github_repo` 目标正确。
- repo_updater：submodule 壳更新分支单测（mock git）。

---

## 实施顺序（建议）

1. `repo_router` 模块 + 配置加载 + 单测（纯函数，零副作用，先立地基）。
2. backfill + `get_code_repo_for_platform` 降级 + 4 调用点改造（源码出口先通）。
3. repo_updater submodule 壳分支 + 每仓 lock + provision（让新仓可更新）。
4. 符号化 family 路由（`github_symbols` + `symbolication`）。
5. crashguard service_filter 两代覆盖（**先 Datadog 实测真实 service tag**）+ ADR/importlinter。
6. UI 设置页「源码仓库路由」卡片 + DB override + 路径校验 + 解析预览。
7. 可观测：每次分析 / PR 落 audit 日志（resolved repo + family + version + confidence）。

## 待确认 / 风险

- 原生在 Datadog 的**真实 service tag** 需上线前实测确认。
- 原生符号化的 **release 仓 + 资产命名**（native android 的 mapping/.so、native ios 的 dSYM 包名）需向 App 团队确认，可能与 Flutter 资产名不同。
- 磁盘容量（多壳 + submodule）。
