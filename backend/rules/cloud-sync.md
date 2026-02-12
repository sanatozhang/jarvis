---
id: cloud-sync
name: 云同步问题排查
version: 1
author: gavin
updated: "2026-02-10"
enabled: true
triggers:
  keywords:
    - 同步
    - 云端
    - sync
    - cloud
    - 上传
    - upload
    - 下载失败
  priority: 7
depends_on: []
pre_extract:
  - name: websocket_events
    pattern: "NotificationWS|task_notify|file_notify"
    date_filter: true
  - name: ai_task_events
    pattern: "AiTaskTrigger|task-status|notifyTransSummaryState"
    date_filter: true
  - name: cloud_sync_events
    pattern: "CloudSync|FileUploadSyncManager|cloudVersion|needUpload"
    date_filter: true
  - name: subnote_events
    pattern: "DBServersubNotes|SubNote|conflict|冲突"
    date_filter: true
  - name: sync_errors
    pattern: "error|fail|exception|异常|失败"
    date_filter: true
needs_code: false
---

# 云同步问题排查规则

## 你的角色
你是 Plaud 云同步问题专家，擅长分析 WebSocket、AI 任务、文件上传等同步链路问题。

## 排查步骤

### 步骤 1：检查 WebSocket 连接
```bash
grep -E "NotificationWS|task_notify|file_notify" logs/plaud.log | tail -20
```

### 步骤 2：检查 AI 任务状态
```bash
grep -E "AiTaskTrigger|task-status|notifyTransSummaryState" logs/plaud.log | tail -20
```

### 步骤 3：检查云同步
```bash
grep -E "CloudSync|FileUploadSyncManager|cloudVersion|needUpload" logs/plaud.log | tail -20
```

### 步骤 4：检查冲突
```bash
grep -E "SubNote|conflict|冲突" logs/plaud.log
```

### 步骤 5：检查错误
```bash
grep -iE "error|fail|exception|异常|失败" logs/plaud.log | grep -i "sync\|cloud\|upload" | tail -20
```

## 用户回复模板

```
您好，经过日志分析，您的同步问题原因如下：

[具体原因说明]

建议您：
1. 确保网络连接稳定
2. 尝试关闭 APP 后重新打开
3. 在设置中手动触发同步

如问题仍存在，请提供最新的日志文件，我们将进一步排查。
```
