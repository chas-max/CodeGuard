import { useState } from "react";
import { api } from "../api";
import { formatClock, relativeTime, useAsync, useNow } from "../hooks";
import { navigate } from "../router";
import { WatchBadge, WatchForm } from "../components/WatchForm";
import { Alert, Empty, Pill, Spinner } from "../components/ui";
import { ClockIcon, PauseIcon, PlayIcon, RefreshIcon } from "../components/icons";

export function WatchersPage({ repo }: { repo: string }) {
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState("");
  const now = useNow(1000);
  const watchers = useAsync(() => api.watchers(), [], 5000);

  const act = async (id: string, action: "poll" | "delete") => {
    setBusy(id);
    setError("");
    try {
      if (action === "poll") await api.pollWatcher(id);
      else await api.deleteWatcher(id);
      watchers.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const toggleAuto = async (id: string, value: boolean) => {
    setBusy(id);
    try {
      await api.patchWatcher(id, { auto_review: value });
      watchers.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const list = watchers.data?.watchers || [];

  return (
    <div className="section">
      <div className="page-head">
        <div className="grow">
          <h1>定时查看</h1>
          <p>按固定间隔拉取 PR 列表，发现新的 head SHA 时可自动入队审查。配置持久化在 <span className="mono">data/jobs/watchers.json</span>。</p>
        </div>
        <button type="button" className="btn" onClick={watchers.reload}>
          <RefreshIcon width={14} height={14} /> 刷新
        </button>
        <button type="button" className="btn btn-primary" onClick={() => setShowForm(true)}>
          <ClockIcon width={14} height={14} /> 新建定时任务
        </button>
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}

      {watchers.loading && !watchers.data ? (
        <div className="card"><div className="card-body" style={{ padding: 36, textAlign: "center" }}><Spinner label="加载轮询配置" /></div></div>
      ) : null}

      {!watchers.loading && !list.length ? (
        <div className="card">
          <Empty
            title="还没有定时查看任务"
            hint="选择仓库与间隔后，后台会周期拉取 PR，并在 head 变化时触发审查"
            action={<button type="button" className="btn btn-primary" onClick={() => setShowForm(true)}>创建第一个</button>}
          />
        </div>
      ) : null}

      {list.map((watcher) => (
        <div key={watcher.id} className="card">
          <div className="card-head">
            <h2 className="mono" style={{ fontSize: 14 }}>{watcher.repo}</h2>
            <Pill tone="muted">{watcher.state}</Pill>
            <Pill tone={watcher.auto_review ? "ok" : "muted"}>{watcher.auto_review ? "自动审查" : "仅发现"}</Pill>
            {watcher.dry_run ? <Pill tone="violet">dry-run</Pill> : null}
            <span style={{ marginLeft: "auto" }} />
            <WatchBadge watcher={watcher} now={now} />
          </div>
          <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div className="grid cols-4" style={{ gap: 10 }}>
              <div>
                <div style={{ fontSize: 11.5, color: "var(--muted)" }}>间隔</div>
                <div className="mono" style={{ fontSize: 13 }}>{Math.round(watcher.interval_sec / 60)} 分钟</div>
              </div>
              <div>
                <div style={{ fontSize: 11.5, color: "var(--muted)" }}>已轮询</div>
                <div className="mono" style={{ fontSize: 13 }}>{watcher.polls} 次</div>
              </div>
              <div>
                <div style={{ fontSize: 11.5, color: "var(--muted)" }}>跟踪 PR</div>
                <div className="mono" style={{ fontSize: 13 }}>{watcher.tracked} 个</div>
              </div>
              <div>
                <div style={{ fontSize: 11.5, color: "var(--muted)" }}>上次</div>
                <div className="mono" style={{ fontSize: 12.5 }}>
                  {watcher.last_poll_at ? `${formatClock(watcher.last_poll_at)} · ${relativeTime(watcher.last_poll_at, now)}` : "—"}
                </div>
              </div>
            </div>

            {watcher.last_error ? <Alert tone="error">轮询错误：{watcher.last_error}</Alert> : null}

            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button type="button" className="btn btn-sm" disabled={busy === watcher.id} onClick={() => act(watcher.id, "poll")}>
                <PlayIcon width={12} height={12} /> 立即检查
              </button>
              <button
                type="button"
                className="btn btn-sm"
                disabled={busy === watcher.id}
                onClick={() => toggleAuto(watcher.id, !watcher.auto_review)}
              >
                {watcher.auto_review ? <PauseIcon width={12} height={12} /> : <PlayIcon width={12} height={12} />}
                {watcher.auto_review ? "改为仅发现" : "开启自动审查"}
              </button>
              <button type="button" className="btn btn-sm btn-danger" disabled={busy === watcher.id} onClick={() => act(watcher.id, "delete")}>
                停止并删除
              </button>
            </div>

            {watcher.detected.length ? (
              <div style={{ border: "1px solid var(--line-soft)", borderRadius: 11, overflow: "hidden" }}>
                <div className="subcard-head" style={{ borderBottom: "1px solid var(--line-soft)" }}>
                  最近一次发现的 PR
                  <span style={{ marginLeft: "auto", fontSize: 11.5, color: "var(--muted)" }}>{watcher.detected.length} 个</span>
                </div>
                <div className="detected">
                  {watcher.detected.slice(0, 10).map((item) => (
                    <div key={item.number} className="detected-item">
                      <span className="no">#{item.number}</span>
                      <span className="t">{item.title || "（无标题）"}</span>
                      {item.draft ? <Pill tone="muted">draft</Pill> : null}
                      {item.changed ? <Pill tone="info">head 变化</Pill> : null}
                      {item.triggered ? <Pill tone="ok">已入队</Pill> : null}
                      {item.job && "job_id" in (item.job as any) ? (
                        <button
                          type="button"
                          className="btn btn-sm btn-ghost"
                          onClick={() => navigate(`/jobs/${(item.job as any).job_id}`)}
                        >
                          追踪
                        </button>
                      ) : null}
                      <a className="mono" style={{ fontSize: 11, color: "var(--muted)" }} href={item.html_url} target="_blank" rel="noreferrer">
                        {item.head_sha.slice(0, 8)}
                      </a>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div style={{ fontSize: 12, color: "var(--muted)" }}>该轮询还没有发现 PR（或上一次轮询失败）。</div>
            )}
          </div>
        </div>
      ))}

      {showForm ? (
        <WatchForm
          repo={repo}
          onClose={() => setShowForm(false)}
          onCreated={() => watchers.reload()}
        />
      ) : null}
    </div>
  );
}
