/** 后端 API 封装。开发态由 vite proxy 转发，生产态与 FastAPI 同源。 */
import type {
  ConsoleConfig, GithubAppStatus, JobSummary, McpStatusResponse, PullsResponse, ReposResponse,
  Snapshot, TraceResponse, Watcher,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  const text = await resp.text();
  let payload: any = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!resp.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : payload;
    throw new ApiError(
      typeof detail === "string" ? detail : `请求失败（HTTP ${resp.status}）`,
      resp.status,
    );
  }
  return payload as T;
}

export const api = {
  config: () => request<ConsoleConfig>("/api/config"),
  mcpStatus: (probe = false) => request<McpStatusResponse>(`/api/mcp/status${probe ? "?probe=1" : ""}`),
  health: () => request<Record<string, any>>("/api/health"),
  repos: (q = "") => request<ReposResponse>(`/api/repos${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  pulls: (repo: string, state = "open") =>
    request<PullsResponse>(`/api/pulls?repo=${encodeURIComponent(repo)}&state=${state}`),
  jobs: (repo = "") =>
    request<{ jobs: JobSummary[]; total: number }>(`/api/jobs${repo ? `?repo=${encodeURIComponent(repo)}` : ""}`),
  snapshot: (jobId: string) => request<{ snapshot: Snapshot }>(`/api/jobs/${jobId}`),
  trace: (jobId: string) => request<TraceResponse>(`/api/jobs/${jobId}/trace`),
  review: (body: Record<string, unknown>) =>
    request<{ job_id: string }>("/api/reviews", { method: "POST", body: JSON.stringify(body) }),
  resume: (
    jobId: string,
    body: { approved: boolean; approver: string; feedback: string; finding_outcomes: Record<string, string> },
  ) => request<{ ok: boolean }>(`/api/jobs/${jobId}/resume`, { method: "POST", body: JSON.stringify(body) }),
  watchers: () => request<{ watchers: Watcher[] }>("/api/watch"),
  createWatcher: (body: {
    repo: string; interval_sec: number; state: string;
    auto_review: boolean; dry_run: boolean; profile?: string;
  }) => request<Watcher>("/api/watch", { method: "POST", body: JSON.stringify(body) }),
  patchWatcher: (id: string, body: Record<string, unknown>) =>
    request<Watcher>(`/api/watch/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  pollWatcher: (id: string) => request<Watcher>(`/api/watch/${id}/poll`, { method: "POST" }),
  deleteWatcher: (id: string) => request<{ ok: boolean }>(`/api/watch/${id}`, { method: "DELETE" }),
  githubApp: () => request<{ status: GithubAppStatus }>("/api/github/app"),
  githubAppManifest: (body: { name: string; public_url: string }) =>
    request<{ action: string; state: string; manifest: Record<string, unknown>; public_url: string }>(
      "/api/github/app/manifest",
      { method: "POST", body: JSON.stringify(body) },
    ),
  githubAppRefresh: () =>
    request<{ status: GithubAppStatus }>("/api/github/app/refresh", { method: "POST" }),
  githubAppDisconnect: (confirm: string) =>
    request<{ ok: boolean; status: GithubAppStatus }>("/api/github/app", {
      method: "DELETE",
      body: JSON.stringify({ confirm }),
    }),
};
