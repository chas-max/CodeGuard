/** 与后端 `/api/*` 对齐的前端类型定义。values 来自 ReviewState，按用途局部收敛。 */

export type JobStatus = "queued" | "running" | "waiting_human" | "done" | "failed";

export interface RepoInfo {
  full_name: string;
  private: boolean | null;
  default_branch: string;
  description: string;
  html_url: string;
  language: string;
  pushed_at: string;
  open_issues: number | null;
}

export interface ReposResponse {
  repos: RepoInfo[];
  source: "github" | "config" | "app";
  message: string;
  app_slug?: string;
  installations?: number;
}

export interface GithubAppStatus {
  configured: boolean;
  app_id: number;
  slug: string;
  name: string;
  owner: string;
  html_url: string;
  install_url: string;
  installations: number;
  repos: number;
  webhook_configured: boolean;
  private_key_configured: boolean;
  last_error: string;
  mode?: string;
  auth?: "app" | "pat" | "none";
  pat_configured?: boolean;
  public_url?: string;
  webhook_url?: string;
}

export interface ConsoleConfig {
  github_configured: boolean;
  github_auth: "app" | "pat" | "none";
  github_login: string;
  github_app: GithubAppStatus;
  public_url: string;
  default_repos: string[];
  merge_mode: string;
  merge_method: string;
  review_mode: string;
  sandbox_backend: string;
  budget_max_cny: number;
  max_review_rounds: number;
  max_fix_rounds: number;
  post_merge_watch_hours: number;
  mcp_enabled?: boolean;
  tool_protocol?: string;
}

export interface ToolEventItem {
  role: string;
  tool: string;
  source?: "builtin" | "mcp" | string;
  server?: string;
  protocol?: string;
  args?: string;
  status: string;
  duration_ms: number;
  output_chars: number;
  error?: string;
}

export interface McpServerStatus {
  name: string;
  enabled: boolean;
  transport: string;
  ok: boolean;
  tools_count: number;
  tools: string[];
  error: string;
  capability: string;
  allowed_roles: string[];
  latency_ms: number;
}

export interface McpStatusResponse {
  enabled: boolean;
  max_capability: string;
  external_max_capability: string;
  tool_protocol: string;
  servers: McpServerStatus[];
}

export interface JobSummary {
  job_id: string;
  repo: string;
  pr_number: number;
  title: string;
  author: string;
  tier: string;
  source: string;
  status: JobStatus;
  outcome: string;
  progress: { status_text: string; percentage: number };
  started_at: string;
  finished_at: string;
  duration_sec: number | null;
  cost_cny: number;
  llm_calls: number;
  findings_total: number;
  findings_blocking: number;
  approvals: number;
  required_approvals: number;
  next: string[];
  requires_human: boolean;
  html_url: string;
  live: boolean;
}

export interface PullInfo {
  number: number;
  title: string;
  author: string;
  state: string;
  draft: boolean;
  merged: boolean;
  head_ref: string;
  base_ref: string;
  head_sha: string;
  html_url: string;
  created_at: string;
  updated_at: string;
  comments: number;
  labels: string[];
  job: JobSummary | null;
}

export interface PullsResponse {
  repo: string;
  state: string;
  pulls: PullInfo[];
  fetched_at: string;
}

export interface Finding {
  id: string;
  source: string;
  rule_id: string;
  category: string;
  severity: "critical" | "major" | "minor" | "info" | string;
  blocking: boolean;
  auto_fixable?: boolean;
  path: string;
  line: number;
  message: string;
  evidence: string;
  suggestion: string;
  lifecycle: string;
  confidence?: number;
  demoted_by?: string;
  group?: string;
}

export interface TraceStep {
  index: number;
  node: string;
  label: string;
  description: string;
  started_at: string;
  finished_at: string;
  duration_ms: number | null;
  summary: string;
  changed_keys: string[];
  next: string[];
  running?: boolean;
  tool_events?: ToolEventItem[];
  tool_calls_count?: number;
  mcp_calls_count?: number;
}

export interface TraceResponse {
  job_id: string;
  steps: TraceStep[];
  current: string;
  next: string[];
  interrupt: InterruptPayload | null;
  done: boolean;
  tracked_snapshots: number;
}

export interface InterruptPayload {
  type: string;
  pr: string;
  reason: string;
  tier: string;
  required_approvals: number;
  collected_approvals: string[];
  require_non_author: boolean;
  author: string;
  review: Record<string, unknown> | null;
  validation: Record<string, unknown> | null;
  diff: string;
}

export interface ReviewValues {
  job_id: string;
  repo: string;
  pr_number: number;
  pr_title: string;
  pr_body: string;
  author: string;
  profile: string;
  base_ref: string;
  base_sha: string;
  head_sha: string;
  started_at: string;
  finished_at: string;
  risk_tier: Record<string, any> | null;
  policy: Record<string, any>;
  diff_stats: Record<string, any>;
  repo_map: Record<string, any>;
  changed_files: any[];
  findings: Finding[];
  finding_history: Finding[];
  blocking_findings: Finding[];
  hybrid_scan_report: Record<string, any> | null;
  deterministic_report: Record<string, any> | null;
  semantic_report: Record<string, any> | null;
  semantic_unavailable: boolean;
  claim: Record<string, any> | null;
  issue_verification: Record<string, any> | null;
  intent_verification: Record<string, any> | null;
  repro_tests: string[];
  validation_report: Record<string, any> | null;
  review_decision: Record<string, any> | null;
  review_round: number;
  review_round_mode: string;
  fix_round: number;
  fix_patch: string;
  fix_files: string[];
  fix_pushed: boolean;
  fix_remote_sha: string;
  merge_decision: Record<string, any> | null;
  outcome: string | null;
  merge_commit_sha: string;
  requires_human: boolean;
  human_decision: Record<string, any> | null;
  human_approvals: any[];
  finding_outcomes: Record<string, string>;
  tool_events: any[];
  tool_calls: number;
  task_events: any[];
  events: any[];
  errors: any[];
  budget_events: any[];
  agent_issues: any[];
  open_issues: any[];
  llm_calls: number;
  status: string;
  error: string | null;
  run_record: Record<string, any>;
  [key: string]: any;
}

export interface Snapshot {
  job_id: string;
  values: ReviewValues;
  interrupt: InterruptPayload | null;
  next: string[];
  done: boolean;
  progress: { status_text: string; percentage: number };
}

export interface Watcher {
  id: string;
  repo: string;
  interval_sec: number;
  state: string;
  auto_review: boolean;
  dry_run: boolean;
  profile: string;
  created_at: string;
  last_poll_at: string;
  next_poll_at: string;
  last_error: string;
  polls: number;
  tracked: number;
  detected: DetectedPull[];
  running: boolean;
}

export interface DetectedPull {
  number: number;
  title: string;
  author: string;
  state: string;
  draft: boolean;
  head_sha: string;
  base_ref: string;
  html_url: string;
  updated_at: string;
  changed: boolean;
  job?: JobSummary | { job_id: string; status: string } | null;
  triggered?: boolean;
  trigger_error?: string;
}

export interface StreamEvent {
  id: number;
  type: string;
  at: number;
  data: Record<string, any>;
}
