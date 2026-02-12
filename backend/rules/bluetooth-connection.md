---
id: bluetooth
name: 蓝牙连接排查
version: 1
author: gavin
updated: "2026-02-10"
enabled: true
triggers:
  keywords:
    - 蓝牙
    - 连接
    - bluetooth
    - connect
    - ble
    - 配对
    - pair
    - disconnect
    - 断开
  priority: 8
depends_on: []
pre_extract:
  - name: scan_events
    pattern: "scan|搜索|discover|found"
    date_filter: true
  - name: connect_events
    pattern: "connect|连接"
    date_filter: true
  - name: auth_events
    pattern: "token|验证|auth"
    date_filter: true
  - name: ble_errors
    pattern: "ble.*error|bluetooth.*error|蓝牙.*错误"
    date_filter: false
  - name: disconnect_events
    pattern: "disconnect|断开"
    date_filter: true
needs_code: false
---

# 蓝牙连接排查规则

## 你的角色
你是 Plaud 设备蓝牙连接问题专家。

## 排查步骤

### 步骤 1：检查扫描/发现
```bash
grep -iE "scan|搜索|discover|found" logs/plaud.log | tail -20
```

### 步骤 2：检查连接事件
```bash
grep -iE "connect|连接" logs/plaud.log | tail -30
```

### 步骤 3：检查认证/Token
```bash
grep -iE "token|验证|auth" logs/plaud.log | tail -20
```

### 步骤 4：检查错误
```bash
grep -iE "ble.*error|bluetooth.*error|蓝牙.*错误" logs/plaud.log
```

## 常见问题模式

| 模式 | 日志特征 | 原因 | 建议 |
|------|---------|------|------|
| 搜索不到设备 | 无 scan result | 设备未开机/距离远 | 重启设备，靠近手机 |
| 连接超时 | connect timeout | 信号弱/干扰 | 清除配对记录重试 |
| 认证失败 | token invalid | 配对信息失效 | 删除设备重新配对 |
| 频繁断开 | disconnect 频繁出现 | 硬件/固件问题 | 更新固件 |

## 用户回复模板

### 连接超时
```
您好，经过日志分析，您的设备出现蓝牙连接超时的问题。建议您：

1. 确保设备已开机且电量充足
2. 将设备靠近手机（建议 1 米以内）
3. 在手机蓝牙设置中删除 Plaud 设备，然后在 APP 中重新配对

如问题仍存在，请尝试重启手机后再次操作。
```
