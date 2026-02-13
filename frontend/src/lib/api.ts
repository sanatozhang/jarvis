/**
 * API client for the Jarvis backend.
 */

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers || {});
  const isFormDataBody =
    typeof FormData !== "undefined" && init?.body instanceof FormData;
  if (!isFormDataBody && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json();
}

// ============================================================
// Types
// ============================================================

export interface Issue {
  record_id: string;
  description: string;
  device_sn: string;
  firmware: string;
  app_version: string;
  priority: string;
  zendesk: string;
  zendesk_id: string;
  feishu_link: string;
  feishu_status: "pending" | "in_progress" | "done";
  result_summary: string;
  root_cause_summary: string;
  created_at_ms: number;
  log_files: { name: string; token: string; size: number }[];
}

export interface IssueListResponse {
  generated_at: string;
  stats: Record<string, number>;
  issues: Issue[];
}

export interface TaskProgress {
  task_id: string;
  issue_id: string;
  status: string;
  progress: number;
  message: string;
  error?: string;
  created_at?: string;
  updated_at?: string;
}

export interface AnalysisResult {
  task_id: string;
  issue_id: string;
  problem_type: string;
  root_cause: string;
  confidence: string;
  confidence_reason: string;
  key_evidence: string[];
  core_logs: string[];
  code_locations: string[];
  user_reply: string;
  needs_engineer: boolean;
  requires_more_info: boolean;
  more_info_guidance: string;
  next_steps: string[];
  fix_suggestion: string;
  rule_type: string;
  agent_type: string;
  created_at?: string;
}

export interface RuleMeta {
  id: string;
  name: string;
  version: number;
  enabled: boolean;
  triggers: { keywords: string[]; priority: number };
  depends_on: string[];
  pre_extract: { name: string; pattern: string; date_filter: boolean }[];
  needs_code: boolean;
}

export interface Rule {
  meta: RuleMeta;
  content: string;
  file_path: string;
}

export interface HealthCheck {
  status: string;
  service: string;
  checks: Record<string, any>;
}

export interface DailyReport {
  date: string;
  total_issues: number;
  analyses: any[];
  category_stats: Record<string, number>;
  markdown: string;
}

export interface AgentConfig {
  default: string;
  timeout: number;
  max_turns: number;
  providers: Record<string, any>;
  routing: Record<string, string>;
}

// ============================================================
// Issues
// ============================================================

export interface PaginatedResponse<T> {
  issues: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  high_priority?: number;
}

export const fetchPendingIssues = (assignee?: string, page = 1, pageSize = 20) => {
  const params = new URLSearchParams();
  if (assignee) params.set("assignee", assignee);
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  return request<PaginatedResponse<Issue>>(`/issues?${params}`);
};
export const fetchIssue = (id: string) => request<Issue>(`/issues/${id}`);
export const refreshIssuesCache = () => request<{ status: string }>("/issues/refresh", { method: "POST" });

// ============================================================
// Tasks
// ============================================================

export const createTask = (issueId: string, agentType?: string, username?: string) =>
  request<TaskProgress>("/tasks", {
    method: "POST",
    body: JSON.stringify({ issue_id: issueId, agent_type: agentType || null, username: username || "" }),
  });

export const batchAnalyze = (issueIds: string[], agentType?: string) =>
  request<TaskProgress[]>("/tasks/batch", {
    method: "POST",
    body: JSON.stringify({ issue_ids: issueIds, agent_type: agentType || null }),
  });

export interface FeedbackUploadPayload {
  description: string;
  device_sn?: string;
  firmware?: string;
  app_version?: string;
  zendesk?: string;
  agent_type?: "codex" | "claude_code";
  files: File[];
}

export const createFeedbackTask = (payload: FeedbackUploadPayload) => {
  const form = new FormData();
  form.append("description", payload.description);
  form.append("device_sn", payload.device_sn || "");
  form.append("firmware", payload.firmware || "");
  form.append("app_version", payload.app_version || "");
  form.append("zendesk", payload.zendesk || "");
  form.append("agent_type", payload.agent_type || "");
  for (const file of payload.files) {
    form.append("files", file);
  }
  return request<TaskProgress>("/tasks/feedback", {
    method: "POST",
    body: form,
  });
};

export const fetchTaskStatus = (id: string) => request<TaskProgress>(`/tasks/${id}`);
export const fetchTaskResult = (id: string) => request<AnalysisResult>(`/tasks/${id}/result`);
export const fetchTasks = () => request<TaskProgress[]>("/tasks");

/**
 * Subscribe to task progress with SSE + polling fallback.
 * If SSE fails, automatically falls back to polling every 2s.
 */
export function subscribeTaskProgress(
  taskId: string,
  onMessage: (p: TaskProgress) => void,
): { stop: () => void } {
  let stopped = false;
  let es: EventSource | null = null;
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  // Attempt SSE first
  try {
    es = new EventSource(`${BASE}/tasks/${taskId}/stream`);
    es.onmessage = (e) => {
      try {
        const p = JSON.parse(e.data) as TaskProgress;
        onMessage(p);
        if (p.status === "done" || p.status === "failed") {
          stop();
        }
      } catch {}
    };
    es.onerror = () => {
      // SSE failed — close and fall back to polling
      es?.close();
      es = null;
      if (!stopped) startPolling();
    };
  } catch {
    // SSE not supported — fall back to polling
    startPolling();
  }

  function startPolling() {
    if (pollTimer || stopped) return;
    pollTimer = setInterval(async () => {
      if (stopped) { clearInterval(pollTimer!); return; }
      try {
        const p = await fetchTaskStatus(taskId);
        onMessage(p);
        if (p.status === "done" || p.status === "failed") {
          stop();
        }
      } catch {}
    }, 2000);
  }

  function stop() {
    stopped = true;
    es?.close();
    if (pollTimer) clearInterval(pollTimer);
  }

  return { stop };
}

// ============================================================
// Rules
// ============================================================

export const fetchRules = () => request<Rule[]>("/rules");
export const fetchRule = (id: string) => request<Rule>(`/rules/${id}`);
export const reloadRules = () => request<{ reloaded: number }>("/rules/reload", { method: "POST" });

export const createRule = (data: any) =>
  request<Rule>("/rules", { method: "POST", body: JSON.stringify(data) });

export const updateRule = (id: string, data: any) =>
  request<Rule>(`/rules/${id}`, { method: "PUT", body: JSON.stringify(data) });

export const deleteRule = (id: string) =>
  request<{ deleted: string }>(`/rules/${id}`, { method: "DELETE" });

export const testRule = (ruleId: string, description: string) =>
  request<{ input: string; matched_rules: string[]; primary: string }>(
    `/rules/${ruleId}/test?description=${encodeURIComponent(description)}`,
    { method: "POST" },
  );

// ============================================================
// Local (Jarvis-tracked issues)
// ============================================================

export interface LocalIssueItem {
  record_id: string;
  description: string;
  device_sn: string;
  firmware: string;
  app_version: string;
  priority: string;
  zendesk: string;
  zendesk_id: string;
  feishu_link: string;
  feishu_status: string;
  result_summary: string;
  root_cause_summary: string;
  created_at_ms: number;
  log_files: any[];
  local_status: string;
  analysis?: AnalysisResult;
  task?: { task_id: string; status: string; progress: number; message: string; error?: string };
}

export const fetchCompleted = (page = 1, pageSize = 20) =>
  request<PaginatedResponse<LocalIssueItem>>(`/local/completed?page=${page}&page_size=${pageSize}`);

export const fetchInProgress = (page = 1, pageSize = 20) =>
  request<PaginatedResponse<LocalIssueItem>>(`/local/in-progress?page=${page}&page_size=${pageSize}`);

export const fetchFailed = (page = 1, pageSize = 20) =>
  request<PaginatedResponse<LocalIssueItem>>(`/local/failed?page=${page}&page_size=${pageSize}`);

export const deleteIssue = (issueId: string) =>
  request<{ status: string }>(`/local/${issueId}`, { method: "DELETE" });

export const fetchTracking = (page = 1, pageSize = 20, createdBy?: string) => {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (createdBy) params.set("created_by", createdBy);
  return request<PaginatedResponse<LocalIssueItem>>(`/local/tracking?${params}`);
};

// ============================================================
// Reports
// ============================================================

export const fetchDailyReport = (date: string) => request<DailyReport>(`/reports/daily/${date}`);
export const fetchReportDates = () => request<{ dates: string[] }>("/reports/dates");

// ============================================================
// Users
// ============================================================

export const loginUser = (username: string) =>
  request<{ username: string; role: string; feishu_email: string }>("/users/login", {
    method: "POST", body: JSON.stringify({ username }),
  });

export const getUser = (username: string) =>
  request<{ username: string; role: string; feishu_email: string }>(`/users/${username}`);

// ============================================================
// Oncall
// ============================================================

export interface OncallGroup { group_index: number; members: string[]; }

export const getOncallCurrent = () => request<{ members: string[]; count: number }>("/oncall/current");
export const getOncallSchedule = () => request<{ groups: OncallGroup[]; start_date: string; total_groups: number }>("/oncall/schedule");
export const updateOncallSchedule = (groups: string[][], startDate: string, username: string) =>
  request<any>(`/oncall/schedule?username=${encodeURIComponent(username)}`, {
    method: "PUT",
    body: JSON.stringify({ groups: groups.map((m) => ({ members: m })), start_date: startDate }),
  });

// ============================================================
// Escalate
// ============================================================

export const escalateIssue = (issueId: string, reason?: string) =>
  request<{ status: string }>(`/local/${issueId}/escalate`, {
    method: "POST", body: JSON.stringify({ reason: reason || "用户手动转工程师" }),
  });

// ============================================================
// Settings & Health
// ============================================================

export const fetchAgentConfig = () => request<AgentConfig>("/settings/agent");
export const updateAgentConfig = (data: any) =>
  request<any>("/settings/agent", { method: "PUT", body: JSON.stringify(data) });
export const fetchHealth = () => request<HealthCheck>("/health");
export const checkAgents = () => request<Record<string, any>>("/health/agents");
