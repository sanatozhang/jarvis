# Gmail SSO 账号体系设计

**日期**: 2026-05-20
**作者**: sanato.zhang@plaud.ai
**状态**: Draft → 待评审

---

## 1. 背景与目标

### 1.1 现状

Apollo（jarvis 工单平台）当前账号体系是"裸登录"：

- `UserRecord` 主键 `username`，无密码、无 token、无 session
- 前端 `localStorage.setItem("appllo_username", v)` + 调 `/api/users/login` 拿 role
- 任何人输入任意 username 即可获得对应权限
- 所有后端 API（除 `/api/v1/analyze` 有 API Key）裸奔无 auth

### 1.2 目标

引入企业级账号体系：

1. **Google OAuth 2.0 (OIDC) SSO 登录**
2. **仅允许 `@plaud.ai` 域名**
3. **硬闸门**：未登录全站 403
4. **1 年长 session**（业务低频，避免频繁登录）
5. **业务流程零改动**：username 仍是主键，所有外键不动
6. **复用 `feishu_email` 字段**：SSO 登录时自动写入，飞书通知按此字段发送，无该字段则跳过
7. **feature flag 控制**：老机器保持现状，新机器强制 SSO，平滑过渡

### 1.3 非目标（YAGNI）

- ❌ 不引入 google_sub 字段防改邮箱（内部工具无此场景）
- ❌ 不做 token 吊销表（1 年 JWT 自包含足够，离职走人工删库）
- ❌ 不做"踢下线"多设备互斥
- ❌ 不做密码登录、不做 2FA、不做邮件验证
- ❌ Gmail 不作为通知通道（通知全程飞书）

---

## 2. 顶层共识（已对齐决策）

| 维度 | 决策 |
|---|---|
| SSO Provider | Google OAuth 2.0 / OIDC |
| 域名限制 | 仅 `@plaud.ai`（`SSO_ALLOWED_DOMAINS` env 可配置） |
| 闸门强度 | 硬闸门（未登录全站 403） |
| Session 有效期 | 1 年（`SSO_COOKIE_DAYS=365`） |
| Session 载体 | httpOnly Cookie + JWT 自包含 |
| Feature Flag | `ENABLE_GMAIL_SSO`，默认 false（兼容老机器） |
| 数据迁移 | 保留 username PK，复用 `feishu_email` 字段，不加新列 |
| 老用户认领 | 派生 `username = email.split('@')[0]`，匹配则更新 feishu_email，不匹配则新建 |
| Gmail 用途 | 仅登录入口 + 飞书通知地址来源 |
| 机器调用 | EXEMPT_PATHS 白名单 + 独立 token（API Key / webhook secret） |
| Admin 来源 | `.env` 的 `ADMIN_EMAILS` 白名单 |
| Admin 升降 | 只升不降（SSO 不主动降级 admin，降级靠人工） |
| 部署域名 | `https://apollo.nicebuild.click/` |

---

## 3. 架构总览

```
┌────────────────────────────────────────────────────────────┐
│                       Frontend (Next.js)                    │
│  ┌────────────┐    ┌─────────────────┐   ┌──────────────┐  │
│  │ /login page│ →  │ AuthProvider    │ → │ AuthGate     │  │
│  │ Google btn │    │ (React Context) │   │ (layout.tsx) │  │
│  └─────┬──────┘    └────────┬────────┘   └──────┬───────┘  │
└────────┼────────────────────┼───────────────────┼───────────┘
         │ ① 点登录            │ ③ fetch + Cookie  │ ④ 401 → /login
         ▼                    ▼                   ▼
┌────────────────────────────────────────────────────────────┐
│                  Backend (FastAPI)                          │
│  ┌──────────────────────────────────────────────────┐      │
│  │  AuthMiddleware (启用条件: ENABLE_GMAIL_SSO=true) │      │
│  │  ├─ EXEMPT_PATHS 直通                             │      │
│  │  ├─ 解 JWT Cookie → request.state.user            │      │
│  │  └─ 无效/缺失 → 401                                │      │
│  └──────────────────────────────────────────────────┘      │
│                                                             │
│  /api/auth/google/login   /api/auth/google/callback         │
│  /api/auth/me              /api/auth/logout                 │
│         │                  │                                │
│         ▼                  ▼                                │
│       Google OAuth Server (accounts.google.com)             │
└────────────────────────────────────────────────────────────┘
```

---

## 4. 数据模型

### 4.1 UserRecord（不新增字段）

```python
class UserRecord(Base):
    __tablename__ = "users"
    username       = Column(String(64), primary_key=True)
    role           = Column(String(16), default="user")        # admin / user
    feishu_email   = Column(String(128), default="")           # ← SSO 自动写入
    created_at     = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, nullable=True)
```

**关键决策**：复用 `feishu_email` 字段作为"SSO email"和"飞书通知地址"的统一载体。语义本来就是"发飞书消息用的邮箱"，SSO 登录时自动填写。**不新增任何字段**。

### 4.2 无新增表

砍掉了：
- `RevokedTokenRecord`（无吊销诉求）
- `LegacyUsernameMap`（新机器强制 SSO，不存在老用户认领冲突）

---

## 5. 后端实现

### 5.1 配置项（`backend/app/config.py` + `.env`）

```bash
# 总开关
ENABLE_GMAIL_SSO=false

# Google OAuth
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxx
GOOGLE_REDIRECT_URI=https://apollo.nicebuild.click/api/auth/google/callback

# 域名 / Session
SSO_ALLOWED_DOMAINS=plaud.ai
SSO_JWT_SECRET=<openssl rand -hex 32>
SSO_COOKIE_NAME=jarvis_session
SSO_COOKIE_DAYS=365
SSO_COOKIE_SECURE=true             # 生产 true，本地 dev false
# SSO_COOKIE_DOMAIN 不设（仅 apollo.nicebuild.click 生效）

# 角色 / 豁免
ADMIN_EMAILS=sanato.zhang@plaud.ai
SSO_EXEMPT_PATHS=/api/health,/api/linear/webhook,/api/v1/,/api/auth/

# Webhook 独立 token
LINEAR_WEBHOOK_SECRET=<新增或已有>
```

### 5.2 模块拓扑

```
backend/app/
├── config.py                ← + SSO_* 字段
├── main.py                  ← + 注册 AuthMiddleware + auth router
├── middleware/
│   └── auth.py              ← 【新】AuthMiddleware
├── services/
│   ├── auth_google.py       ← 【新】Google OAuth flow（基于 google-auth-oauthlib）
│   ├── auth_jwt.py          ← 【新】JWT 签发/校验（基于 PyJWT）
│   └── notify_orchestrator.py  ← 【新】"按 username 发飞书" 统一封装
├── api/
│   ├── auth.py              ← 【新】4 个 endpoint
│   └── users.py             ← 改造：SSO 开启时 /login 410 Gone
└── db/
    └── database.py          ← UserRecord 不动；helper 加 update_user_feishu_email
```

### 5.3 新增 API

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET`  | `/api/auth/google/login`    | 生成 state（HMAC 签 next + nonce）→ 302 跳 Google |
| `GET`  | `/api/auth/google/callback` | 验 state → 换 token → 验 id_token → 域名校验 → 写/查 User → 签 JWT → Set-Cookie → 302 回 next |
| `GET`  | `/api/auth/me`              | 返回 `{username, email, role, feishu_email}`；未登录 401 |
| `POST` | `/api/auth/logout`          | 清 Cookie，返回 204 |

### 5.4 依赖

```
google-auth-oauthlib==1.x
PyJWT==2.x
```

### 5.5 AuthMiddleware 核心流程

```python
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # 1. 总开关关闭 → 直通
        if not settings.ENABLE_GMAIL_SSO:
            return await call_next(request)

        # 2. 豁免路径直通
        if self._is_exempt(request.url.path):
            return await call_next(request)

        # 3. 解 Cookie
        token = request.cookies.get(settings.SSO_COOKIE_NAME)
        if not token:
            return JSONResponse({"detail": "unauthenticated"}, status_code=401)

        # 4. 验 JWT
        try:
            payload = jwt_verify(token, settings.SSO_JWT_SECRET)
        except JWTError:
            return JSONResponse({"detail": "invalid_token"}, status_code=401)

        # 5. 注入 user
        request.state.user = {
            "username": payload["username"],
            "email": payload["email"],
            "role": payload["role"],
        }
        return await call_next(request)
```

### 5.6 JWT Claim 结构

```json
{
  "username": "sanato.zhang",
  "email": "sanato.zhang@plaud.ai",
  "role": "admin",
  "iat": 1715000000,
  "exp": 1746536000        // iat + 365d
}
```

### 5.7 SSO 回调核心逻辑（伪代码）

```python
async def google_callback(code, state):
    next_url = verify_state(state)         # state 是 HMAC(next + nonce)，验签同时解出 next
    id_token_payload = oauth_flow.fetch_token(code).id_token
    email = id_token_payload["email"]

    domain = email.split("@", 1)[1]
    if domain not in settings.SSO_ALLOWED_DOMAINS:
        return redirect("/login?error=domain_not_allowed")

    username = derive_username(email)              # email.split("@")[0] 规范化
    user = await db.get_user(username)
    is_env_admin = email in settings.ADMIN_EMAILS

    if user:
        # 老 username 命中 → 更新 feishu_email，role 只升不降
        final_role = "admin" if (is_env_admin or user["role"] == "admin") else user["role"]
        await db.update_user(username, feishu_email=email, role=final_role)
    else:
        final_role = "admin" if is_env_admin else "user"
        await db.create_user(username=username, feishu_email=email, role=final_role)

    jwt_token = jwt_sign(username=username, email=email, role=final_role,
                          exp=now + timedelta(days=365))
    response = redirect(next_url or "/")
    response.set_cookie(name=settings.SSO_COOKIE_NAME, value=jwt_token,
                         httponly=True, secure=settings.SSO_COOKIE_SECURE,
                         samesite="lax", max_age=365*86400)
    return response
```

### 5.8 启动 fail-fast 校验

```python
if settings.ENABLE_GMAIL_SSO:
    assert settings.GOOGLE_CLIENT_ID
    assert settings.GOOGLE_CLIENT_SECRET
    assert settings.SSO_JWT_SECRET and len(settings.SSO_JWT_SECRET) >= 32
    assert settings.GOOGLE_REDIRECT_URI.startswith("https://")
    if not settings.ADMIN_EMAILS:
        logger.warning("ADMIN_EMAILS empty — no admin will be created")
```

---

## 6. 前端实现

### 6.1 模块拓扑

```
frontend/src/
├── app/
│   ├── login/
│   │   └── page.tsx              ← 【新】登录页
│   └── layout.tsx                ← 改：包 <AuthProvider><AuthGate>
├── components/
│   ├── AuthProvider.tsx          ← 【新】Context
│   ├── AuthGate.tsx              ← 【新】路由守卫
│   └── Sidebar.tsx               ← 改：底部显示用户 + 登出
└── lib/
    ├── api.ts                    ← 改：credentials: 'include' + 401 兜底
    └── auth.ts                   ← 【新】useAuth hook
```

### 6.2 AuthProvider 状态机

```
loading ──GET /api/auth/me──▶ authed     → 渲染应用
   │   401                       ▲
   ▼                              │
anonymous ────────────────────────┘ 登录成功后
   │
   └─▶ AuthGate 跳 /login?next=当前路径
```

### 6.3 登录页 `/login/page.tsx`

```
┌─────────────────────────────────────┐
│          🅰 Apollo                  │
│      Jarvis Ticket Platform         │
│                                     │
│   [G] Sign in with Google           │
│                                     │
│   仅限 @plaud.ai 邮箱                │
└─────────────────────────────────────┘
```

点击 → `window.location = "/api/auth/google/login?next=" + encodeURIComponent(原路径)`。

错误码 query 提示：
- `domain_not_allowed` → "请使用 @plaud.ai 邮箱登录"
- `invalid_state` → "登录会话已过期，请重新登录"
- `oauth_failed` → "Google 登录失败，请重试"
- `google_unavailable` → "Google 服务暂时不可用"

### 6.4 AuthGate 路由守卫

```tsx
<AuthProvider>
  <AuthGate>
    <div className="flex h-screen">
      <Sidebar />
      <main>{children}</main>
    </div>
  </AuthGate>
</AuthProvider>
```

- `loading` → 骨架屏
- `anonymous` 且非 `/login` → `router.replace('/login?next=' + 当前路径)`
- `anonymous` 在 `/login` → 渲染登录页
- `authed` → 渲染 children

### 6.5 `api.ts` 改造

```ts
export const request = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (res.status === 401) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.href = `/login?next=${next}`;
    throw new Error('unauthenticated');
  }
  if (!res.ok) throw new Error(...);
  return res.json();
};
```

### 6.6 useAuth fallback（兼容老机器）

```ts
export function useAuthInit() {
  // 优先调 /api/auth/me；404 时 fallback 读 localStorage
  // 这样一份前端代码同时兼容 SSO 开/关两种后端
}
```

### 6.7 localStorage 命运

| Key | 处理 |
|---|---|
| `appllo_username` | SSO 启用后由 useAuth 屏蔽；SSO 关闭时仍读 |
| `appllo_role` | 同上 |
| `appllo_feishu_email` | 同上 |

> 兼容期保留 localStorage 老路径作 fallback；全量切换 SSO 后未来 PR 清理。

### 6.8 Sidebar 用户区块

```
┌─────────────────────┐
│  ... 现有菜单 ...   │
│ ─────────────────── │
│ 👤 sanato.zhang     │
│ @plaud.ai           │
│ [登出]              │
└─────────────────────┘
```

---

## 7. 数据流

### 7.1 首次登录

```
浏览器 → /                                401（中间件）
浏览器 ← AuthGate redirect → /login
浏览器 → 点 Google 按钮
浏览器 → GET /api/auth/google/login?next=/
浏览器 ← 302 → accounts.google.com/o/oauth2/v2/auth?...
浏览器 ← Google 授权 → 302 → /api/auth/google/callback?code=xxx
后端：换 token → 验 id_token → 域名校验 → 写/查 user → 签 JWT
浏览器 ← Set-Cookie jarvis_session → 302 → / (next)
浏览器 → / (带 Cookie) → 200
```

### 7.2 稳态 API 调用

```
浏览器 → GET /api/issues (Cookie)
后端：中间件解 JWT → request.state.user 注入 → 路由处理 → 200
```

### 7.3 飞书发消息

```python
# app/services/notify_orchestrator.py
async def notify_users_by_username(usernames: List[str], message: dict) -> NotifyResult:
    sent, skipped, failed = [], [], []
    for username in usernames:
        user = await db.get_user(username)
        if not user:
            skipped.append((username, "user_not_found"))
            continue
        if not user.get("feishu_email"):
            skipped.append((username, "no_feishu_email"))
            continue
        try:
            await feishu_cli.send_message(email=user["feishu_email"], **message)
            sent.append(username)
        except Exception as e:
            failed.append((username, str(e)))
    return NotifyResult(sent=sent, skipped=skipped, failed=failed)
```

存量飞书发送点（`escalation_reminder.py`, `notify.py`, `oncall.py` 等）改造为走此封装。oncall 的"按 email 列表批量发"路径保留底层 API。

### 7.4 登出

```
浏览器 → POST /api/auth/logout (Cookie)
后端：Set-Cookie jarvis_session=; Max-Age=0
浏览器 ← 204
浏览器 → router.replace('/login')
```

### 7.5 老机器（SSO 关闭）

```
浏览器 → GET /api/auth/me
后端：路由未注册 → 404
浏览器：useAuth fallback → 读 localStorage → 调 /api/users/login（兼容路径）
```

### 7.6 机器调用

```
Linear → POST /api/linear/webhook (X-Webhook-Sig)
中间件：路径在 EXEMPT_PATHS → 直通
路由内：自行校验 webhook secret

外部脚本 → POST /api/v1/analyze (Bearer K)
中间件：路径在 EXEMPT_PATHS → 直通
路由内：_check_api_key
```

---

## 8. 错误处理 & 边界场景

### 8.1 SSO 流程错误

| 场景 | 处理 | 用户看到 |
|---|---|---|
| 非 @plaud.ai 邮箱 | 302 → `/login?error=domain_not_allowed` | "请使用 @plaud.ai 邮箱登录" |
| state 缺失/不匹配 | 302 → `/login?error=invalid_state` | "登录会话已过期，请重新登录" |
| Google token 换取失败 | 重试 1 次（指数退避），仍失败 → `/login?error=oauth_failed` | "Google 登录失败，请重试" |
| id_token 验签失败 | 同上 + 高优先级日志 | 同上 |
| Google 后端 5xx | 重试 1 次，失败 → `/login?error=google_unavailable` | "Google 服务暂时不可用" |
| ADMIN_EMAILS 为空 | 启动日志 WARNING | 无影响 |

### 8.2 Session 错误

| 场景 | 处理 |
|---|---|
| JWT 签名错 / 过期 / Cookie 缺失 | 中间件 401 → 前端跳 `/login?next=...` |
| JWT 解出 username 在 DB 不存在 | 401 + 日志 "user_deleted_but_token_valid" |
| `ENABLE_GMAIL_SSO` 运行时切换 | 已签 Cookie 静默失效，**用户需重新登录**（接受） |

### 8.3 数据一致性

| 场景 | 处理 |
|---|---|
| 同 username 被新 email 登录 | 覆盖 feishu_email，日志记录变更 |
| 老 admin 邮箱不在 ADMIN_EMAILS | 保留 admin role（只升不降） |
| ADMIN_EMAILS 里的用户首次 SSO | 升级为 admin |

### 8.4 部署 / 启动校验

`ENABLE_GMAIL_SSO=true` 时缺任一关键 env → 应用拒绝启动。

### 8.5 跨标签页 / 多设备

- 同用户多标签页：Cookie 共享，全部已登录
- 同用户多设备：互不影响，无踢下线
- 一标签页登出：只清当前浏览器

---

## 9. 测试策略

### 9.1 后端单元测试

**`tests/auth/test_google_oauth.py`**
- `/api/auth/google/login` 返回 302 + state Cookie
- callback state mismatch → 重定向带 `error=invalid_state`
- callback 域名非 plaud.ai → `error=domain_not_allowed`
- callback 首次登录 → 新建 user + 签 JWT + Set-Cookie
- callback 老 username → 覆盖 feishu_email
- ADMIN_EMAILS 命中 → role=admin
- 老 admin 不在 ADMIN_EMAILS → 保留 admin

**`tests/auth/test_middleware.py`**
- SSO 关闭 → 直通
- 豁免路径 → 直通
- 无 Cookie / 过期 / 签名错 → 401
- 有效 JWT → 注入 user
- JWT username DB 不存在 → 401

**`tests/auth/test_jwt.py`**
- 签发/校验闭环
- 不同密钥验失败
- exp = iat + 365d

**`tests/auth/test_startup_validation.py`**
- 缺 GOOGLE_CLIENT_ID → 启动失败
- JWT_SECRET < 32 字节 → 启动失败
- redirect_uri 非 https → 启动失败

### 9.2 后端集成测试

**`tests/integration/test_auth_flow.py`**
- Mock Google → callback → `/api/auth/me` 正确返回
- 登出后 `/api/auth/me` → 401

**`tests/integration/test_notify_orchestrator.py`**
- 全部有 feishu_email → 全发
- 部分无 → skipped + 日志
- user 不存在 → skipped
- feishu_cli 抛异常 → failed

### 9.3 前端手动验证

- [ ] SSO 关闭：老路径正常
- [ ] SSO 开启：首次访问跳 `/login`
- [ ] Google 登录 → 回原页面（next 生效）
- [ ] 非 plaud.ai 账号 → 错误提示
- [ ] Sidebar 显示邮箱
- [ ] 登出 → Cookie 清 + 跳 `/login`
- [ ] 关浏览器再开 → Cookie 仍在
- [ ] SSE/fetch 带 Cookie 正常

### 9.4 部署灰度

```
阶段 1（PR 合并后）：仅新机器开启 ENABLE_GMAIL_SSO=true，观察一周
阶段 2：老机器逐台切换，切换瞬间用户重新登录
阶段 3：稳定后清理 localStorage fallback + /api/users/login 旧路径
```

### 9.5 日志埋点

| 事件 | level | 字段 |
|---|---|---|
| sso_login_success | INFO | username, email, role |
| sso_login_rejected_domain | WARN | email |
| sso_oauth_network_error | ERROR | err |
| auth_rejected | DEBUG | path, reason |
| feishu_email_changed | INFO | username, old, new |
| promote_to_admin | WARN | username, via=env_whitelist |
| notify_skipped | INFO | username, reason |

---

## 10. 实施清单（高层）

| 阶段 | 内容 |
|---|---|
| ① 后端 auth 基础设施 | config / middleware / jwt / google oauth service |
| ② 后端 auth API | `/api/auth/*` 4 个 endpoint |
| ③ 后端 startup 校验 + EXEMPT_PATHS 接入 main.py | |
| ④ 飞书发送统一封装 | `notify_orchestrator.py` + 替换存量调用点 |
| ⑤ 前端 AuthProvider + AuthGate + /login 页 | |
| ⑥ 前端 api.ts credentials + 401 兜底 | |
| ⑦ 前端 useAuth 改造各页面 + Sidebar 用户区块 | |
| ⑧ 测试覆盖 | 9.1 / 9.2 全集 |
| ⑨ Google Cloud Console 配置 | 创建 OAuth 凭据 + 配置回调 URI |
| ⑩ 灰度部署 | 新机器先开，观察一周 |

详细步骤拆解 → 进入 writing-plans 阶段。

---

## 11. 风险

| 风险 | 缓解 |
|---|---|
| Google Cloud Console OAuth 配置错（redirect_uri 不匹配等） | 部署前 dev 环境完整跑通一次 |
| `SSO_JWT_SECRET` 泄露 | 用 env 注入，不入库；定期轮换需要全员重登 |
| 老机器升级时用户体验中断（需重新登录） | 提前通知 + 选低峰期切换 |
| 飞书发送统一封装漏改某个调用点 | grep 全量 `feishu_cli.send_message` 调用点，逐个迁移 + 测试覆盖 |
| `ADMIN_EMAILS` 配置遗漏导致首位用户无法管理 | 启动 WARNING + 文档明确部署 checklist |
