import { useCallback, useEffect, useRef, useState } from "react";
import type { StreamEvent } from "./types";

export function errorText(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

interface AsyncState<T> {
  data: T | null;
  error: string;
  loading: boolean;
}

/** 轻量异步加载：deps 变化或手动 reload 时重新拉取，可选轮询。 */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[], intervalMs = 0) {
  const [state, setState] = useState<AsyncState<T>>({ data: null, error: "", loading: true });
  const [tick, setTick] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const reload = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    let alive = true;
    setState((prev) => ({ ...prev, loading: true }));
    loaderRef.current()
      .then((data) => {
        if (alive) setState({ data, error: "", loading: false });
      })
      .catch((err) => {
        if (alive) setState((prev) => ({ data: prev.data, error: errorText(err), loading: false }));
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  useEffect(() => {
    if (!intervalMs) return;
    const timer = window.setInterval(reload, intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, reload]);

  return { ...state, reload };
}

/** 每 intervalMs 触发一次重渲染，用于倒计时展示。 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

const REPO_KEY = "codeguard.repo";
const RECENT_KEY = "codeguard.recent_repos";

function readRecent(): string[] {
  try {
    const raw = window.localStorage.getItem(RECENT_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

let currentRepo = window.localStorage.getItem(REPO_KEY) || "";
const repoListeners = new Set<(value: string) => void>();

export function selectRepo(repo: string): void {
  currentRepo = repo;
  window.localStorage.setItem(REPO_KEY, repo);
  if (repo) {
    const recent = [repo, ...readRecent().filter((item) => item !== repo)].slice(0, 8);
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(recent));
  }
  repoListeners.forEach((listener) => listener(repo));
}

export function useRepo(): [string, (value: string) => void] {
  const [repo, setRepo] = useState(currentRepo);
  useEffect(() => {
    repoListeners.add(setRepo);
    return () => {
      repoListeners.delete(setRepo);
    };
  }, []);
  return [repo, selectRepo];
}

export function useRecentRepos(): string[] {
  const [repo] = useRepo();
  return [repo, ...readRecent().filter((item) => item !== repo)];
}

/** 订阅单任务 SSE：progress / 各节点完成事件 / interrupt / done / error。 */
export function useJobStream(jobId: string, enabled: boolean, onEvent: (event: StreamEvent) => void) {
  const callback = useRef(onEvent);
  callback.current = onEvent;

  useEffect(() => {
    if (!enabled || !jobId) return;
    const source = new EventSource(`/api/jobs/${jobId}/stream`);
    const types = [
      "progress", "scan_done", "intent_verified", "issue_verified", "validation_done",
      "review_done", "fix_done", "fix_baseline_failed", "merge_gate_done",
      "interrupt", "done", "error",
    ];
    let seq = 0;
    const handlers: Array<[string, EventListener]> = types.map((type) => {
      const handler = ((ev: MessageEvent) => {
        if (!ev.data) return;
        let data: Record<string, any> = {};
        try {
          data = JSON.parse(ev.data);
        } catch {
          data = { raw: String(ev.data) };
        }
        seq += 1;
        callback.current({ id: seq, type, at: Date.now(), data });
        if (type === "done" || type === "error") source.close();
      }) as EventListener;
      source.addEventListener(type, handler);
      return [type, handler];
    });
    return () => {
      handlers.forEach(([type, handler]) => source.removeEventListener(type, handler));
      source.close();
    };
  }, [jobId, enabled]);
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function formatClock(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

export function relativeTime(value: string | null | undefined, now = Date.now()): string {
  if (!value) return "—";
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return value;
  const diff = Math.round((time - now) / 1000);
  const abs = Math.abs(diff);
  const unit =
    abs < 60 ? `${abs} 秒` : abs < 3600 ? `${Math.round(abs / 60)} 分钟`
      : abs < 86400 ? `${Math.round(abs / 3600)} 小时` : `${Math.round(abs / 86400)} 天`;
  return diff >= 0 ? `${unit}后` : `${unit}前`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}
