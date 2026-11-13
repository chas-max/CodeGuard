import type { JobSummary } from "../types";
import { formatDuration, relativeTime } from "../hooks";
import { navigate } from "../router";
import { Empty, OutcomePill, Pill, StatusPill } from "./ui";

export function JobTable({ jobs, empty }: { jobs: JobSummary[]; empty?: string }) {
  if (!jobs.length) return <Empty title={empty || "暂无任务"} hint="从 PR 看板触发一次审查后，这里会出现完整的执行记录" />;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>任务</th>
            <th>状态</th>
            <th>Tier</th>
            <th>Findings</th>
            <th>审批</th>
            <th>耗时</th>
            <th>成本</th>
            <th>开始</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.job_id} onClick={() => navigate(`/jobs/${job.job_id}`)}>
              <td>
                <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <span style={{ fontSize: 13 }}>
                    <span className="mono" style={{ color: "var(--muted)", marginRight: 6 }}>
                      {job.repo}#{job.pr_number}
                    </span>
                    {job.title || "（历史记录）"}
                  </span>
                  <span className="mono" style={{ fontSize: 10.5, color: "#5b6679" }}>
                    {job.job_id}
                    {job.source ? ` · ${job.source}` : ""}
                  </span>
                </div>
              </td>
              <td>
                {job.outcome ? <OutcomePill outcome={job.outcome} /> : <StatusPill status={job.status} />}
              </td>
              <td>{job.tier ? <Pill tone={job.tier === "T2" ? "danger" : job.tier === "T1" ? "warn" : "ok"}>{job.tier}</Pill> : "—"}</td>
              <td>
                <span className="mono" style={{ fontSize: 12 }}>
                  {job.findings_total}
                  {job.findings_blocking ? <span style={{ color: "var(--red)" }}> / {job.findings_blocking}B</span> : null}
                </span>
              </td>
              <td>
                <span className="mono" style={{ fontSize: 12, color: job.requires_human ? "var(--amber)" : "var(--muted)" }}>
                  {job.approvals}/{job.required_approvals}
                </span>
              </td>
              <td><span className="mono" style={{ fontSize: 11.5 }}>{formatDuration(job.duration_sec ? job.duration_sec * 1000 : null)}</span></td>
              <td><span className="mono" style={{ fontSize: 11.5 }}>¥{Number(job.cost_cny || 0).toFixed(2)}</span></td>
              <td><span style={{ fontSize: 11.5, color: "var(--muted)" }}>{relativeTime(job.started_at)}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
