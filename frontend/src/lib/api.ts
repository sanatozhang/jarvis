/**
 * API client for the Jarvis backend.
 */

/**
 * Convert a UTC ISO timestamp (from the backend) to local time string.
 * Backend sends "2026-02-19T07:30:00Z" — we display "2026-02-19 15:30" (in UTC+8).
 */
export function formatLocalTime(utcIso: string | undefined | null, mode: "datetime" | "date" = "datetime"): string {
  if (!utcIso) return "—";
  const d = new Date(utcIso.endsWith("Z") ? utcIso : utcIso + "Z");
  if (isNaN(d.getTime())) return utcIso;
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  if (mode === "date") return date;
  return `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

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
  source?: string;
  feishu_link: string;
  feishu_status: "pending" | "in_progress" | "done";
  linear_issue_id?: string;
  linear_issue_url?: string;
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
  problem_type_en?: string;
  root_cause: string;
  root_cause_en?: string;
  confidence: string;
  confidence_reason: string;
  key_evidence: string[];
  core_logs: string[];
  code_locations: string[];
  user_reply: string;
  user_reply_en?: string;
  needs_engineer: boolean;
  requires_more_info: boolean;
  more_info_guidance: string;
  next_steps: string[];
  fix_suggestion: string;
  rule_type: string;
  agent_type: string;
  followup_question?: string;
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

export const createTask = (issueId: string, agentType?: string, username?: string, followupQuestion?: string) =>
  request<TaskProgress>("/tasks", {
    method: "POST",
    body: JSON.stringify({
      issue_id: issueId,
      agent_type: agentType || null,
      username: username || "",
      followup_question: followupQuestion || "",
    }),
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
  linear_issue_id?: string;
  linear_issue_url?: string;
  result_summary: string;
  root_cause_summary: string;
  created_at_ms: number;
  created_at?: string;
  created_by?: string;
  platform?: string;
  category?: string;
  source?: string;
  log_files: any[];
  local_status: string;
  analysis_count?: number;
  analysis?: AnalysisResult;
  task?: { task_id: string; status: string; progress: number; message: string; error?: string };
}

export const fetchIssueDetail = (issueId: string) =>
  request<LocalIssueItem>(`/local/${issueId}/detail`);

export const fetchIssueAnalyses = (issueId: string) =>
  request<AnalysisResult[]>(`/local/${issueId}/analyses`);

export const fetchCompleted = (page = 1, pageSize = 20) =>
  request<PaginatedResponse<LocalIssueItem>>(`/local/completed?page=${page}&page_size=${pageSize}`);

export const fetchInProgress = (page = 1, pageSize = 20) =>
  request<PaginatedResponse<LocalIssueItem>>(`/local/in-progress?page=${page}&page_size=${pageSize}`);

export const fetchFailed = (page = 1, pageSize = 20) =>
  request<PaginatedResponse<LocalIssueItem>>(`/local/failed?page=${page}&page_size=${pageSize}`);

export const deleteIssue = (issueId: string) =>
  request<{ status: string }>(`/local/${issueId}`, { method: "DELETE" });

export interface TrackingFilters {
  created_by?: string;
  platform?: string;
  category?: string;
  status?: string;
  source?: string;
  date_from?: string;
  date_to?: string;
}

export const fetchTracking = (page = 1, pageSize = 20, filters?: TrackingFilters) => {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (filters) {
    if (filters.created_by) params.set("created_by", filters.created_by);
    if (filters.platform) params.set("platform", filters.platform);
    if (filters.category) params.set("category", filters.category);
    if (filters.status) params.set("status", filters.status);
    if (filters.source) params.set("source", filters.source);
    if (filters.date_from) params.set("date_from", filters.date_from);
    if (filters.date_to) params.set("date_to", filters.date_to);
  }
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

export interface UserListItem {
  username: string;
  role: string;
  feishu_email: string;
  created_at: string;
  last_active_at: string;
  action_count: number;
}

export const fetchUsers = () => request<UserListItem[]>("/users");

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
// Inaccurate
// ============================================================

export const markInaccurate = (issueId: string) =>
  request<{ status: string }>(`/local/${issueId}/inaccurate`, { method: "POST" });

export const fetchInaccurate = (page = 1, pageSize = 20) =>
  request<PaginatedResponse<LocalIssueItem>>(`/local/inaccurate?page=${page}&page_size=${pageSize}`);

// ============================================================
// Settings & Health
// ============================================================

export const fetchAgentConfig = () => request<AgentConfig>("/settings/agent");
export const updateAgentConfig = (data: any) =>
  request<any>("/settings/agent", { method: "PUT", body: JSON.stringify(data) });
export const fetchHealth = () => request<HealthCheck>("/health");
export const checkAgents = () => request<Record<string, any>>("/health/agents");

// ============================================================
// Golden Samples
// ============================================================

export interface GoldenSample {
  id: number;
  issue_id: string;
  analysis_id: number;
  problem_type: string;
  description: string;
  root_cause: string;
  user_reply: string;
  confidence: string;
  rule_type: string;
  tags: string[];
  quality: string;
  created_by: string;
  created_at: string;
}

export interface GoldenSamplesStats {
  total: number;
  by_rule_type: Record<string, number>;
  by_problem_type: Record<string, number>;
}

export const promoteToGoldenSample = (analysisId: number, createdBy: string = "") =>
  request<GoldenSample>("/golden-samples", {
    method: "POST",
    body: JSON.stringify({ analysis_id: analysisId, created_by: createdBy }),
  });

export const fetchGoldenSamples = (ruleType?: string, limit?: number) => {
  const params = new URLSearchParams();
  if (ruleType) params.set("rule_type", ruleType);
  if (limit) params.set("limit", String(limit));
  return request<GoldenSample[]>(`/golden-samples?${params}`);
};

export const fetchGoldenSamplesStats = () =>
  request<GoldenSamplesStats>("/golden-samples/stats");

export const deleteGoldenSample = (id: number) =>
  request<{ status: string }>(`/golden-samples/${id}`, { method: "DELETE" });

// ============================================================
// Rule Accuracy
// ============================================================

export interface RuleAccuracyStat {
  rule_type: string;
  total: number;
  done: number;
  inaccurate: number;
  accuracy_rate: number;
  avg_confidence_score: number;
}

export const fetchRuleAccuracy = (days: number = 30) =>
  request<RuleAccuracyStat[]>(`/analytics/rule-accuracy?days=${days}`);

// ============================================================
// Eval Pipeline
// ============================================================

export interface EvalDataset {
  id: number;
  name: string;
  description: string;
  sample_ids: number[];
  created_by: string;
  created_at: string;
}

export interface EvalRunSummary {
  total_samples: number;
  completed: number;
  errors: number;
  avg_overall_score: number;
  avg_problem_type_match: number;
  avg_root_cause_similarity: number;
  avg_confidence_match: number;
}

export interface EvalRun {
  id: number;
  dataset_id: number;
  status: string;
  config: Record<string, any>;
  results: any[];
  summary: EvalRunSummary & Record<string, any>;
  started_at: string | null;
  finished_at: string | null;
  created_by: string;
  created_at: string;
}

export const createEvalDataset = (data: { name: string; description?: string; sample_ids: number[]; created_by?: string }) =>
  request<{ id: number; name: string; status: string }>("/eval/datasets", {
    method: "POST",
    body: JSON.stringify(data),
  });

export const fetchEvalDatasets = () => request<EvalDataset[]>("/eval/datasets");

export const fetchEvalDataset = (id: number) => request<EvalDataset>(`/eval/datasets/${id}`);

export const startEvalRun = (datasetId: number, config: Record<string, any> = {}, createdBy: string = "") =>
  request<{ id: number; status: string }>("/eval/run", {
    method: "POST",
    body: JSON.stringify({ dataset_id: datasetId, config, created_by: createdBy }),
  });

export const fetchEvalRuns = (datasetId?: number) => {
  const params = new URLSearchParams();
  if (datasetId) params.set("dataset_id", String(datasetId));
  return request<EvalRun[]>(`/eval/runs?${params}`);
};

export const fetchEvalRun = (id: number) => request<EvalRun>(`/eval/runs/${id}`);

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------
export interface LostFileFinderResult {
  markdown: string;
  total_records: number;
  anomaly_count: number;
  problem_date_text: string;
  timezone_label: string;
}

export async function analyzeLostFile(
  file: File,
  problemDate: string,
  tzOffset: number,
): Promise<LostFileFinderResult> {
  const form = new FormData();
  form.append("file", file);
  form.append("problem_date", problemDate);
  form.append("tz_offset", String(tzOffset));
  const res = await fetch(`${BASE}/tools/lost-file-finder`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "分析失败");
  }
  return res.json();
}

// ============================================================
// Wishes (许愿池)
// ============================================================
export interface Wish {
  id: number;
  title: string;
  description: string;
  status: string;
  votes: number;
  created_by: string;
  created_at: string;
}

export const fetchWishes = () => request<Wish[]>("/wishes");

export const createWish = (data: { title: string; description?: string; created_by?: string }) =>
  request<Wish>("/wishes", { method: "POST", body: JSON.stringify(data) });

export const updateWish = (id: number, data: { title?: string; description?: string; status?: string }) =>
  request<Wish>(`/wishes/${id}`, { method: "PUT", body: JSON.stringify(data) });

export const voteWish = (id: number) =>
  request<Wish>(`/wishes/${id}/vote`, { method: "POST" });

export const deleteWish = (id: number) =>
  request<{ deleted: number }>(`/wishes/${id}`, { method: "DELETE" });
