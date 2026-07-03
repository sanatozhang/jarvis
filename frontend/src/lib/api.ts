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

/**
 * Convert a UTC ISO timestamp to Singapore time (UTC+8) string.
 * Unlike formatLocalTime, this is deterministic regardless of browser TZ —
 * Plaud 主用户在 SG/上海，所有时间统一锚定 UTC+8 展示，避免跨时区误读。
 *
 * Backend may send naive UTC strings without "Z" suffix; we add it before parsing.
 */
export function formatSGT(utcIso: string | undefined | null, mode: "datetime" | "date" = "datetime"): string {
  if (!utcIso) return "—";
  const iso = utcIso.endsWith("Z") ? utcIso : utcIso + "Z";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return utcIso;
  // 强制 UTC+8 投影：用 getTime() + 8h 后取 UTC 字段，避开浏览器本地 TZ
  const sgt = new Date(d.getTime() + 8 * 3600 * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${sgt.getUTCFullYear()}-${pad(sgt.getUTCMonth() + 1)}-${pad(sgt.getUTCDate())}`;
  if (mode === "date") return date;
  return `${date} ${pad(sgt.getUTCHours())}:${pad(sgt.getUTCMinutes())}`;
}

const BASE = "/api";

// 携带 HTTP 状态 + endpoint 的结构化错误，前端可按 status 决定是否降级显示
export class ApiError extends Error {
  status: number;
  endpoint: string;
  body: string;
  constructor(status: number, endpoint: string, body: string) {
    super(`API ${status}: ${body || "request failed"}`);
    this.status = status;
    this.endpoint = endpoint;
    this.body = body;
    this.name = "ApiError";
  }
}

const DEFAULT_TIMEOUT_MS = 15000;
const RETRY_DELAYS_MS = [300, 800]; // 最多重试 2 次

// GET / HEAD 才自动重试——POST/PUT/DELETE 重试可能造成副作用（重复下单）
function _isRetriableMethod(method: string): boolean {
  const m = method.toUpperCase();
  return m === "GET" || m === "HEAD";
}

// 5xx + 网络错（fetch 抛 TypeError）才重试，4xx 直接抛
function _isRetriableStatus(status: number): boolean {
  return status >= 500 && status <= 599;
}

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const headers = new Headers(init?.headers || {});
  const isFormDataBody =
    typeof FormData !== "undefined" && init?.body instanceof FormData;
  if (!isFormDataBody && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const timeoutMs = init?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const method = (init?.method || "GET").toUpperCase();
  const canRetry = _isRetriableMethod(method);
  const maxAttempts = canRetry ? RETRY_DELAYS_MS.length + 1 : 1;

  let lastErr: any = null;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    // 每次 attempt 用独立 AbortController + 超时
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeoutMs);
    try {
      const res = await fetch(`${BASE}${path}`, {
        ...init,
        headers,
        credentials: init?.credentials ?? "include",
        signal: init?.signal ?? ac.signal,
      });
      if (res.ok) {
        return await res.json();
      }
      // 401 → SSO 未登录/过期：跳 /login（除非已在 /login 页）
      if (res.status === 401 && typeof window !== "undefined" && window.location.pathname !== "/login") {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = `/login?next=${next}`;
        throw new ApiError(401, path, "unauthenticated");
      }
      // 非 2xx
      const text = await res.text();
      if (canRetry && _isRetriableStatus(res.status) && attempt < maxAttempts - 1) {
        lastErr = new ApiError(res.status, path, text);
        await new Promise((r) => setTimeout(r, RETRY_DELAYS_MS[attempt]));
        continue;
      }
      throw new ApiError(res.status, path, text);
    } catch (err: any) {
      // 已经是 ApiError 而且不可重试 → 直接抛
      if (err instanceof ApiError) {
        if (canRetry && _isRetriableStatus(err.status) && attempt < maxAttempts - 1) {
          lastErr = err;
          await new Promise((r) => setTimeout(r, RETRY_DELAYS_MS[attempt]));
          continue;
        }
        throw err;
      }
      // 网络错 / abort / DNS / 连接拒绝 → 仅 GET 重试
      lastErr = err;
      if (canRetry && attempt < maxAttempts - 1) {
        await new Promise((r) => setTimeout(r, RETRY_DELAYS_MS[attempt]));
        continue;
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }
  throw lastErr || new Error("request failed");
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
  assignee?: string;
  assignee_emails?: string[];
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
  code_routing?: {
    family?: string;       // flutter | native | web | desktop | ""
    repo?: string;
    version?: string;
    platform?: string;
    confidence?: string;   // high | low | fallback | none
    source?: string;       // resolved | fallback-app | logs-only
  };
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
  // T1: 字段拆分
  system_failure?: boolean;
  needs_user_retry?: boolean;
  // T3: 客服反馈状态（null=未反馈, true=确实需要, false=AI 误判）
  engineer_label_feedback?: boolean | null;
  engineer_label_feedback_by?: string;
  engineer_label_feedback_at?: string;
  engineer_label_feedback_note?: string;
  requires_more_info: boolean;
  more_info_guidance: string;
  next_steps: string[];
  fix_suggestion: string;
  rule_type: string;
  agent_type: string;
  agent_model: string;
  followup_question?: string;
  log_metadata?: LogMetadata;
  // 计量（每次分析/追问独立计费）
  total_tokens?: number;
  total_cost_usd?: number;
  usage_breakdown?: Record<string, { cost_usd?: number; model?: string; source?: string; input_tokens?: number; output_tokens?: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number }>;
  cost_source?: string;        // cli_reported / computed / partial
  is_deep_analysis?: boolean;  // 深度分析标记 → 结果页打 label
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
  call_mode: "api" | "cli";
  api_traffic_ratio: number;  // 0.0–1.0
  timeout: number;
  max_turns: number;
  providers: Record<string, any>;
  routing: Record<string, string>;
}

// Agent execution trace (claude_api only)
export interface AgentTraceToolCall {
  name: string;
  input: Record<string, any>;
  ok: boolean;
  summary?: string;
  error?: string;
}

export interface AgentTraceUsage {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
}

export interface AgentTraceTurn {
  turn: number;
  stop_reason?: string;
  model?: string;
  tool_calls?: AgentTraceToolCall[];
  usage?: AgentTraceUsage;
  duration_ms?: number;
  error?: string;
  msg?: string;
  status?: number;
  final_text_chars?: number;
}

export interface AgentTraceEvent {
  event: string;
  [k: string]: any;
}

export interface AgentTraceSummary {
  total_turns: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_creation_tokens: number;
  cache_hit_ratio: number;
  total_duration_ms: number;
}

export interface AgentTraceResponse {
  task_id: string;
  summary: AgentTraceSummary;
  events: AgentTraceEvent[];
  turns: AgentTraceTurn[];
}

export const fetchTaskTrace = async (taskId: string): Promise<AgentTraceResponse | null> => {
  try {
    return await request<AgentTraceResponse>(`/tasks/${taskId}/trace`);
  } catch (e: any) {
    // 404 means task ran in CLI mode (no trace) — return null instead of throwing
    if (typeof e?.message === "string" && e.message.includes("404")) return null;
    throw e;
  }
};

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

export const createTask = (issueId: string, agentType?: string, username?: string, followupQuestion?: string, deepAnalysis?: boolean) =>
  request<TaskProgress>("/tasks", {
    method: "POST",
    body: JSON.stringify({
      issue_id: issueId,
      agent_type: agentType || null,
      username: username || "",
      followup_question: followupQuestion || "",
      deep_analysis: deepAnalysis ?? false,
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

export const loginUser = (username: string, email?: string) =>
  request<{ username: string; role: string; feishu_email: string }>("/users/login", {
    method: "POST",
    body: JSON.stringify(email ? { username, email } : { username }),
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

// Feishu tickets handled directly in Feishu (not escalated through the site).
// status: "open" (pending+in_progress, default) | "done" | "all"
// oncallOnly=false → all assignees' tickets (each carries assignee_emails for client-side grouping)
export const getOncallFeishuTickets = (status: string = "open", oncallOnly: boolean = true) =>
  request<{ tickets: Issue[]; count: number; status: string }>(
    `/oncall/feishu-tickets?status=${encodeURIComponent(status)}&oncall_only=${oncallOnly}`
  );

// Mark a Feishu ticket done (确认提交=true on the bitable)
export const resolveFeishuTicket = (recordId: string) =>
  request<{ status: string; record_id: string }>(
    `/oncall/feishu-tickets/${recordId}/resolve`, { method: "PUT" }
  );

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

export const escalateIssue = (issueId: string, note: string = "", escalatedBy: string = "", escalatedByEmail: string = "") => {
  const appllo_url = typeof window !== "undefined"
    ? `${window.location.origin}/tracking?detail=${issueId}`
    : "";
  return request<{ status: string; chat_id?: string; group_name?: string; share_link?: string; group_exists?: boolean }>(`/local/${issueId}/escalate`, {
    method: "POST",
    body: JSON.stringify({ note, escalated_by: escalatedBy, escalated_by_email: escalatedByEmail, appllo_url }),
  });
};

export const markInaccurate = (issueId: string) =>
  request<{ status: string }>(`/local/${issueId}/inaccurate`, { method: "POST" });

export const markComplete = (issueId: string, username: string = "", reason: string = "") =>
  request<{ status: string; feishu_synced: boolean; feishu_notified: boolean }>(`/local/${issueId}/complete`, {
    method: "POST",
    body: JSON.stringify({ username, reason }),
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

export type CrashFatality = "fatal" | "non_fatal";

export interface CrashTopItem extends CrashTopItemAnalysisFlag {
  datadog_issue_id: string;
  datadog_url: string;
  title: string;
  platform: string;
  service: string;
  kind?: string;
  fatality?: CrashFatality;
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
  analysis_id?: number | null;
  analysis_feasibility_score?: number | null;
  analysis_confidence?: string;
}

export interface CrashTopAggregates {
  p0_count: number;
  surge_count: number;
  new_count: number;
  fatal_count: number;
  non_fatal_count: number;
  total_events: number;
  total_users: number;
  total_sessions: number;
  // crash-free sessions % (来自 crash_metric_snapshots，window_hours 加权 sum)
  // null = 窗口内无 session 数据
  crash_free_sessions_pct?: number | null;
  crash_free_total_sessions?: number;
  crash_free_crashed_sessions?: number;
}

export interface CrashTopResponse {
  date: string;
  count: number;
  issues: CrashTopItem[];
  total: number;
  aggregates?: CrashTopAggregates;
  // 分页字段（仅当 page 传参时返回）
  page?: number;
  page_size?: number;
  total_pages?: number;
}

export type CrashSortBy = "events" | "impact" | "users" | "new_first";

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
  id?: number;
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

export interface CrashStackVariant {
  top_frame: string;
  count: number;
  pct: number;
  representative_stack: string;
  sample_app_version?: string;
  sample_view?: string;
  stack_quality?: string;
  is_main?: boolean;
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
  stack_variants?: CrashStackVariant[];
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

// 时间窗口档位（小时）。1d / 7d / 14d / 30d
export type CrashWindowHours = 24 | 168 | 336 | 720;

export const fetchCrashTop = (
  limit = 40,
  target_date?: string,
  opts?: {
    fatality?: CrashFatality | "";
    kinds?: string;
    // 新分页 + 后端过滤参数（首页用）
    page?: number;
    page_size?: number;
    platform?: string;
    status?: string;
    search?: string;
    sort_by?: CrashSortBy;
    window_hours?: CrashWindowHours;
  },
) => {
  const q = new URLSearchParams({ limit: String(limit) });
  if (target_date) q.set("target_date", target_date);
  if (opts?.fatality) q.set("fatality", opts.fatality);
  if (opts?.kinds) q.set("kinds", opts.kinds);
  if (opts?.page !== undefined) q.set("page", String(opts.page));
  if (opts?.page_size !== undefined) q.set("page_size", String(opts.page_size));
  if (opts?.platform) q.set("platform", opts.platform);
  if (opts?.status) q.set("status", opts.status);
  if (opts?.search) q.set("search", opts.search);
  if (opts?.sort_by) q.set("sort_by", opts.sort_by);
  if (opts?.window_hours) q.set("window_hours", String(opts.window_hours));
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

export const startCrashAnalysis = (issueId: string, userPrompt = "") =>
  request<CrashAnalyzeResponse>(`/crash/analyze/${encodeURIComponent(issueId)}`, {
    method: "POST",
    body: JSON.stringify({ user_prompt: userPrompt }),
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

export interface AutoPrQueueItem {
  analysis_id?: number | string;
  datadog_issue_id?: string;
  title?: string;
  platform?: string;
  feasibility_score?: number;
  created_at?: string;
  started_at?: string;
}
export interface AutoPrQueuePr {
  id: number;
  datadog_issue_id: string;
  repo: string;
  pr_number?: number | null;
  pr_url: string;
  pr_status: string;
  branch_name?: string;
  created_at?: string;
}
export interface AutoPrQueueFailure {
  analysis_id: number | string;
  error: string;
  created_at?: string;
}
export interface AutoPrQueueResponse {
  threshold: number;
  summary: {
    pending: number;
    running: number;
    recent_prs: number;
    recent_failures: number;
  };
  pending: AutoPrQueueItem[];
  running: AutoPrQueueItem[];
  recent_prs: AutoPrQueuePr[];
  recent_failures: AutoPrQueueFailure[];
}

export const fetchAutoPrQueue = () =>
  request<AutoPrQueueResponse>(`/crash/auto-pr-queue`);

export interface BackfillAutoPrResult {
  scanned: number;
  triggered: number;
  skipped_dup: number;
  skipped_limit?: number;
  failed: { analysis_id: number; error: string }[];
  candidates?: any[];
}
export const backfillAutoPr = (opts?: {
  days?: number;
  dry_run?: boolean;
  min_feasibility?: number;
  limit?: number;
}) =>
  request<BackfillAutoPrResult>(`/crash/backfill-auto-pr`, {
    method: "POST",
    body: JSON.stringify({
      days: opts?.days ?? 14,
      dry_run: opts?.dry_run ?? false,
      min_feasibility: opts?.min_feasibility,
      limit: opts?.limit ?? 0,
    }),
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
    timeoutMs: 120_000,  // report generation can take 30-60s; default 15s would abort it
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
  kind: "daily" | "hourly_alert" | "core_metric_alert";
  id: number;
  sort_key?: string | null;
  report_date: string | null;
  report_type: "morning" | "evening" | "hourly_alert" | "core_metric_alert";
  hour_utc?: string | null;
  // core_metric_alert 专属
  window_start?: string | null;
  platforms_alerted?: string | null;
  direction?: string | null;
  top_n: number;
  new_count: number;
  regression_count: number;
  surge_count: number;
  feishu_message_id: string;
  created_at: string | null;
  summary: string;
  attention_total: number;
}

export const fetchCoreMetricAlertDetail = (id: number) =>
  request<{
    id: number;
    kind: "core_metric_alert";
    window_start: string | null;
    direction: string;
    platforms_alerted: string;
    feishu_message_id: string;
    markdown: string;
    payload: Record<string, unknown>;
    created_at: string | null;
  }>(`/crash/alerts/core-metric/${id}`);

export const fetchCrashReportHistory = (opts?: {
  days?: number;
  report_type?: "morning" | "evening" | "hourly_alert" | "core_metric_alert";
  page?: number;
  page_size?: number;
}) => {
  const qs = new URLSearchParams();
  if (opts?.days) qs.set("days", String(opts.days));
  if (opts?.report_type) qs.set("report_type", opts.report_type);
  if (opts?.page) qs.set("page", String(opts.page));
  if (opts?.page_size) qs.set("page_size", String(opts.page_size));
  const q = qs.toString();
  return request<{
    items: CrashReportHistoryItem[];
    total: number;
    page: number;
    page_size: number;
    total_pages: number;
    days: number;
  }>(`/crash/reports/history${q ? "?" + q : ""}`);
};

export const fetchCrashReportDetail = (id: number, window_hours?: CrashWindowHours) => {
  const qs = window_hours ? `?window_hours=${window_hours}` : "";
  return request<{
    id: number;
    report_date: string | null;
    report_type: string;
    window_hours?: number;
    markdown: string;
    payload: Record<string, unknown>;
    created_at: string | null;
  }>(`/crash/reports/${id}${qs}`);
};

export interface CrashJobStatusItem {
  name: string;
  label: string;
  desc: string;
  cron: string;
  enabled: boolean;
  interval_minutes: number | null;
  next_fire_at: string | null;
  last_fired_at: string | null;
  last_status: "success" | "failed" | "skipped" | null;
  last_duration_ms: number;
  last_error: string;
  last_summary: Record<string, unknown>;
  last_success_at: string | null;
  fail_count_in_recent_50: number;
  consecutive_failures: number;
  stale: boolean;
  health: "ok" | "degraded" | "failing" | "stale";
  // 模块归属 — 前端按这个字段分发 trigger / heartbeats 调用到正确后端 API。
  // crashguard 自家任务该字段缺省（向后兼容），coreguard 任务由后端注入 "coreguard"。
  module?: "crashguard" | "coreguard";
}

export const fetchCrashJobsStatus = () =>
  request<{
    items: CrashJobStatusItem[];
    server_time_local: string;
    server_time_utc: string;
  }>("/crash/jobs/status");

// Coreguard 心跳走独立 API（隔离合约），shape 已经在后端对齐 crashguard。
export const fetchCoreguardJobsStatus = () =>
  request<{
    items: CrashJobStatusItem[];
    server_time_local: string;
    server_time_utc: string;
  }>("/coreguard/jobs/status");

export const fetchCoreguardJobHeartbeats = (jobName: string, limit = 50) =>
  request<{ job_name: string; items: CrashJobHeartbeatItem[] }>(
    `/coreguard/jobs/${encodeURIComponent(jobName)}/heartbeats?limit=${limit}`,
  );

export const triggerCoreguardJobNow = (jobName: string) =>
  request<{ ok: boolean; job?: string; reason?: string }>(
    `/coreguard/jobs/${encodeURIComponent(jobName)}/run-now`,
    { method: "POST", body: "{}" },
  );

export interface CrashJobHeartbeatItem {
  id: number;
  fired_at: string | null;
  // 与后端 record_heartbeat 三态升级对齐（commit 5cfdfe2）：success / degraded / failed；
  // skipped 是 kill_switch 场景，保留兜底
  status: "success" | "degraded" | "failed" | "skipped";
  duration_ms: number;
  error: string;
  summary: Record<string, unknown>;
}

export const fetchCrashJobHeartbeats = (jobName: string, limit = 50) =>
  request<{ job_name: string; items: CrashJobHeartbeatItem[]; total: number }>(
    `/crash/jobs/${encodeURIComponent(jobName)}/heartbeats?limit=${limit}`,
  );

export const triggerCrashJobNow = (jobName: string) =>
  request<{ ok: boolean; job_name: string; result: Record<string, unknown> }>(
    `/crash/jobs/${encodeURIComponent(jobName)}/run-now`,
    { method: "POST", body: "{}" },
  );

export interface AlertChannelItem {
  name: string;
  label: string;
  count_24h: number;
  enabled: boolean;
  shadow_mode: boolean;
  threshold?: Record<string, number>;
}

export interface AlertChannelsStatus {
  ok: boolean;
  window_hours: number;
  as_of: string;
  channels: AlertChannelItem[];
  audit_rows_24h: number;
  datadog_cache: { keys: string[]; count: number };
}

export const fetchAlertChannelsStatus = () =>
  request<AlertChannelsStatus>("/crash/alert-channels");

export const fetchCrashHourlyAlertDetail = (id: number) =>
  request<{
    id: number;
    kind: "hourly_alert";
    hour_utc: string | null;
    new_count: number;
    surge_count: number;
    feishu_message_id: string;
    markdown: string;
    payload: Record<string, unknown>;
    created_at: string | null;
  }>(`/crash/alerts/hourly/${id}`);

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

export interface ApproveCrashPrResult {
  ok: boolean;
  error?: string;
  reason?: string;
  total?: number;
  succeeded?: number;
  failed?: number;
  prs?: {
    ok: boolean;
    error?: string;
    pr_url?: string;
    pr_number?: number | null;
    pr_status?: "draft" | "open" | "merged" | "closed";
    branch_name?: string;
    repo?: string;
  }[];
  pr_id?: number;
  pr_url?: string;
  pr_number?: number | null;
  pr_status?: "draft" | "open" | "merged" | "closed";
  branch?: string;
  repo?: string;
  dry_run?: boolean;
}

export const approveCrashPr = (analysisId: number, opts?: { approver?: string; dry_run?: boolean }) =>
  request<ApproveCrashPrResult>(`/crash/approve-pr/${analysisId}`, {
    method: "POST",
    body: JSON.stringify({
      approver: opts?.approver || "human",
      dry_run: !!opts?.dry_run,
    }),
  });

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

// 兼容老调用：异步启动 + 轮询，最长 8 分钟。userPrompt 可选——传入则当作 followup 引导 AI。
export const analyzeCrashIssue = async (issueId: string, userPrompt = ""): Promise<CrashAnalysisStatus> => {
  const start = await startCrashAnalysis(issueId, userPrompt);
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

export const fetchCrashIssue = (
  issueId: string,
  target_date?: string,
  window_hours?: CrashWindowHours,
) => {
  const q = new URLSearchParams();
  if (target_date) q.set("target_date", target_date);
  if (window_hours) q.set("window_hours", String(window_hours));
  const qs = q.toString();
  return request<CrashIssueDetail>(`/crash/issues/${encodeURIComponent(issueId)}${qs ? "?" + qs : ""}`);
};

export const fetchCrashHealth = () => request<CrashHealth>("/crash/health");

export interface CrashLatestRelease {
  versions: { flutter: string; android: string; ios: string };
  min_events_threshold: number;
  source: { flutter: string; android: string; ios: string };
  // 用户量最大版本（仅 android / ios；Flutter 也跑在这俩上）
  top_user_versions?: Partial<Record<"android" | "ios", { version: string; users: number }>>;
  top_user_versions_source?: Partial<Record<"android" | "ios", string>>;
}

export const fetchCrashLatestRelease = () =>
  request<CrashLatestRelease>("/crash/latest-release");

export interface CrashVersionSlice {
  version: string;
  sessions: number;
  // 该版本的 crashed sessions 数（@session.crash.count:>0）
  // Datadog 主路径直查；crash_issues_fallback 路径反推不出 → null
  crashes?: number | null;
  pct: number;
}

export interface CrashVersionDistribution {
  data: Partial<Record<"android" | "ios", CrashVersionSlice[]>>;
  source: "datadog_rum" | "crash_issues_fallback";
  window_hours: number;
}

export const fetchCrashVersionDistribution = (window_hours = 24) =>
  request<CrashVersionDistribution>(`/crash/version-distribution?window_hours=${window_hours}`);

export interface CrashDeviceSlice {
  model: string;
  sessions: number;
  pct: number;
}

export interface CrashDeviceDistribution {
  data: Partial<Record<"android" | "ios", CrashDeviceSlice[]>>;
  source: string;
  window_hours: number;
}

export const fetchCrashDeviceDistribution = (window_hours = 24) =>
  request<CrashDeviceDistribution>(`/crash/device-distribution?window_hours=${window_hours}`);

export interface CrashOsVersionSlice {
  version: string;
  sessions: number;
  pct: number;
}

export interface CrashOsVersionDistribution {
  data: Partial<Record<"android" | "ios", CrashOsVersionSlice[]>>;
  source: string;
  window_hours: number;
}

export const fetchCrashOsVersionDistribution = (window_hours = 24) =>
  request<CrashOsVersionDistribution>(`/crash/os-version-distribution?window_hours=${window_hours}`);

export interface CrashPlatformSummary {
  total_sessions: number;
  crashed_sessions: number;
  crash_free_pct: number;
}

export interface CrashPlatformSummaryResponse {
  data: Partial<Record<"android" | "ios", CrashPlatformSummary>>;
  source: string;
  window_hours: number;
}

export const fetchCrashPlatformSummary = (window_hours = 24) =>
  request<CrashPlatformSummaryResponse>(`/crash/platform-summary?window_hours=${window_hours}`);

// ---------------------------------------------------------------------------
// T3 客服反馈闭环：对 AI 的 needs_engineer 标签做事后纠偏
// ---------------------------------------------------------------------------
export interface EngineerLabelFeedbackResponse {
  status: string;
  analysis_id: number;
  ai_needs_engineer: boolean;
  actually_needed_engineer: boolean;
  matched: boolean;
}

export async function submitEngineerLabelFeedback(params: {
  issue_id: string;
  task_id?: string;
  actually_needed_engineer: boolean;
  feedback_by?: string;
  note?: string;
}): Promise<EngineerLabelFeedbackResponse> {
  return request<EngineerLabelFeedbackResponse>("/feedback/engineer-label", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
}

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

// 完整闭环：拉数 → Top10 选取 → 串行 auto-analyze（含 auto-PR 钩子）
// timeoutMs 显式提到 60s：warmup 同步阶段要拉 Datadog 双路 + upsert 7 张表，
// 默认 15s 在 Datadog 抖动 / 冷启动时会被 AbortController 强行 abort，
// 抛 "signal is aborted without reason"，污染用户视线（AI 分析其实仍在后台跑）
export const triggerCrashWarmup = () =>
  request<{
    issues_processed: number;
    attention_count: number;
    analyzed: number;
    auto_pr?: {
      scanned: number;
      attempted: number;
      created: number;
      skipped: number;
      failed: { analysis_id: string; error: string }[];
    };
  }>(
    "/crash/warmup", { method: "POST", timeoutMs: 60_000 }
  );

// ── Phase 1 深度诊断 ───────────────────────────────────────────

export interface DiagnosisHypothesis {
  id: string;
  title: string;
  evidence: string[];
  confidence: number;
  fix_direction: string;
  code_pointers: string[];
  can_fix_now: boolean;
  complexity: "simple" | "complex";
}

export interface DiagnosisDataGap {
  description: string;
  collection_method: string;
  instrumentation_code: string;
  datadog_query: string;
}

export interface DiagnosisStatus {
  run_id: string;
  datadog_issue_id: string;
  phase: "diagnosis" | "fix";
  status: "pending" | "running" | "success" | "failed" | "empty" | "waiting_data";
  crash_type: string;
  hypotheses: DiagnosisHypothesis[];
  data_gaps: DiagnosisDataGap[];
  investigation_log: string[];
  overall_confidence: number;
  recommended_hypothesis: string;
  confirmed_hypothesis_id: string;
  error: string;
  created_at: string | null;
}

export const startDeepAnalysis = (issueId: string) =>
  request<{ run_id: string; status: string }>(
    `/crash/issues/${encodeURIComponent(issueId)}/deep-analyze`,
    { method: "POST", body: JSON.stringify({}) },
  );

export const fetchDiagnosisStatus = (runId: string) =>
  request<DiagnosisStatus>(`/crash/analyses/${encodeURIComponent(runId)}`);

export const confirmDiagnosisHypothesis = (runId: string, hypothesisId: string) =>
  request<{ diagnosis_run_id: string; phase2_run_id: string; hypothesis_id: string }>(
    `/crash/analyses/${encodeURIComponent(runId)}/confirm-hypothesis`,
    { method: "POST", body: JSON.stringify({ hypothesis_id: hypothesisId }) },
  );

export const markDiagnosisDataNeeded = (runId: string, note: string) =>
  request<{ run_id: string; status: string; note: string }>(
    `/crash/analyses/${encodeURIComponent(runId)}/mark-data-needed`,
    { method: "POST", body: JSON.stringify({ note }) },
  );

export interface SymbolSettings {
  symbol_upload_keep_versions: number;
  github_cache_keep_versions: number;
}
export const fetchSymbolSettings = () =>
  request<SymbolSettings>("/crash/settings/symbols");
export const updateSymbolSettings = (patch: Partial<SymbolSettings>) =>
  request<SymbolSettings & { updated: string[] }>(
    "/crash/settings/symbols",
    { method: "PATCH", body: JSON.stringify(patch) },
  );

// ============================================================
// Release 自动化
// ============================================================
export interface ReleaseRepoCommit {
  name: string;
  commit_sha: string;
}

export interface ReleaseBranch {
  id: number;
  branch: string;
  version: string;
  date_tag: string;
  repos: ReleaseRepoCommit[];
  created_by: string;
  created_at: string;
  status: string;
}

export interface ReleaseBuild {
  id: number;
  branch: string;
  target: "cn" | "global";
  android_multi_channel: boolean;
  params: Record<string, string>;
  jenkins_server: string;
  jenkins_job: string;
  jenkins_queue_id: number | null;
  jenkins_build_number: number | null;
  jenkins_build_url: string;
  status:
    | "pending"
    | "queued"
    | "running"
    | "success"
    | "failure"
    | "aborted"
    | "error";
  started_at: string | null;
  finished_at: string | null;
  error_message: string;
  artifact_android_url: string;
  artifact_ios_url: string;
  triggered_by: string;
  triggered_at: string;
}

export const createReleaseBranch = (branch: string, source_branch: string = "main") =>
  request<ReleaseBranch>("/release/branches", {
    method: "POST",
    body: JSON.stringify({ branch, source_branch }),
  });

export const listReleaseBranches = (limit = 50, offset = 0) =>
  request<{ items: ReleaseBranch[]; total: number }>(
    `/release/branches?limit=${limit}&offset=${offset}`,
  );

// Source branches available for cutting a new release from.
// Filtered + intersected server-side: only `main` + real `release/*` branches
// present on every product sub-repo's remote.
export const listReleaseSourceBranches = () =>
  request<{ branches: string[] }>("/release/source-branches");

export interface TriggerBuildOptions {
  is_online_package?: boolean;             // default true
  upload_to_github_release?: boolean;      // default true
  skip_asc_upload?: boolean;               // global only, default false
  android_multi_channel_pack?: boolean;    // cn only, default true
  description?: string;
}

export const triggerReleaseBuild = (
  branch: string,
  target: "cn" | "global",
  options: TriggerBuildOptions = {},
) =>
  request<ReleaseBuild>("/release/builds", {
    method: "POST",
    body: JSON.stringify({ branch, target, ...options }),
  });

export const listReleaseBuilds = (
  filters?: { branch?: string; target?: string; status?: string },
  limit = 50,
  offset = 0,
) => {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  if (filters?.branch) params.set("branch", filters.branch);
  if (filters?.target) params.set("target", filters.target);
  if (filters?.status) params.set("status", filters.status);
  return request<{ items: ReleaseBuild[] }>(`/release/builds?${params.toString()}`);
};

export const releaseArtifactUrl = (buildId: number, platform: "android" | "ios") =>
  `${BASE}/release/builds/${buildId}/artifacts/${platform}`;

// ============================================================
// Repo Routing (源码仓库路由)
// ============================================================

export interface RepoBand {
  min_version: string;
  family: string;
  wrapper: string;
  sub: string;
  github_repo: string;
  symbol_profile: string;
}

export interface RepoRoutingConfig {
  routing: Record<string, { bands: RepoBand[] }>;
  service_filter: string;
  support_web: boolean;
  support_desktop: boolean;
}

export interface RepoRoutingPreviewResult {
  resolved: boolean;
  family?: string;
  platform?: string;
  sub_repo_path?: string;
  github_repo?: string;
  symbol_profile?: string;
  confidence?: "high" | "low";
  reason?: string;
}

export const getRepoRouting = () =>
  request<RepoRoutingConfig>("/settings/repo-routing");

export const updateRepoRouting = (body: { routing: Record<string, { bands: RepoBand[] }>; service_filter?: string; support_web?: boolean; support_desktop?: boolean }) =>
  request<{ ok: boolean }>("/settings/repo-routing", {
    method: "PUT",
    body: JSON.stringify(body),
  });

export const previewRepoRouting = (platform: string, version?: string) =>
  request<RepoRoutingPreviewResult>("/settings/repo-routing/preview", {
    method: "POST",
    body: JSON.stringify({ platform, version }),
  });

// ============================================================
// Site Feedback
// ============================================================

export interface SiteFeedbackPayload {
  message: string;
  page_url: string | null;
  screenshot: string | null;
  user_email: string | null;
}

export async function submitSiteFeedback(payload: SiteFeedbackPayload): Promise<{ status: string; image_sent: boolean }> {
  const resp = await fetch(`${BASE}/site-feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) throw new Error(`feedback failed: ${resp.status}`);
  return resp.json();
}
