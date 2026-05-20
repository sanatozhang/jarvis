# Feishu 开放平台 — Apollo SSO 配置

> 在开启 `ENABLE_SSO=true` 前必须完成此配置。复用现有 appllobot (`cli_a96b43d075f89cc0`)。

## 1. 进入应用管理

https://open.feishu.cn/app/cli_a96b43d075f89cc0/

## 2. 安全设置 → 重定向 URL

加两条：
- `http://localhost:8000/api/auth/feishu/callback`（本地 dev）
- `https://apollo.nicebuild.click/api/auth/feishu/callback`（生产）

## 3. 应用功能 → 网页应用

启用。桌面端 / 移动端主页填 `https://apollo.nicebuild.click`。

## 4. 权限管理 → API 权限

申请并开通：
- `contact:user.email:readonly`
- `contact:user.base:readonly`

## 5. 版本管理

创建新版本 → 上线（自建应用权限变更需要新版本生效）。

## 6. 拷贝凭据到 .env

应用 ID / Secret 在"凭证与基础信息"页：

```
ENABLE_SSO=true
SSO_FEISHU_APP_ID=cli_a96b43d075f89cc0
SSO_FEISHU_APP_SECRET=<...>
SSO_FEISHU_REDIRECT_URI=http://localhost:8000/api/auth/feishu/callback   # 本地
SSO_JWT_SECRET=<openssl rand -hex 32>
SSO_COOKIE_SECURE=false                       # localhost 无 HTTPS 必须 false
ADMIN_EMAILS=sanato.zhang@plaud.ai
```

## 常见错误

| 错误 | 原因 | 解决 |
|---|---|---|
| redirect_uri 不匹配 | 后台白名单与请求不一致 | 完整复制 URL，注意路径 |
| `email` 字段为空 | 用户没设企业邮箱 | 用 `email` 字段 fallback |
| 域名拒绝 | 非 plaud.ai 用户 | 此为正常行为 |
