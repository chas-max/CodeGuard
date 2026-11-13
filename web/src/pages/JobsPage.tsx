import { useState } from "react";
import { api } from "../api";
import { useAsync } from "../hooks";
import { JobTable } from "../components/JobTable";
import { Alert, Stat } from "../components/ui";
import { RefreshIcon } from "../components/icons";

type Filter = "all" | "running" | "waiting" | "done";

export function JobsPage({ repo }: { repo: string }) {
  const [scope, setScope] = useState<"repo" | "all">(repo ? "repo" : "all");
  const [filter, setFilter] = useState<Filter>("all");
  const jobs = useAsync(() => api.jobs(scope === "repo" ? repo : ""), [scope, repo], 8000);

  const list = jobs.data?.jobs || [];
  const filtered = list.filter((job) => {
    if (filter === "all") return true;
    if (filter === "running") return job.status === "running" || job.status === "queued";
    if (filter === "waiting") return job.status === "waiting_human";
    return job.status === "done" || job.status === "failed";
  });
  const merged = list.filter((job) => job.outcome === "MERGED").length;
  const blocked = list.filter((job) => job.status === "waiting_human").length;
  const cost = list.reduce((sum, job) => sum + Number(job.cost_cny || 0), 0);

  return (
    <div className="section">
      <div className="page-head">
        <div className="grow">
          <h1>任务</h1>
          <p>所有 PR 审查任务，包含历史报告与内存中的活动任务。</p>
        </div>
        <button type="button" className="btn" onClick={jobs.reload}>
          <RefreshIcon width={14} height={14} /> 刷新
        </button>
      </div>

      <div className="grid cols-4">
        <Stat label="任务总数" value={list.length} hint={scope === "repo" ? repo : "全部仓库"} />
        <Stat label="已合并" tone="accent" value={merged} hint="门控通过并完成 merge" />
        <Stat label="待人工" tone="warn" value={blocked} hint="HITL 中断中" />
        <Stat label="累计成本" value={`¥${cost.toFixed(2)}`} hint="按任务预算事件汇总" />
      </div>

      {jobs.error ? <Alert tone="error">{jobs.error}</Alert> : null}

      <div className="card">
        <div className="list-toolbar">
          <div className="segmented">
            <button type="button" className={scope === "repo" ? "active" : ""} onClick={() => setScope("repo")} disabled={!repo}>
              当前仓库
            </button>
            <button type="button" className={scope === "all" ? "active" : ""} onClick={() => setScope("all")}>
              全部
            </button>
          </div>
          <span style={{ flex: 1 }} />
          <div className="segmented">
            {([
              ["all", "全部"],
              ["running", "运行中"],
              ["waiting", "待人工"],
              ["done", "已完成"],
            ] as Array<[Filter, string]>).map(([value, label]) => (
              <button key={value} type="button" className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>
                {label}
              </button>
            ))}
          </div>
        </div>
        <JobTable jobs={filtered} empty={filter === "all" ? "暂无任务" : "没有符合筛选条件的任务"} />
      </div>
    </div>
  );
}
