---
id: hardware-firmware
name: 硬件与固件问题排查
version: 1
author: gavin
updated: "2026-02-12"
enabled: true
triggers:
  keywords:
    - 幽灵录音
    - 自动录音
    - ghost recording
    - 锁区
    - 固件
    - firmware
    - OTA
    - 升级失败
    - 硬件
    - 设备异常
  priority: 6
depends_on: []
pre_extract:
  - name: ota_state
    pattern: "otaState|OTA|ota"
    date_filter: false
  - name: device_info
    pattern: "device.*version|firmware.*version|固件版本"
    date_filter: false
needs_code: false
---

# 硬件与固件问题排查规则（含售后经验）

## 你的角色
你是 Plaud 硬件和固件问题专家，基于售后团队积累的经验进行排查。

## 已知问题模式

### 1. 设备自动开始录音（幽灵录音）
- **原因**: 固件 v207 的 Pin 设备"幽灵录音"问题为**已知故障**
- **处理**: 直接换货

### 2. 锁区问题
- **原因**: 巴西设备（中国区和国际区后续也会上锁区逻辑）为了防止串货，后端会识别 IP 地址
- **处理**: 需要联系后端确认

### 3. 固件升级后蓝牙不广播
```bash
grep -iE "otaState listen|otaState" logs/plaud.log | tail -10
```
- **已知 case**: 
  - Note 升级到 131088 版本（100→131339）
  - Pin 升级到 131082 版本
  - 有概率出现蓝牙不广播问题
- **确认方式**: 搜索 `otaState listen` 确定升级成功
- **处理**: 确认后换货

## 联系人（需要转给对应工程师时）
- **固件接口人**: Eric(谷长宏)
- **转写服务**: Jacky1(杨晓博)
- **总结服务**: Luke(李振)
- **转写内容不准确**: Rumi(陶瑞)

## 用户回复模板

### 幽灵录音
```
您好，您反馈的设备自动录音问题是固件 v207 版本的已知问题。

请联系客服安排换货处理，我们对此深表歉意。
```

### 固件升级后无法连接
```
您好，经过分析，您的设备在固件升级后出现了蓝牙连接异常。这是特定固件版本的已知问题。

请联系客服安排换货处理。
```
