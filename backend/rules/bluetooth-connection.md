---
id: bluetooth
name: 蓝牙连接排查
version: 2
author: gavin
updated: "2026-02-12"
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
    - 搜索不到
    - 搜不到
    - NRF
    - token
    - tokennotmatch
    - wifi快传
    - fast transfer
    - FindMy
    - 蜂鸣
  priority: 8
depends_on: []
pre_extract:
  - name: scan_results
    pattern: "scanResults"
    date_filter: false
  - name: try_connect
    pattern: "tryconnect|tryConnect"
    date_filter: false
  - name: token_match
    pattern: "tokennotmatch|TokenNotMatch|token.*not.*match"
    date_filter: false
  - name: connect_state
    pattern: "DeviceConnectManager.connectState.*BleState"
    date_filter: false
  - name: device_bind
    pattern: "/device/bind-token|/device/bind"
    date_filter: false
  - name: ota_state
    pattern: "otaState listen|otaState"
    date_filter: false
  - name: wifi_close
    pattern: "wifiClose"
    date_filter: false
  - name: ble_errors
    pattern: "ble.*error|bluetooth.*error|蓝牙.*错误|connect.*fail"
    date_filter: false
  - name: disconnect_events
    pattern: "disconnect|断开"
    date_filter: true
needs_code: false
---

# 蓝牙连接排查规则（含售后经验）

## 你的角色
你是 Plaud 设备蓝牙连接问题专家，基于售后团队积累的经验进行排查。

## 已知问题模式（按发生频率排序）

### 1. 设备无响应/搜索不到
```bash
grep "scanResults" logs/plaud.log | tail -20
```
- **原因**: 设备问题，或 Android 系统搜索频率过高
- **确认方式**: 查看 scanResults 是否有返回
- **处理**: 让用户做 troubleshooting，做了还搜不到 → 换货

### 2. 搜索到设备但无法连接（TokenNotMatch）
```bash
grep -iE "tryconnect|tryConnect" logs/plaud.log | tail -20
grep -iE "tokennotmatch|TokenNotMatch" logs/plaud.log | tail -20
```
- **原因**: APP端解绑时 token 未清除，导致新用户连接时云端与设备 token 不匹配（**最常见**）
- **确认方式**: 日志中连接结果是否返回 tokennotmatch
- **处理**: 确认后换货

### 3. 提示被其他用户绑定
```bash
grep -E "/device/bind-token|/device/bind" logs/plaud.log | tail -10
```
- **原因**: 云端存在绑定，用户登错账号，SSO 账号弄错
- **确认方式**: 通过设备 SN 找后端查绑定记录
- **处理**: 引导用户登录正确账号
- **日志关键词**:
  - 离线绑定: `/device/bind-token` 接口
  - 正常云端绑定: `/device/bind` 接口

### 4. 连接超时（固件升级后）
```bash
grep -iE "otaState listen|otaState" logs/plaud.log | tail -10
grep "scanResults" logs/plaud.log | tail -10
```
- **已知 case**: Note 设备升级到 131088 版本（100→131339）、Pin 升级到 131082 版本有概率出现蓝牙不广播
- **确认方式**: 搜索 otaState listen 确定升级成功，再搜索 scanResults 查看搜索结果
- **处理**: 确认后换货

### 5. 异常断连（开启录音后立刻断连）
```bash
grep "DeviceConnectManager.connectState.*BleState" logs/plaud.log | tail -20
```
- **原因**: 设备本身问题（较少见）
- **确认方式**: 对比连接态和断连态之间，APP 是否有主动调用断开连接
- **处理**: 如果 APP 没有主动断开 → 换货；如果有 → APP 问题

### 6. NotePin 解除 FindMy 后持续蜂鸣
- **原因**: 大概率硬件问题
- **处理**: 确认后换货

### 7. WiFi 快传无法连接
```bash
grep "wifiClose" logs/plaud.log | tail -10
```
- **原因**: 大部分是 iOS 端，系统返回 -7 错误（日志显示 -1007）
- **处理**: 让用户尝试 troubleshooting，不行换货

### 8. WiFi 快传突然断开
- **原因**: 固件 WiFi 快传机制（Pro 和 Pin S1.1 版本以上），当电量=0、电压低于 3500 时会退出 WiFi 快传
- **确认方式**: 查看电量是否很低
- **处理**: 重启设备

## 用户回复模板

### TokenNotMatch（最常见）
```
您好，经过日志分析，您的设备出现了连接认证不匹配（Token Not Match）的问题。这是一个已知的硬件/固件问题。

建议您联系我们的客服团队安排换货处理。

如需进一步帮助，请随时联系我们。
```

### 搜索不到设备
```
您好，经过日志分析，APP 确实无法搜索到您的设备。建议您先尝试以下步骤：

1. 确保设备已开机且电量充足
2. 在手机蓝牙设置中忘记/删除 Plaud 设备
3. 重启手机和设备后重新搜索

如以上步骤无法解决，请联系客服安排换货处理。
```

### 被其他用户绑定
```
您好，经过日志分析，您的设备已被另一个账号绑定。这通常是因为使用了不同的登录方式。

请确认您使用的是最初注册的邮箱或 SSO 账号登录 APP。如果您不确定是哪个账号，请提供设备 SN，我们可以帮您查询。
```

### WiFi 快传问题
```
您好，WiFi 快传连接问题可能与系统兼容性有关。建议您尝试：

1. 重启设备和手机
2. 确保设备电量充足（电量过低会自动退出 WiFi 快传）
3. 如使用 iOS，请尝试在设置中重置网络设置

如问题仍存在，请联系客服处理。
```
