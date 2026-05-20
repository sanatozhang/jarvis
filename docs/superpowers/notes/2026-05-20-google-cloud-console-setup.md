# Google Cloud Console — Apollo SSO 凭据配置

> 在新机器开启 `ENABLE_GMAIL_SSO=true` 前必须完成此配置。

## 1. 进入 Google Cloud Console

https://console.cloud.google.com → 选择 plaud.ai 组织下的项目（无则新建：`apollo-sso`）。

## 2. 启用 OAuth API

- 左侧 → APIs & Services → Library
- 搜索 "Google+ API" 或 "People API"，启用

## 3. 配置 OAuth consent screen

- 左侧 → APIs & Services → OAuth consent screen
- User type 选 **Internal**（仅 plaud.ai 工作区员工可登录，这是硬域名限制的底层支撑）
- App name: `Apollo`
- User support email: `sanato.zhang@plaud.ai`
- Authorized domains 加 `plaud.ai`、`nicebuild.click`
- Scopes：勾 `openid`、`.../auth/userinfo.email`、`.../auth/userinfo.profile`

## 4. 创建 OAuth 2.0 Client ID

- APIs & Services → Credentials → Create Credentials → OAuth client ID
- Application type: **Web application**
- Name: `Apollo Web`
- Authorized redirect URIs:
  - `https://apollo.nicebuild.click/api/auth/google/callback`
  - （本地 dev）`http://localhost:8000/api/auth/google/callback`（仅开发用，生产不要加）

## 5. 拷贝凭据到 .env

```
GOOGLE_CLIENT_ID=<上一步生成>.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-<...>
GOOGLE_REDIRECT_URI=https://apollo.nicebuild.click/api/auth/google/callback
SSO_JWT_SECRET=<openssl rand -hex 32>
ENABLE_GMAIL_SSO=true
ADMIN_EMAILS=sanato.zhang@plaud.ai
```

## 6. 验证回调 URI

部署后访问 https://apollo.nicebuild.click → 点 Google 按钮 → Google 应直接跳回，不报 `redirect_uri_mismatch`。

## 7. 常见错误

| 错误 | 原因 | 解决 |
|---|---|---|
| `redirect_uri_mismatch` | URI 与 Console 中 Authorized redirect URIs 不一致 | 完整复制，注意 https 和路径末尾斜杠 |
| `unauthorized_client` | OAuth Client 被禁用 | 重新启用 Client，或重建一组凭据 |
| 域名错误 (`hd not allowed`) | 用了非 plaud.ai 账号 | 切换 Google 账号 |
| `invalid_client` | client_id/secret 错配 | 重新复制凭据到 .env，注意空格 |
