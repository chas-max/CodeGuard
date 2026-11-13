import { useState } from "react";
import { api } from "../api";
import { useAsync, useNow, relativeTime, formatClock } from "../hooks";
import { navigate } from "../router";
import type { PullInfo, Watcher } from "../types";
import { JobTable } from "../components/JobTable";
import { WatchBadge, WatchForm } from "../components/WatchForm";
import { Alert, Empty, OutcomePill, Pill, Spinner, Stat, StatusPill } from "../components/ui";
import { BoltIcon, ClockIcon, ExternalIcon, PlayIcon, RefreshIcon, RouteIcon } from "../components/icons";

export function DashboardPage({ repo, onPickRepo }: { repo: string; onPickRepo: () => void }) {
  const [pullState, setPullState] = useState("open");
  const [showWatch, setShowWatch] = useState(false);
  const [busyPr, setBusyPr] = useState<number | null>(null);
  const [actionError, setActionError] = useState("");
  const now = useNow(1000);

  const pulls = useAsync(
    async () => (repo ? api.pulls(repo, pullState) : { repo: "", state: pullState, pulls: [], fetched_at: "" }),
    [repo, pullState],
    60000,
  );
  const jobs = useAsync(async () => (repo ? api.jobs(repo) : { jobs: [], total: 0 }), [repo], 0);
  const watchers = useAsync(() => api.watchers(), [], 5000);

  const repoWatchers = (watchers.data?.watchers || []).filter((item) => item.repo === repo);
  const jobList = jobs.data?.jobs || [];
  const running = jobList.filter((job) => job.status === "running" || job.status === "queued");
  const waiting = jobList.filter((job) => job.status === "waiting_human");
  const today = new Date().toISOString().slice(0, 10);
  const settledToday = jobList.filter((job) => (job.outcome || job.status === "done") && (job.started_at || "").slice(0, 10) === today);
  const cost = jobList.reduce((sum, job) => sum + Number(job.cost_cny || 0), 0);

  const trigger = async (pr: PullInfo) => {
    setActionError("");
    setBusyPr(pr.number);
    try {
      const result = await api.review({ repo, pr_number: pr.number });
      navigate(`/jobs/${result.job_id}`);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyPr(null);
    }
  };

  if (!repo) {
    return (
      <div className="section">
        <div className="page-head">
          <div className="grow">
            <h1>PR 看板</h1>
            <p>选择要接入的 GitHub 仓库，开始审查与追踪。</p>
          </div>
        </div>
        <div className="card">
          <div className="card-body" style={{ padding: 36, textAlign: "center" }}>
            <div style={{ display: "grid", placeItems: "center", gap: 14 }}>
              <div className="brand-mark" style={{ width: 52, height: 52, borderRadius: 16 }}>
                <BoltIcon width={24} height={24} />
              </div>
              <h2 style={{ margin: 0, fontSize: 18 }}>还没有选择仓库</h2>
              <p style={{ margin: 0, color: "var(--muted)", fontSize: 13, maxWidth: 520 }}>
                仓库选择会保存在本地。选定后会自动拉取 PR 列表、历史任务与执行链路；也可以用
                <code style={{ margin: "0 4px" }}>CODEGUARD_REPOS</code> 在后端预置可切换的仓库。
              </p>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", justifyContent: "center" }}>
                <button type="button" className="btn btn-primary" onClick={onPickRepo}>
                  选择 GitHub 仓库
                </button>
                <button type="button" className="btn btn-ghost" onClick={() => navigate("/settings")}>
                  连接 GitHub
                </button>
              </div>
              <p style={{ margin: 0, color: "var(--muted)", fontSize: 12 }}>
                连接 GitHub App 后会自动同步你授权给它的仓库，无需手工填环境变量。
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="section">
      <div className="page-head">
        <div className="grow">
          <h1>PR 看板</h1>
          <p>
            <span className="mono">{repo}</span> · 拉取于 {pulls.data ? formatClock(pulls.data.fetched_at) : "—"}
            {repoWatchers.length ? " · 定时查看中" : ""}
          </p>
        </div>
        <button type="button" className="btn" onClick={() => setShowWatch(true)}>
          <ClockIcon width={14} height={14} />
          {repoWatchers.length ? "调整定时查看" : "开启定时查看"}
        </button>
        <button type="button" className="btn" onClick={() => { pulls.reload(); jobs.reload(); watchers.reload(); }}>
          <RefreshIcon width={14} height={14} />
          刷新
        </button>
      </div>

      <div className="grid cols-4">
        <Stat label="运行中" tone="info" value={running.length} hint={running.length ? "LangGraph 执行中" : "当前空闲"} />
        <Stat label="待人工介入" tone="warn" value={waiting.length} hint={waiting.length ? "HITL 中断等待回答" : "无阻塞"} />
        <Stat label="今日完成" tone="accent" value={settledToday.length} hint={`累计 ${jobList.length} 个任务`} />
        <Stat label="累计成本" value={`¥${cost.toFixed(2)}`} hint="LLM 调用估算" />
      </div>

      {actionError ? <Alert tone="error">{actionError}</Alert> : null}

      <div className="card">
        <div className="list-toolbar">
          <strong style={{ fontSize: 14 }}>Pull Requests</strong>
          <span className="pill muted">{pulls.data?.pulls.length ?? 0} 个</span>
          {repoWatchers.map((watcher) => <WatchBadge key={watcher.id} watcher={watcher} now={now} />)}
          <span style={{ flex: 1 }} />
          <div className="segmented">
            {["open", "closed", "all"].map((value) => (
              <button
                key={value}
                type="button"
                className={pullState === value ? "active" : ""}
                onClick={() => setPullState(value)}
              >
                {value}
              </button>
            ))}
          </div>
        </div>

        {pulls.loading && !pulls.data ? (
          <div style={{ padding: 34, textAlign: "center" }}><Spinner label="拉取 PR 列表" /></div>
        ) : null}
        {pulls.error ? <div style={{ padding: 16 }}><Alert tone="error">{pulls.error}</Alert></div> : null}
        {!pulls.loading && pulls.data && !pulls.data.pulls.length ? (
          <Empty
            title={`${repo} 没有 ${pullState} 状态的 PR`}
            hint="确认 token 有 repo 读权限，或换一个状态筛选"
          />
        ) : null}

        <div className="pr-list">
          {(pulls.data?.pulls || []).map((pr) => (
            <div key={pr.number} className="pr-row">
              <div className="pr-title">
                <div className="line1">
                  <span className="no">#{pr.number}</span>
                  <a href={pr.html_url} target="_blank" rel="noreferrer">{pr.title || "（无标题）"}</a>
                  {pr.draft ? <Pill tone="muted">draft</Pill> : null}
                  {pr.merged ? <Pill tone="violet">merged</Pill> : null}
                  {pr.labels.slice(0, 2).map((label) => <span key={label} className="tag">{label}</span>)}
                </div>
                <div className="line2">
                  <span>{pr.author || "unknown"}</span>
                  <span className="mono">{pr.head_ref} → {pr.base_ref}</span>
                  <span>更新于 {relativeTime(pr.updated_at, now)}</span>
                </div>
              </div>

              <div className="pr-status">
                {pr.job ? (
                  <>
                    {pr.job.outcome ? <OutcomePill outcome={pr.job.outcome} /> : <StatusPill status={pr.job.status} />}
                    <div className="progress-line">
                      <i style={{ width: `${pr.job.progress?.percentage ?? 0}%` }} />
                    </div>
                  </>
                ) : (
                  <span className="pill muted">未审查</span>
                )}
              </div>

              <div style={{ fontSize: 11.5, color: "var(--muted)" }}>
                {pr.job ? (
                  <span className="mono">{pr.job.progress?.status_text || pr.job.job_id}</span>
                ) : (
                  <span>—</span>
                )}
              </div>

              <div className="pr-actions">
                {pr.job ? (
                  <button type="button" className="btn btn-sm" onClick={() => navigate(`/jobs/${pr.job!.job_id}`)}>
                    <RouteIcon width={13} height={13} />
                    追踪
                  </button>
                ) : null}
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  disabled={busyPr === pr.number}
                  onClick={() => trigger(pr)}
                >
                  <PlayIcon width={12} height={12} />
                  {busyPr === pr.number ? "入队中" : pr.job ? "重跑" : "审查"}
                </button>
                <a className="btn btn-sm btn-ghost btn-icon" href={pr.html_url} target="_blank" rel="noreferrer">
                  <ExternalIcon width={13} height={13} />
                </a>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <div className="card-head">
            <h2>最近任务</h2>
            <span className="spacer" />
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate("/jobs")}>全部任务</button>
          </div>
          <JobTable jobs={jobList.slice(0, 6)} empty="这个仓库还没有审查记录" />
        </div>

        <div className="card">
          <div className="card-head">
            <h2>定时查看</h2>
            <span className="spacer" />
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate("/watchers")}>管理</button>
          </div>
          <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {repoWatchers.length ? (
              repoWatchers.map((watcher) => (
                <div key={watcher.id} className="subcard">
                  <div className="subcard-body" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <Pill tone={watcher.auto_review ? "ok" : "muted"}>{watcher.auto_review ? "自动审查" : "仅发现"}</Pill>
                      <span className="mono" style={{ fontSize: 11.5, color: "var(--muted)" }}>
                        每 {Math.round(watcher.interval_sec / 60)} 分钟
                      </span>
                      <span style={{ marginLeft: "auto" }}><WatchBadge watcher={watcher} now={now} /></span>
                    </div>
                    <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
                      已轮询 {watcher.polls} 次 · 跟踪 {watcher.tracked} 个 PR · 上次 {relativeTime(watcher.last_poll_at, now)}
                    </div>
                    {watcher.last_error ? <Alert tone="error">{watcher.last_error}</Alert> : null}
                    {watcher.detected.filter((item) => item.changed).slice(0, 3).map((item) => (
                      <div key={item.number} className="mono" style={{ fontSize: 11.5, color: "var(--muted)" }}>
                        新变化 #{item.number} {item.title.slice(0, 40)}
                      </div>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <Empty
                title="未开启定时查看"
                hint="开启后会周期性拉取 PR，发现新 head 可自动入队审查"
                action={<button type="button" className="btn btn-sm btn-primary" onClick={() => setShowWatch(true)}>立即开启</button>}
              />
            )}
            <div className="alert info" style={{ marginTop: "auto" }}>
              轮询只处理 head SHA 变化；正在审查中的 PR 不会被重复入队。
            </div>
          </div>
        </div>
      </div>

      {showWatch ? (
        <WatchForm
          repo={repo}
          onClose={() => setShowWatch(false)}
          onCreated={(_watcher: Watcher) => { watchers.reload(); }}
        />
      ) : null}
    </div>
  );
}
