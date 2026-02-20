# Jarvis API Documentation

> Base URL: `http://<host>:8000`
>
> Interactive docs: `http://<host>:8000/docs` (Swagger UI)

---

## Table of Contents

1. [Authentication](#authentication)
2. [Core API — AI Analysis (v1)](#core-api--ai-analysis-v1)
3. [Feedback Submission](#feedback-submission)
4. [Task Management](#task-management)
5. [Ticket Tracking](#ticket-tracking)
6. [Analysis Rules](#analysis-rules)
7. [Linear Integration (Webhook)](#linear-integration-webhook)
8. [System & Health](#system--health)
9. [Data Types Reference](#data-types-reference)
10. [Error Handling](#error-handling)
11. [Examples](#examples)

---

## Authentication

Public API endpoints (`/api/v1/*`) support **Bearer token** authentication.

Set `JARVIS_API_KEY` in the server `.env` file. If not set, API access is open (no auth required).

```
Authorization: Bearer <your-api-key>
```

Internal endpoints (`/api/tasks`, `/api/feedback`, etc.) do not require authentication by default.

---

## Core API — AI Analysis (v1)

The primary external API. Submit a problem description + log files, get back an AI-powered analysis.

### Submit Analysis

```
POST /api/v1/analyze
Content-Type: multipart/form-data
Authorization: Bearer <api_key>
```

| Field | Type | Required | Description |
|---|---|---|---|
| `description` | string | **Yes** | Problem description in any language |
| `device_sn` | string | No | Device serial number |
| `priority` | string | No | `H` (high) or `L` (low), default `L` |
| `webhook_url` | string | No | URL to receive POST callback when analysis completes |
| `log_files` | file[] | No | One or more log files (`.plaud`, `.log`, `.zip`, `.gz`) |

**Response** (`200 OK`):

```json
{
  "task_id": "api_a1b2c3d4e5f6",
  "status": "processing",
  "message": "Analysis started. Poll GET /api/v1/analyze/api_a1b2c3d4e5f6 for result."
}
```

### Poll Result

```
GET /api/v1/analyze/{task_id}
Authorization: Bearer <api_key>
```

**Response** (`200 OK`):

```json
{
  "task_id": "api_a1b2c3d4e5f6",
  "status": "done",
  "problem_type": "蓝牙连接异常",
  "root_cause": "S3 上传链路异常（Connection reset by peer），非蓝牙 Token/绑定类问题",
  "confidence": "high",
  "key_evidence": [
    "2026-02-19 14:26:40 ERROR S3Upload: Connection reset by peer",
    "2026-02-19 14:26:41 WARN BleTransfer: retry count exceeded"
  ],
  "user_reply": "Your device's file upload encountered a network interruption...",
  "needs_engineer": false,
  "rule_type": "bluetooth",
  "agent_type": "codex",
  "created_at": "2026-02-19T14:28:40Z",
  "error": ""
}
```

| `status` | Meaning |
|---|---|
| `processing` | Analysis is running |
| `done` | Completed successfully |
| `failed` | Analysis failed (see `error` field) |

### Webhook Callback

If `webhook_url` is provided during submission, Jarvis will POST the result to that URL when analysis finishes:

```json
{
  "task_id": "api_a1b2c3d4e5f6",
  "status": "done",
  "result": {
    "problem_type": "蓝牙连接异常",
    "root_cause": "...",
    "confidence": "high",
    "key_evidence": ["..."],
    "user_reply": "...",
    "needs_engineer": false,
    "rule_type": "bluetooth",
    "agent_type": "codex"
  }
}
```

---

## Feedback Submission

Submit a ticket with full metadata + file uploads. Immediately triggers AI analysis.

### Submit Feedback

```
POST /api/feedback
Content-Type: multipart/form-data
```

| Field | Type | Required | Description |
|---|---|---|---|
| `description` | string | **Yes** | Problem description |
| `category` | string | No | Problem category |
| `device_sn` | string | No | Device SN |
| `firmware` | string | No | Firmware version |
| `app_version` | string | No | App version |
| `platform` | string | No | `APP` / `Web` / `Desktop` (default `APP`) |
| `priority` | string | No | `H` / `L` (default `L`) |
| `zendesk` | string | No | Zendesk ticket number or URL |
| `username` | string | No | Submitter's name |
| `log_files` | file[] | No | Log files (max 50MB each) |

**Response**:

```json
{
  "status": "ok",
  "record_id": "fb_a1b2c3d4e5",
  "task_id": "task_f6g7h8i9j0k1",
  "files_uploaded": 2,
  "message": "反馈已提交，AI 分析已启动"
}
```

### Import from Zendesk

Pre-fill a feedback form using Zendesk ticket data (AI-summarized).

```
POST /api/feedback/import-zendesk
Content-Type: multipart/form-data
```

| Field | Type | Required | Description |
|---|---|---|---|
| `zendesk_input` | string | **Yes** | Zendesk ticket number or URL |

**Response**: Returns AI-extracted form fields (`description`, `category`, `priority`, `device_sn`, etc.)

---

## Task Management

### Create Task (for existing issues)

```
POST /api/tasks
Content-Type: application/json
```

```json
{
  "issue_id": "recXXXXXXXX",
  "agent_type": "codex",
  "username": "gavin"
}
```

`agent_type` is optional. Allowed values: `claude_code`, `codex`.

**Response**: `TaskProgress` object (see Data Types).

### Batch Analyze

```
POST /api/tasks/batch
Content-Type: application/json
```

```json
{
  "issue_ids": ["rec001", "rec002", "rec003"],
  "agent_type": "codex"
}
```

**Response**: Array of `TaskProgress` objects.

### Get Task Status

```
GET /api/tasks/{task_id}
```

**Response**: `TaskProgress` object.

### Get Task Result

```
GET /api/tasks/{task_id}/result
```

**Response**: Full `AnalysisResult` object.

### Stream Progress (SSE)

```
GET /api/tasks/{task_id}/stream
Accept: text/event-stream
```

Real-time Server-Sent Events stream. Each event is a JSON `TaskProgress` object.

```
data: {"task_id":"task_abc","status":"analyzing","progress":50,"message":"AI 分析中..."}

data: {"task_id":"task_abc","status":"done","progress":100,"message":"分析完成"}
```

### List Recent Tasks

```
GET /api/tasks?limit=50
```

---

## Ticket Tracking

### In-Progress Issues

```
GET /api/local/in-progress?page=1&page_size=20
```

### Completed Issues (success + failed)

```
GET /api/local/completed?page=1&page_size=20
```

### Multi-Filter Tracking

```
GET /api/local/tracking?page=1&page_size=20&created_by=gavin&platform=APP&status=done&date_from=2026-02-01&date_to=2026-02-19
```

| Param | Description |
|---|---|
| `created_by` | Filter by submitter |
| `platform` | `APP` / `Web` / `Desktop` |
| `category` | Problem category |
| `status` | `analyzing` / `done` / `failed` |
| `date_from` | Start date (YYYY-MM-DD) |
| `date_to` | End date (YYYY-MM-DD) |

### Delete Issue (soft-delete)

```
DELETE /api/local/{issue_id}
```

### Escalate to Engineer

```
POST /api/local/{issue_id}/escalate
Content-Type: application/json
```

```json
{
  "reason": "AI analysis inconclusive, needs manual review"
}
```

Sends a Feishu notification to the current on-call engineer.

---

## Analysis Rules

### List Rules

```
GET /api/rules
```

### Get Rule

```
GET /api/rules/{rule_id}
```

### Create Rule

```
POST /api/rules
Content-Type: application/json
```

```json
{
  "id": "my_custom_rule",
  "name": "Custom Rule",
  "triggers": {
    "keywords": ["keyword1", "keyword2"],
    "priority": 5
  },
  "content": "# Analysis instructions\n\nWhen you see keyword1...",
  "depends_on": [],
  "pre_extract": [
    {"name": "error_logs", "pattern": "ERROR.*"}
  ],
  "needs_code": false
}
```

### Update Rule

```
PUT /api/rules/{rule_id}
Content-Type: application/json
```

Partial update — only include fields to change:

```json
{
  "enabled": false,
  "content": "Updated analysis instructions..."
}
```

### Delete Rule

```
DELETE /api/rules/{rule_id}
```

### Reload Rules (from files)

```
POST /api/rules/reload
```

---

## Linear Integration (Webhook)

Jarvis integrates with [Linear](https://linear.app) via webhooks. When a comment containing `@ai-agent` is posted on a Linear issue, Jarvis automatically:

1. Fetches the issue details + attachments
2. Runs the AI analysis pipeline
3. Posts the result back as a comment

### Setup

1. In Linear: **Settings → API → Webhooks → Create webhook**
2. URL: `https://<your-domain>/api/linear/webhook`
3. Events: `Comment` (create)
4. Set `LINEAR_API_KEY` and optionally `LINEAR_WEBHOOK_SECRET` in `.env`

### Webhook Endpoint

```
POST /api/linear/webhook
```

This endpoint is called by Linear automatically. No manual invocation needed.

---

## System & Health

### Health Check

```
GET /api/health
```

Returns system status including database, Redis, agent availability, and loaded rules.

```json
{
  "status": "healthy",
  "service": "jarvis",
  "checks": {
    "database": {"status": "ok"},
    "redis": {"status": "unavailable", "note": "Fallback to in-process tasks"},
    "agents": {
      "claude_code": {"status": "ok", "available": true, "version": "..."},
      "codex": {"status": "ok", "available": true, "version": "..."}
    },
    "rules": {"status": "ok", "count": 10}
  }
}
```

### Check Agent Availability

```
GET /api/health/agents
```

---

## Data Types Reference

### TaskProgress

```typescript
{
  task_id: string;
  issue_id: string;
  status: "queued" | "downloading" | "decrypting" | "extracting" | "analyzing" | "done" | "failed";
  progress: number;       // 0-100
  message: string;
  error?: string;
  created_at: string;     // ISO 8601 UTC
  updated_at: string;
}
```

### AnalysisResult

```typescript
{
  task_id: string;
  issue_id: string;
  problem_type: string;       // e.g. "蓝牙连接异常"
  problem_type_en: string;    // e.g. "Bluetooth Connection Error"
  root_cause: string;         // Detailed root cause (Chinese)
  root_cause_en: string;      // Detailed root cause (English)
  confidence: "high" | "medium" | "low";
  confidence_reason: string;
  key_evidence: string[];     // Key log lines that support the diagnosis
  user_reply: string;         // Suggested reply to the user (Chinese)
  user_reply_en: string;      // Suggested reply to the user (English)
  needs_engineer: boolean;    // Whether manual engineering review is needed
  fix_suggestion: string;
  rule_type: string;          // Which analysis rule was matched
  agent_type: string;         // Which AI agent was used (codex / claude_code)
}
```

### Issue

```typescript
{
  record_id: string;
  description: string;
  device_sn: string;
  firmware: string;
  app_version: string;
  priority: "H" | "L";
  zendesk: string;            // Full Zendesk URL
  zendesk_id: string;         // e.g. "#378794"
  source: "feishu" | "linear" | "api" | "local";
  feishu_link: string;
  platform: string;           // APP / Web / Desktop
  category: string;
  created_by: string;
  created_at: string;         // ISO 8601 UTC
}
```

---

## Error Handling

All endpoints return standard HTTP status codes:

| Code | Meaning |
|---|---|
| `200` | Success |
| `400` | Bad request (missing required fields) |
| `401` | Missing API key |
| `403` | Invalid API key |
| `404` | Resource not found |
| `500` | Internal server error |

Error response format:

```json
{
  "detail": "Human-readable error message"
}
```

---

## Examples

### Example 1: Quick Analysis via cURL

```bash
# Submit
curl -X POST http://localhost:8000/api/v1/analyze \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -F "description=用户反馈录音文件丢失，设备 SN 8801030171711129" \
  -F "device_sn=8801030171711129" \
  -F "priority=H" \
  -F "log_files=@/path/to/user_log.plaud"

# Response: {"task_id": "api_abc123", "status": "processing", ...}

# Poll (repeat until status is "done" or "failed")
curl http://localhost:8000/api/v1/analyze/api_abc123 \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### Example 2: Analysis with Webhook Callback

```bash
curl -X POST http://localhost:8000/api/v1/analyze \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -F "description=蓝牙连接断开后无法重连" \
  -F "webhook_url=https://your-server.com/callback" \
  -F "log_files=@debug.log"

# Jarvis will POST to https://your-server.com/callback when done
```

### Example 3: Python Client

```python
import httpx
import time

API_URL = "http://localhost:8000"
API_KEY = "your-api-key"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# Submit
with open("user_log.plaud", "rb") as f:
    resp = httpx.post(
        f"{API_URL}/api/v1/analyze",
        headers=HEADERS,
        data={
            "description": "录音文件丢失，设备SN: 8801030171711129",
            "device_sn": "8801030171711129",
            "priority": "H",
        },
        files={"log_files": ("user_log.plaud", f)},
    )
task_id = resp.json()["task_id"]

# Poll for result
while True:
    result = httpx.get(
        f"{API_URL}/api/v1/analyze/{task_id}",
        headers=HEADERS,
    ).json()

    if result["status"] == "done":
        print(f"Problem: {result['problem_type']}")
        print(f"Root Cause: {result['root_cause']}")
        print(f"Confidence: {result['confidence']}")
        print(f"User Reply: {result['user_reply']}")
        break
    elif result["status"] == "failed":
        print(f"Failed: {result['error']}")
        break

    time.sleep(5)
```

### Example 4: Feedback Submission (with file upload)

```bash
curl -X POST http://localhost:8000/api/feedback \
  -F "description=用户无法完成转写，一直卡在 processing 状态" \
  -F "category=文件管理（转写，总结，文件编辑，分享导出，更多菜单，ASK Plaud，PCS）" \
  -F "platform=APP" \
  -F "device_sn=8801030171711129" \
  -F "firmware=2.1.0" \
  -F "app_version=3.5.2" \
  -F "priority=H" \
  -F "zendesk=378794" \
  -F "username=gavin" \
  -F "log_files=@app.log" \
  -F "log_files=@device.plaud"
```

### Example 5: SSE Progress Monitoring (JavaScript)

```javascript
const eventSource = new EventSource("/api/tasks/task_abc123/stream");

eventSource.onmessage = (event) => {
  const progress = JSON.parse(event.data);
  console.log(`[${progress.status}] ${progress.progress}% - ${progress.message}`);

  if (progress.status === "done" || progress.status === "failed") {
    eventSource.close();
  }
};
```
