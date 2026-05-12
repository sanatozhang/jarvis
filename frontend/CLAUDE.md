# Frontend — Jarvis（Next.js 15 + React 19 + Tailwind CSS 4）

通用前端约定。业务模块的页面细节见 `docs/modules/*.md` 的「前端」章节。

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
│   ├── page.tsx            首页：工单分析主入口
│   ├── tracking/           工单追踪（详情抽屉支持 ?detail=<id> 深链）
│   ├── feedback/           本地工单提交（fb_ 前缀）
│   ├── rules/              规则 CRUD
│   ├── reports/            报表
│   ├── oncall/             Oncall 排班 + 当前值班
│   ├── analytics/          数据统计仪表盘
│   ├── crashguard/         Crashguard 子模块（看 frontend/src/app/crashguard/CLAUDE.md）
│   ├── settings/           系统设置
│   └── eval/ wishes/ samples/ tools/  辅助工具
├── components/             共享 UI 组件（Toast、MarkdownText、Sidebar 等）
└── lib/
    ├── api.ts              所有后端 API 调用集中在这里（单一抓手）
    └── i18n.ts             中文 key → 英文翻译，用 useT() 取
```

## 核心约定

| 主题 | 约定 |
|------|------|
| API 调用 | 全部进 `src/lib/api.ts`。新接口先在这里加 wrapper + 类型，组件只用 wrapper，不 `fetch` 直连 |
| 实时进度 | SSE 走 `subscribeTaskProgress()`（api.ts），用 EventSource。任务详情页订阅，结束自动关闭 |
| i18n | 文案用 `t("中文 key")`，`useT()` hook 取 t 函数。新文案先在 `i18n.ts` 加 key → 英文 |
| API 地址 | `NEXT_PUBLIC_API_URL`：本地默认 `http://localhost:8000`；Docker build 通过 `frontend/Dockerfile` 的 `ARG` 注入（默认 `http://backend:8000`），不能漏 ARG 否则 SSR rewrites 回退到 localhost |
| 主题色 | 站点金调 `#B8922E`（jarvis gold），Crashguard 内部复用相同 token，定义在每个页面顶部 `const D = {...}` |
| 状态色 | open=红 / investigating=黄 / resolved=绿 / ignored,wontfix=灰 |
| 深链 | 详情抽屉用 URL query 同步：进入 `?detail=<id>` 自动开抽屉，关闭去掉 query。`router.replace` 不 `push` |
| 类型 | 后端响应类型在 `api.ts` 中定义并 `export`，组件 import 使用；不在组件内自己重复声明 shape |

## 数据流

```
组件 → src/lib/api.ts wrapper → /api/* (Next.js rewrite 转发) → backend:8000
                                ↑
                       SSE 走 EventSource，同样走 rewrite
```

## 子模块前端文档

- `src/app/crashguard/CLAUDE.md` — Crashguard 前端（首页 + reports + pull-requests 三个子页）
- 工单分析 / Oncall / 数据统计 的页面级约定见 `docs/modules/*.md` 「前端」章节
