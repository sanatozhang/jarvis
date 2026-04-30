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
  platform?: string;
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

export interface ProblemCategoryItem {
  category: string;
  subcategory: string;
}

export interface LogMetadata {
  app_version?: string;
  build_info?: string;
  os_version?: string;
  platform?: string;
  device_model?: string;
  uid?: string;
  locale?: string;
  api_region?: string;
  file_ids?: string[];
}

export interface AnalysisResult {
  task_id: string;
  issue_id: string;
  problem_type: string;
  problem_type_en?: string;
  problem_categories?: ProblemCategoryItem[];
  device_type?: string;
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
  agent_model: string;
  followup_question?: string;
  log_metadata?: LogMetadata;
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

export const fetchPendingIssues = (assignee?: string, page = 1, pageSize = 20, includeInProgress = false) => {
  const params = new URLSearchParams();
  if (assignee) params.set("assignee", assignee);
  if (includeInProgress) params.set("include_in_progress", "true");
  params.set("page", String(page));
  params.set("page_size", String(pageSize));
  return request<PaginatedResponse<Issue> & { in_progress_count?: number }>(`/issues?${params}`);
};
export const fetchIssue = (id: string) => request<Issue>(`/issues/${id}`);
export const refreshIssuesCache = () => request<{ status: string }>("/issues/refresh", { method: "POST" });
export const searchFeishuIssues = (keyword: string) =>
  request<{ issues: Issue[] }>("/issues/import/search", {
    method: "POST",
    body: JSON.stringify({ url: keyword }),
  });
export const importIssueById = (recordId: string) =>
  request<{ status: string; issue: Issue }>("/issues/import", {
    method: "POST",
    body: JSON.stringify({ record_id: recordId }),
  });

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
  result_summary_en?: string;
  root_cause_summary: string;
  root_cause_summary_en?: string;
  created_at_ms: number;
  created_at?: string;
  created_by?: string;
  platform?: string;
  category?: string;
  source?: string;
  log_files: any[];
  local_status: string;
  escalated_at?: string;
  escalated_by?: string;
  escalation_note?: string;
  escalation_status?: string;
  escalation_chat_id?: string;
  escalation_share_link?: string;
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
  zendesk_id?: string;
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
    if (filters.zendesk_id) params.set("zendesk_id", filters.zendesk_id);
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

export interface EscalatedTicket {
  record_id: string;
  description: string;
  problem_type: string;
  problem_type_en: string;
  root_cause: string;
  confidence: string;
  user_reply: string;
  zendesk_id: string;
  source: string;
  escalated_at: string;
  escalated_by: string;
  escalation_note: string;
  escalation_status: string;
  escalation_resolved_at: string;
  escalation_chat_id?: string;
  escalation_share_link?: string;
  created_at: string;
}

export const getOncallTickets = (status?: string, weeks?: number) => {
  const p = new URLSearchParams();
  if (status) p.set("status", status);
  if (weeks !== undefined) p.set("weeks", String(weeks));
  const qs = p.toString();
  return request<{ tickets: EscalatedTicket[]; count: number; since_date: string; weeks: number }>(
    `/oncall/tickets${qs ? "?" + qs : ""}`
  );
};

export const resolveOncallTicket = (issueId: string) =>
  request<{ status: string; issue_id: string; feishu_notified: boolean }>(`/oncall/tickets/${issueId}/resolve`, { method: "PUT" });

export interface OncallWeekStat {
  week_num: number;
  group_index: number;
  members: string[];
  week_start: string;
  week_end: string;
  is_current: boolean;
  total: number;
  in_progress: number;
  resolved: number;
}

export const getOncallStats = () =>
  request<{ weeks: OncallWeekStat[]; groups: string[][]; start_date: string; current_week_num: number }>("/oncall/stats");

// ============================================================
// Inaccurate
// ============================================================

export const escalateIssue = (issueId: string, note: string = "", escalatedBy: string = "") => {
  const appllo_url = typeof window !== "undefined"
    ? `${window.location.origin}/tracking?detail=${issueId}`
    : "";
  return request<{ status: string; chat_id?: string; group_name?: string; share_link?: string }>(`/local/${issueId}/escalate`, {
    method: "POST",
    body: JSON.stringify({ note, escalated_by: escalatedBy, appllo_url }),
  });
};

export const markInaccurate = (issueId: string) =>
  request<{ status: string }>(`/local/${issueId}/inaccurate`, { method: "POST" });

export const markComplete = (issueId: string, username: string = "") =>
  request<{ status: string; feishu_synced: boolean }>(`/local/${issueId}/complete`, {
    method: "POST",
    body: JSON.stringify({ username }),
  });

export const fetchInaccurate = (page = 1, pageSize = 20) =>
  request<PaginatedResponse<LocalIssueItem>>(`/local/inaccurate?page=${page}&page_size=${pageSize}`);

// ============================================================
// Settings & Health
// ============================================================

export const fetchAgentConfig = () => request<AgentConfig>("/settings/agent");
export const fetchEscalationMembers = () => request<{ members: string[] }>("/settings/escalation-members");
export const updateEscalationMembers = (members: string[]) =>
  request<{ status: string; members: string[] }>("/settings/escalation-members", {
    method: "PUT",
    body: JSON.stringify({ members }),
  });
export const updateAgentConfig = (data: any) =>
  request<any>("/settings/agent", { method: "PUT", body: JSON.stringify(data) });
export const fetchHealth = () => request<HealthCheck>("/health");
export const checkAgents = () => request<Record<string, any>>("/health/agents");

// L1.5 Context Condensation
export interface CondensationConfig {
  enabled: boolean;
  provider: string;
  model: string;
  has_api_key: boolean;
  api_key_masked: string;
  log_size_threshold_mb: number;
  time_window_hours_before: number;
  time_window_hours_after: number;
  timeout: number;
  default_models: Record<string, string>;
}
export const fetchCondensationConfig = () => request<CondensationConfig>("/settings/condensation");
export const updateCondensationConfig = (data: Partial<CondensationConfig> & { api_key?: string }) =>
  request<{ status: string }>("/settings/condensation", {
    method: "PUT",
    body: JSON.stringify(data),
  });

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
// Problem Type Statistics
// ============================================================

export interface ProblemTypeItem {
  problem_type: string;
  problem_type_en: string;
  count: number;
}

export interface ProblemTypeStats {
  date_from: string;
  date_to: string;
  total: number;
  distribution: ProblemTypeItem[];
  top10: ProblemTypeItem[];
  trend: Record<string, Record<string, number>>; // { "2026-04-01": { "蓝牙连接": 3, ... } }
}

export const fetchProblemTypeStats = (days: number = 30) =>
  request<ProblemTypeStats>(`/analytics/problem-types?days=${days}`);

// ============================================================
// Classification Statistics (category + device breakdown)
// ============================================================

export interface SubcategoryItem {
  subcategory: string;
  count: number;
}

export interface CategoryDistItem {
  category: string;
  count: number;
  subcategories: SubcategoryItem[];
}

export interface DeviceCategoryItem {
  category: string;
  count: number;
}

export interface DeviceDistItem {
  device_type: string;
  count: number;
  categories: DeviceCategoryItem[];
}

export interface ClassificationStats {
  date_from: string;
  date_to: string;
  total: number;
  total_with_categories: number;
  category_distribution: CategoryDistItem[];
  device_distribution: DeviceDistItem[];
}

export const fetchClassificationStats = (days: number = 30) =>
  request<ClassificationStats>(`/analytics/classification-stats?days=${days}`);

export const backfillClassifications = (limit: number = 500) =>
  request<{ status: string; updated: number; total_candidates: number }>(
    `/analytics/backfill-classifications?limit=${limit}`,
    { method: "POST" },
  );

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

// ============================================================
// Crashguard
// ============================================================

export type CrashStatus =
  | "open"
  | "investigating"
  | "resolved_by_pr"
  | "ignored"
  | "wontfix";

export interface CrashTopItemAnalysisFlag {
  first_analyzed_at?: string | null;
  last_analyzed_at?: string | null;
}

export interface CrashTopItem extends CrashTopItemAnalysisFlag {
  datadog_issue_id: string;
  datadog_url: string;
  title: string;
  platform: string;
  service: string;
  events_count: number;
  users_affected: number;
  sessions_affected: number;
  crash_free_impact_score: number;
  is_new_in_version: boolean;
  is_regression: boolean;
  is_surge: boolean;
  tier: "P0" | "P1";
  status: CrashStatus;
  assignee: string;
  first_seen_version: string;
  last_seen_version: string;
  has_pr?: boolean;
  pr_url?: string;
  pr_number?: number | null;
  pr_status?: "" | "draft" | "open" | "merged" | "closed";
  pr_repo?: string;
}

export interface CrashTopResponse {
  date: string;
  count: number;
  issues: CrashTopItem[];
}

export interface CrashSnapshot {
  snapshot_date?: string;
  events_count: number;
  users_affected: number;
  sessions_affected?: number;
  crash_free_impact_score: number;
  is_new_in_version: boolean;
  is_regression: boolean;
  is_surge: boolean;
  app_version: string;
}

export interface CrashCause {
  title: string;
  evidence: string;
  confidence: string;
  code_pointer: string;
}

export interface CrashAnalysis {
  scenario: string;
  root_cause: string;
  fix_suggestion: string;
  feasibility_score: number;
  confidence: string;
  reproducibility: string;
  agent_name: string;
  agent_model?: string;
  status: string;
  possible_causes?: CrashCause[];
  complexity_kind?: "simple" | "complex" | "";
  solution?: string;
  hint?: string;
  run_id?: string;
  created_at?: string;
}

export interface CrashIssueDetail {
  datadog_issue_id: string;
  datadog_url: string;
  stack_fingerprint: string;
  title: string;
  platform: string;
  service: string;
  first_seen_at?: string;
  last_seen_at?: string;
  first_seen_version: string;
  last_seen_version: string;
  total_events: number;
  total_users_affected: number;
  representative_stack: string;
  tags: Record<string, unknown>;
  status: CrashStatus;
  assignee: string;
  top_os?: string;
  top_device?: string;
  top_app_version?: string;
  snapshot: CrashSnapshot | Record<string, never>;
  analysis: CrashAnalysis | Record<string, never>;
  pull_requests?: CrashIssuePr[];
}

export interface CrashIssuePr {
  id: number;
  pr_url: string;
  pr_number: number | null;
  pr_status: "draft" | "open" | "merged" | "closed";
  repo: string;
  branch_name: string;
  created_at: string | null;
  merged_at: string | null;
  closed_at: string | null;
  last_synced_at: string | null;
}

export interface CrashHealth {
  module: string;
  enabled: boolean;
  datadog_configured: boolean;
  feishu_target_set: boolean;
}

export const fetchCrashTop = (limit = 40, target_date?: string) => {
  const q = new URLSearchParams({ limit: String(limit) });
  if (target_date) q.set("target_date", target_date);
  return request<CrashTopResponse>(`/crash/top?${q.toString()}`);
};

export const updateCrashIssue = (
  issueId: string,
  patch: { status?: CrashStatus; assignee?: string },
) =>
  request<{ datadog_issue_id: string; status: CrashStatus; assignee: string }>(
    `/crash/issues/${encodeURIComponent(issueId)}`,
    { method: "PATCH", body: JSON.stringify(patch) },
  );

export interface CrashAnalyzeResponse {
  run_id: string;
  status: "pending" | "running" | "success" | "empty" | "failed";
}

export interface CrashAnalysisStatus {
  run_id: string;
  datadog_issue_id: string;
  status: "pending" | "running" | "success" | "empty" | "failed";
  scenario?: string;
  root_cause?: string;
  fix_suggestion?: string;
  feasibility_score?: number;
  confidence?: string;
  reproducibility?: string;
  agent_name?: string;
  agent_model?: string;
  possible_causes?: CrashCause[];
  complexity_kind?: "simple" | "complex" | "";
  solution?: string;
  hint?: string;
  error?: string;
  created_at?: string | null;
}

export const startCrashAnalysis = (issueId: string) =>
  request<CrashAnalyzeResponse>(`/crash/analyze/${encodeURIComponent(issueId)}`, {
    method: "POST",
    body: JSON.stringify({}),
  });

export const fetchCrashAnalysisStatus = (runId: string) =>
  request<CrashAnalysisStatus>(`/crash/analyses/${encodeURIComponent(runId)}`);

export interface CrashAnalysisRecord extends CrashAnalysisStatus {
  is_followup: boolean;
  followup_question: string;
  answer: string;
  parent_run_id: string;
}

export interface CrashAnalysesResponse {
  datadog_issue_id: string;
  count: number;
  analyses: CrashAnalysisRecord[];
}

export const fetchCrashAnalyses = (issueId: string) =>
  request<CrashAnalysesResponse>(`/crash/issues/${encodeURIComponent(issueId)}/analyses`);

export const followupCrashIssue = (issueId: string, question: string, parent_run_id?: string) =>
  request<{ run_id: string; status: string }>(
    `/crash/issues/${encodeURIComponent(issueId)}/followup`,
    {
      method: "POST",
      body: JSON.stringify({ question, parent_run_id }),
    },
  );

export interface BatchAnalyzeResult {
  scheduled: { datadog_issue_id: string; title: string; run_id: string; tier: string }[];
  skipped: { datadog_issue_id: string; title: string; reason: string }[];
  scanned: number;
}

export const batchAnalyzeCrash = (top_n?: number, force = false) =>
  request<BatchAnalyzeResult>(`/crash/batch-analyze`, {
    method: "POST",
    body: JSON.stringify({ top_n, force }),
  });

export interface DailyReportRunResult {
  ok: boolean;
  dry_run?: boolean;
  preview?: string;
  payload?: any;
  sent?: boolean;
  skipped_reason?: string;
  persisted_id?: number;
}

export const runCrashDailyReport = (
  report_type: "morning" | "evening",
  opts?: { top_n?: number; dry_run?: boolean; chat_id?: string }
) =>
  request<DailyReportRunResult>(`/crash/reports/run-now`, {
    method: "POST",
    body: JSON.stringify({
      report_type,
      top_n: opts?.top_n ?? 10,
      dry_run: opts?.dry_run ?? true,
      chat_id: opts?.chat_id,
    }),
  });

export interface CrashAuditSummary {
  window_hours: number;
  total: number;
  by_op: Record<string, { success: number; failed: number; last_at: string | null }>;
  recent_errors: { op: string; target_id: string; error: string; created_at: string | null }[];
}

export const fetchCrashAuditSummary = (hours = 48) =>
  request<CrashAuditSummary>(`/crash/audit-summary?hours=${hours}`);

export interface CrashReportHistoryItem {
  id: number;
  report_date: string | null;
  report_type: "morning" | "evening";
  top_n: number;
  new_count: number;
  regression_count: number;
  surge_count: number;
  feishu_message_id: string;
  created_at: string | null;
  summary: string;
  attention_total: number;
}

export const fetchCrashReportHistory = (opts?: {
  days?: number;
  report_type?: "morning" | "evening";
  limit?: number;
}) => {
  const qs = new URLSearchParams();
  if (opts?.days) qs.set("days", String(opts.days));
  if (opts?.report_type) qs.set("report_type", opts.report_type);
  if (opts?.limit) qs.set("limit", String(opts.limit));
  const q = qs.toString();
  return request<{ items: CrashReportHistoryItem[]; total: number; days: number }>(
    `/crash/reports/history${q ? "?" + q : ""}`
  );
};

export const fetchCrashReportDetail = (id: number) =>
  request<{
    id: number;
    report_date: string | null;
    report_type: string;
    markdown: string;
    payload: Record<string, unknown>;
    created_at: string | null;
  }>(`/crash/reports/${id}`);

export interface CrashPullRequestItem {
  id: number;
  datadog_issue_id: string;
  title: string;
  repo: string;
  branch_name: string;
  pr_url: string;
  pr_number: number | null;
  pr_status: "draft" | "open" | "merged" | "closed";
  triggered_by: string;
  approved_by: string | null;
  approved_at: string | null;
  feasibility: number;
  created_at: string | null;
  merged_at: string | null;
  closed_at: string | null;
  last_synced_at: string | null;
}

export interface CrashPrSyncResult {
  ok: boolean;
  pr_id?: number;
  old_status?: string;
  new_status?: string;
  changed?: boolean;
  skipped?: string;
  error?: string;
}

export const refreshCrashPr = (prId: number) =>
  request<CrashPrSyncResult>(`/crash/pull-requests/${prId}/refresh`, { method: "POST" });

export const syncAllCrashPrs = () =>
  request<{ checked: number; changed: number; errors: number }>(
    `/crash/pull-requests/sync-all`,
    { method: "POST" },
  );

export const fetchCrashPullRequests = (opts?: {
  days?: number;
  status?: "draft" | "open" | "merged" | "closed";
  repo?: "flutter" | "android" | "ios" | "app";
  limit?: number;
}) => {
  const qs = new URLSearchParams();
  if (opts?.days) qs.set("days", String(opts.days));
  if (opts?.status) qs.set("status", opts.status);
  if (opts?.repo) qs.set("repo", opts.repo);
  if (opts?.limit) qs.set("limit", String(opts.limit));
  const q = qs.toString();
  return request<{ items: CrashPullRequestItem[]; total: number; days: number }>(
    `/crash/pull-requests${q ? "?" + q : ""}`
  );
};

export const auditCleanup = (keep_days = 30) =>
  request<{ deleted: number; keep_days: number; cutoff: string }>(`/crash/audit-cleanup`, {
    method: "POST",
    body: JSON.stringify({ keep_days }),
  });

// 兼容老调用：异步启动 + 轮询，最长 8 分钟。
export const analyzeCrashIssue = async (issueId: string): Promise<CrashAnalysisStatus> => {
  const start = await startCrashAnalysis(issueId);
  const runId = start.run_id;
  const deadline = Date.now() + 8 * 60 * 1000;
  let delay = 3000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(delay + 1000, 8000);
    const st = await fetchCrashAnalysisStatus(runId);
    if (st.status === "success" || st.status === "failed" || st.status === "empty") {
      return st;
    }
  }
  return {
    run_id: runId,
    datadog_issue_id: issueId,
    status: "failed",
    error: "polling timeout (8min)",
  };
};

export const fetchCrashIssue = (issueId: string, target_date?: string) => {
  const q = new URLSearchParams();
  if (target_date) q.set("target_date", target_date);
  const qs = q.toString();
  return request<CrashIssueDetail>(`/crash/issues/${encodeURIComponent(issueId)}${qs ? "?" + qs : ""}`);
};

export const fetchCrashHealth = () => request<CrashHealth>("/crash/health");

// Lightweight feature flag check — used by Sidebar to conditionally show entry
export async function fetchCrashEnabled(): Promise<boolean> {
  try {
    const h = await fetchCrashHealth();
    return Boolean(h?.enabled);
  } catch {
    return false;  // 网络错 / 后端没起 → 视为不可用，隐藏入口
  }
}

export const triggerCrashPipeline = (latest_release: string, recent_versions: string[], target_date?: string) =>
  request<{ issues_processed: number; snapshots_written: number; top_n_count: number }>("/crash/trigger", {
    method: "POST",
    body: JSON.stringify({ latest_release, recent_versions, target_date }),
  });
