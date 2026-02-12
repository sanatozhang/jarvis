---
id: flutter-crash
name: Flutter 崩溃排查
version: 1
author: gavin
updated: "2026-02-10"
enabled: true
triggers:
  keywords:
    - 灰屏
    - 白屏
    - 崩溃
    - crash
    - grey screen
    - blank screen
    - 闪退
    - 卡死
    - ANR
  priority: 9
depends_on: []
pre_extract:
  - name: flutter_errors
    pattern: "FlutterError"
    date_filter: false
  - name: runtime_errors
    pattern: "RangeError|TypeError|StateError|NoSuchMethodError"
    date_filter: false
  - name: platform_exceptions
    pattern: "PlatformException"
    date_filter: false
  - name: severe_logs
    pattern: "SEVERE:"
    date_filter: false
  - name: route_changes
    pattern: "GOING TO ROUTE|当前路由"
    date_filter: false
needs_code: true
---

# Flutter 崩溃排查规则

## 你的角色
你是 Flutter 应用崩溃问题专家。需要分析日志中的错误信息，定位崩溃原因。

## 排查步骤（按优先级）

### 步骤 1：搜索 FlutterError
```bash
grep "FlutterError" logs/plaud.log
```

### 步骤 2：搜索运行时错误
```bash
grep -E "RangeError|TypeError|StateError|NoSuchMethodError" logs/plaud.log
```

### 步骤 3：搜索平台异常
```bash
grep "PlatformException" logs/plaud.log
```

### 步骤 4：搜索严重日志
```bash
grep "SEVERE:" logs/plaud.log
```

### 步骤 5：定位路由（确定用户在哪个页面崩溃）
```bash
grep -E "GOING TO ROUTE|当前路由" logs/plaud.log | tail -10
```

### 步骤 6：关联分析
- 对比路由进入时间 vs 错误发生时间
- 确认因果关系

## 常见路由映射

| 用户描述 | 可能的路由 |
|---------|----------|
| 主页 | `/home` |
| 录音详情 | `/file-detail` |
| 设备页面 | `/device-detail` |
| 设置 | `/settings` |

## 代码搜索指引

如果 code/ 目录存在，可搜索：
```bash
# 查找错误相关的代码
grep -r "相关错误类" code/lib/
# 查找路由定义
grep -r "GetPage" code/lib/app/routes/
```

## 用户回复模板

```
您好，经过日志分析，APP [灰屏/崩溃] 是由于 [具体原因] 导致的。

目前工程团队已知晓此问题，建议您：
1. 更新 APP 到最新版本
2. 如问题复现，请尝试关闭 APP 后重新打开

我们会在后续版本中修复此问题。感谢您的反馈！
```
