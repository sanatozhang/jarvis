# Frontend — jarvis（Next.js 15 + React 19 + Tailwind CSS 4）

通用前端约定。工单处理相关页面已物理迁出到独立仓库 Apollo（2026-08），本仓库根路径直接
`router.replace("/crashguard")`，只剩 crashguard/release/settings 三块。

## 启动

```bash
cd frontend
npm install
npm run dev       # http://localhost:3000
npm run build
npm run lint
npm start
```

## 目录约定（App Router）

```
src/
├── app/
│   ├── layout.tsx          全局布局（侧栏 + 主题）
│   ├── page.tsx            根路径重定向到 /crashguard
│   ├── crashguard/         crashguard 子模块（看 frontend/src/app/crashguard/CLAUDE.md）
│   ├── release/            Release 自动化看板
│   ├── settings/           系统设置
│   └── api/crash/          crashguard 专用的 Next.js route handler（非通用 API）
├── components/             共享 UI 组件（Toast、Sidebar 等）
└── lib/
    ├── api.ts              所有后端 API 调用集中在这里（单一抓手）
    └── i18n.ts             中文 key → 英文翻译，用 useT() 取
```

⚠️ `src/components/AnalysisResultView.tsx` 与 `IssueComponents.tsx` 是工单详情面板的
遗留组件，物理拆分时未清理（已知后续瘦身项，非 bug）——两者已无任何页面引用，不要
再往里加新功能，新的 crashguard UI 一律走 `crashguard/CLAUDE.md` 里的既有组件。

## 核心约定

| 主题 | 约定 |
|------|------|
| API 调用 | 全部进 `src/lib/api.ts`。新接口先在这里加 wrapper + 类型，组件只用 wrapper，不 `fetch` 直连 |
| i18n | 文案用 `t("中文 key")`，`useT()` hook 取 t 函数。新文案先在 `i18n.ts` 加 key → 英文 |
| API 地址 | `NEXT_PUBLIC_API_URL`：本地默认 `http://localhost:8000`；Docker build 通过 `frontend/Dockerfile` 的 `ARG` 注入（默认 `http://backend:8000`），不能漏 ARG 否则 SSR rewrites 回退到 localhost |
| 主题色 | 站点配色定义在每个页面顶部 `const S = {...}`（或 crashguard 页面内的 `D`） |
| 状态色 | open=红 / investigating=黄 / resolved=绿 / ignored,wontfix=灰 |
| 深链 | 详情类交互用 URL query 同步：`router.replace` 不 `push`，避免污染浏览器历史 |
| 类型 | 后端响应类型在 `api.ts` 中定义并 `export`，组件 import 使用；不在组件内自己重复声明 shape |

全局 `FeedbackWidget`（右下角悬浮反馈，html2canvas 截图 + `/api/site-feedback`）挂在 `layout.tsx`。

## 数据流

```
组件 → src/lib/api.ts wrapper → /api/* (Next.js rewrite 转发) → backend:8000
                                ↑
                       SSE 走 EventSource，同样走 rewrite
```

## 子模块前端文档

- `src/app/crashguard/CLAUDE.md` — crashguard 前端（首页 + reports + pull-requests 三个子页）
