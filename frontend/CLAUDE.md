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

全局 `FeedbackWidget`（右下角悬浮反馈，html2canvas 截图 + `/api/site-feedback`）挂在 `layout.tsx`。

## 工单详情面板：「分析结果」区已抽成共享组件

「工单分析结果」对话区（初次/追问卡片、问题类型、置信度、模型、需工程师 badge、
问题原因、关键证据、Agent 轨迹、建议回复、深度分析 CTA、追问输入）已抽成
**`src/components/AnalysisResultView.tsx`，首页 + tracking 共用同一份**。以首页（更丰富）
为基准；首页专属的交互（客服反馈 widget）通过 `renderEngineerFeedback` slot 注入，
CN/EN 切换通过 `onSetLang` 注入（仅首页传），tracking 跟随站点语言。

→ **改分析结果卡片只改 `AnalysisResultView.tsx` 一处**，两页自动同步。

| 页面 | 文件 | 状态字段 |
|------|------|---------|
| 首页 `/`（工单分析） | `src/app/page.tsx` | `detailId` + `detailData` |
| `/tracking`（工单跟踪） | `src/app/tracking/page.tsx` | `detailItem` |

⚠️ 注意：详情面板**其余部分**（面板外壳/meta 信息栅格/附件/升级转交/标记完成等动作区）
**仍各写一份**，改这些区域两处都要同步。右侧停靠 35% 分栏 / Esc 关闭 / 选中行高亮的
壳层用 `globals.css` 的 `panel-slide-in`，配色 token 用 `IssueComponents.tsx` 的 `S`。

## 数据流

```
组件 → src/lib/api.ts wrapper → /api/* (Next.js rewrite 转发) → backend:8000
                                ↑
                       SSE 走 EventSource，同样走 rewrite
```

## 子模块前端文档

- `src/app/crashguard/CLAUDE.md` — Crashguard 前端（首页 + reports + pull-requests 三个子页）
- 工单分析 / Oncall / 数据统计 的页面级约定见 `docs/modules/*.md` 「前端」章节
