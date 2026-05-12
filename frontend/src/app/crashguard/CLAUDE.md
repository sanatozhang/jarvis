# Crashguard 前端模块

后端文档见 `backend/app/crashguard/CLAUDE.md`。

## 页面结构

```
frontend/src/app/crashguard/
├── page.tsx               主页：Top issue 列表 + 详情抽屉 + 健康度 header
├── reports/
│   └── page.tsx           历史日报列表 + 单份日报详情
└── pull-requests/
    └── page.tsx           Crashguard auto / 半自动 PR 列表 + GitHub 状态
```

入口 sidebar item 由 `frontend/src/components/Sidebar.tsx` 用 `fetchCrashEnabled()` 探测，后端关闭 kill switch 时整个入口隐藏。

## 状态约定

| 状态 | 颜色 | label |
|------|------|-------|
| `open` | 红 `#DC2626` | 未处理 |
| `investigating` | 黄 `#D97706` | 排查中 |
| `resolved_by_pr` | 绿 `#16A34A` | 已修复 |
| `ignored` | 灰 `#6B7280` | 忽略 |
| `wontfix` | 灰 `#6B7280` | 暂不修 |

平台 alias：`flutter→Flutter` / `ios→iOS` / `android→Android` / `browser→Web`。

## 主页 header 信息

| 元素 | 数据源 | API |
|------|--------|-----|
| 最新版本（按平台） | 三平台各派生 | `GET /api/crash/latest-release` → `versions` + `source` |
| 用户量最大（仅 android / ios） | 24h Datadog RUM cardinality(@usr.id) | 同上 → `top_user_versions` + `top_user_versions_source` |
| 受影响会话 / 总事件 | 当日聚合 | `GET /api/crash/top` 响应 totals |
| Datadog 未配置警告 | `health` | `GET /api/crash/health::datadog_configured` |

source 字段渲染规则（取自 page.tsx）：
- 最新版本：`config_override → ·配置` / `derived → ·派生` / `unknown → 无标` 
- 用户量最大：`datadog_rum → ·RUM` / `crash_issues_fallback → ·崩溃回落` / `unknown → 无标`

## API 调用（全部在 src/lib/api.ts）

| Wrapper | 后端端点 |
|---------|---------|
| `fetchCrashTop` | `/api/crash/top` |
| `fetchCrashIssue` | `/api/crash/issues/{id}` |
| `updateCrashIssue` | `PATCH /api/crash/issues/{id}` |
| `analyzeCrashIssue` | `POST /api/crash/analyze/{id}` |
| `fetchCrashAnalyses` / `fetchCrashAnalysisStatus` | `/api/crash/issues/{id}/analyses` |
| `followupCrashIssue` | `POST /api/crash/issues/{id}/followup` |
| `batchAnalyzeCrash` | `POST /api/crash/batch-analyze` |
| `runCrashDailyReport` | `POST /api/crash/reports/run-now` |
| `approveCrashPr` | `POST /api/crash/approve-pr/{id}` |
| `fetchAutoPrQueue` | `/api/crash/auto-pr-queue` |
| `fetchCrashLatestRelease` | `/api/crash/latest-release` |
| `triggerCrashWarmup` | `POST /api/crash/warmup` |
| `fetchCrashHealth` | `/api/crash/health` |
| `fetchCrashEnabled` | health 派生（用于 Sidebar） |

类型定义同样在 `api.ts` 中 `export interface CrashTopItem / CrashIssueDetail / CrashAnalysisRecord / CrashLatestRelease / ...`。组件 import 使用，**不要自己在组件里重新声明 shape**。

## 深链 & URL 同步

- 主页 `?issue=<id>` 自动开详情抽屉，关抽屉 `router.replace` 去掉 query
- Reports 页 `?report=<id>` 同理
- PR 页支持 status / platform 筛选写入 query

## 一键 PR approve（半自动 PR）

Android / iOS 走 `feasibility 0.5~0.7` 区间的方案需人工 approve：

1. 在详情抽屉点「✋ 一键提交 PR」
2. 调 `approveCrashPr(analysisId)`
3. 后端校验 user role（admin 白名单）后调 `repo_updater.create_branch_pr(..., draft=True)`
4. 返回 PR URL，前端跳转 / 高亮

## 主题色

- 沿用站点金调 `#B8922E`（`D.accent`）+ jarvis 主题 token，定义在 `page.tsx` 顶部 `const D = {...}`
- P0 红 / P1 蓝用于 tier 标识
- Toast / Modal 复用 `@/components/Toast` 等共享组件

## i18n

中文 key 写在 `useT()` 里，对应英文翻译加到 `src/lib/i18n.ts`（Crashguard 的 key 已混在主 i18n.ts 里，未拆出独立文件）。
