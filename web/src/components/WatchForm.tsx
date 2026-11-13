import { useState } from "react";
import { api } from "../api";
import type { Watcher } from "../types";
import { Alert } from "./ui";

const INTERVALS = [
  { value: 60, label: "每 1 分钟" },
  { value: 300, label: "每 5 分钟" },
  { value: 900, label: "每 15 分钟" },
  { value: 3600, label: "每 1 小时" },
];

export function WatchForm({
  repo, onClose, onCreated,
}: { repo: string; onClose: () => void; onCreated: (watcher: Watcher) => void }) {
  const [target, setTarget] = useState(repo);
  const [interval, setIntervalSec] = useState(300);
  const [state, setState] = useState("open");
  const [autoReview, setAutoReview] = useState(false);
  const [dryRun, setDryRun] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    setError("");
    setBusy(true);
    try {
      const watcher = await api.createWatcher({
        repo: target.trim(),
        interval_sec: interval,
        state,
        auto_review: autoReview,
        dry_run: dryRun,
      });
      onCreated(watcher);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="overlay" onMouseDown={onClose}>
      <div className="modal" style={{ width: "min(520px, 100%)" }} onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <h3>开启定时查看</h3>
        </div>
        <div className="modal-body" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="field">
            <label>仓库</label>
            <input className="input" value={target} placeholder="owner/name" onChange={(event) => setTarget(event.target.value)} />
          </div>
          <div className="field-row">
            <div className="field">
              <label>轮询间隔</label>
              <select className="select" value={interval} onChange={(event) => setIntervalSec(Number(event.target.value))}>
                {INTERVALS.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>PR 状态</label>
              <select className="select" value={state} onChange={(event) => setState(event.target.value)}>
                <option value="open">open</option>
                <option value="all">all</option>
                <option value="closed">closed</option>
              </select>
            </div>
          </div>
          <label className="checkbox">
            <input type="checkbox" checked={autoReview} onChange={(event) => setAutoReview(event.target.checked)} />
            发现新 PR 或 head 变更时自动触发审查（每次轮询最多 3 个）
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={dryRun} onChange={(event) => setDryRun(event.target.checked)} />
            dry-run 演练（不推送修复、不合并）
          </label>
          {error ? <Alert tone="error">{error}</Alert> : null}
          <div style={{ fontSize: 11.5, color: "var(--muted)" }}>
            轮询只负责发现变化与入队，审查仍走既有的 LangGraph 流程；head SHA 未变化的 PR 不会重复触发。
          </div>
        </div>
        <div className="modal-foot">
          <button type="button" className="btn btn-ghost" onClick={onClose}>取消</button>
          <button type="button" className="btn btn-primary" disabled={busy || !target.includes("/")} onClick={submit}>
            {busy ? "创建中…" : "开始定时查看"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function WatchBadge({ watcher, now }: { watcher: Watcher; now: number }) {
  const next = new Date(watcher.next_poll_at).getTime();
  const seconds = Number.isNaN(next) ? 0 : Math.max(0, Math.round((next - now) / 1000));
  return (
    <span className="chip" style={{ color: "var(--accent)", borderColor: "rgba(53,214,164,0.3)" }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: "currentColor" }} />
      定时 {Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")} 后刷新
    </span>
  );
}
