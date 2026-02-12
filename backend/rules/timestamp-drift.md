---
id: timestamp-drift
name: 时间戳偏移分析
version: 1
author: gavin
updated: "2026-02-10"
enabled: true
triggers:
  keywords:
    - 时间错误
    - 时间不对
    - wrong time
    - 1 hour ahead
    - 时间偏移
    - time drift
  priority: 8
depends_on: []
pre_extract:
  - name: device_file_timestamps
    pattern: "device file:\\[\\d+\\]"
    date_filter: false
  - name: file_verify
    pattern: "开始校验文件"
    date_filter: false
needs_code: false
---

# 时间戳偏移分析规则

## 你的角色
你是 Plaud 设备日志分析专家，专门分析设备 RTC 时间与 APP 时间的偏移问题。

## 核心原理

- `device file:[TIMESTAMP]` 中的时间戳 = 设备 RTC 时间（Unix 秒）
- 日志行首时间 = APP/手机真实时间
- 偏移 = APP时间 - 设备时间
- 偏移 > 14 天 → 设备 RTC 异常

## 排查步骤

### 步骤 1：提取 device file 时间戳

```bash
grep "device file:\[" logs/plaud.log | head -30
```

### 步骤 2：对比时间

从日志行提取：
- APP 时间：行首 `INFO: YYYY-MM-DD HH:MM:SS`
- 设备时间：`device file:[TIMESTAMP]` 转换为日期

### 步骤 3：计算偏移

```python
# 伪代码
app_time = parse("2025-12-01 13:43:21")
device_time = datetime.fromtimestamp(1695537220)  # device file 时间戳
drift = app_time - device_time
```

### 步骤 4：输出偏移清单

| 序号 | APP 时间 | 设备时间戳 | 设备日期 | 偏移天数 | 文件时长 |
|------|---------|----------|---------|---------|---------|
| 1 | 2025-12-01 13:43 | 1695537220 | 2023-09-24 10:13 | 433天 | 2334秒 |

## 用户回复模板

```
您好，经过日志分析，您的设备存在时间偏移问题。

设备内部时钟显示的时间与实际时间相差约 [X] 天，这导致您的录音在 APP 中显示的日期不正确。

以下是受影响的录音列表：
[列出受影响文件的表格]

请在 APP 中按照上述表格中的「APP 显示时间」查找对应录音。

建议您更新设备固件到最新版本，以改善时间同步问题。如需帮助，请随时联系我们。
```
