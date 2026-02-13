---
id: membership-payment
name: 会员购买与支付问题排查
version: 1
author: gavin
updated: "2026-02-12"
enabled: true
triggers:
  keywords:
    - 会员
    - 购买
    - 支付
    - 扣款
    - 退款
    - 订阅
    - membership
    - payment
    - purchase
    - subscription
    - starter
    - pro
    - 未生效
    - 重复购买
    - 重复扣款
  priority: 7
depends_on: []
pre_extract:
  - name: making_purchase
    pattern: "makingPurchase"
    date_filter: false
  - name: purchase_update
    pattern: "_listenToPurchaseUpdated"
    date_filter: false
  - name: transaction
    pattern: "/user/me/transaction"
    date_filter: false
needs_code: false
---

# 会员购买与支付问题排查规则（含售后经验）

## 你的角色
你是 Plaud 会员和支付问题专家，基于售后团队积累的经验进行排查。

## 已知问题模式

### 1. 购买未生效
```bash
grep "makingPurchase" logs/plaud.log | tail -10
grep "_listenToPurchaseUpdated" logs/plaud.log | tail -10
grep "/user/me/transaction" logs/plaud.log | tail -10
```
- **原因**: 大部分是订单绑定到了最初购买的账号上
- **确认方式**: 让用户提供邮件订单 ID，给后端查
- **处理**: 引导用户登录正确邮箱
- **日志关键词**:
  - `makingPurchase`: 用户发起了购买
  - `_listenToPurchaseUpdated`: 系统返回了购买商品状态
  - `/user/me/transaction`: 向后台核销的接口

### 2. 重复购买/重复扣款
- **原因**: 
  1. Stripe 主体迁移出现过两个主体同时扣款问题（历史较少）
  2. 重复购买 Pro 时，因不同促销活动后台存在 3 个 Pro 商品，用户看到未刷新价格导致差异
- **确认方式**: 找后端确认
- **处理**: 发现后回复原因即可

### 3. 调价后价格不一致
- **原因**: 后端配置价格调整时的延迟
- **处理**: 告知用户刷新 APP 即可

## 用户回复模板

### 购买未生效
```
您好，经过日志分析，您的会员购买订单可能绑定到了最初注册的账号上。

请确认您当前登录的邮箱是否与购买时使用的邮箱一致。如果不确定，请提供您的订单 ID（可在邮件中找到），我们帮您查询具体绑定的账号。
```

### 账号显示 Starter
```
您好，您反馈的会员未生效问题，通常是因为购买的会员绑定在了另一个账号上。

请尝试：
1. 检查购买时使用的邮箱，确保当前登录的账号与之一致
2. 如果您使用了 SSO 登录，请确认是否使用了正确的 SSO 账号

如需帮助确认账号信息，请提供订单详情，我们将为您查询。
```
