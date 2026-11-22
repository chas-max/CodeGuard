import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { formatClock, formatDuration, relativeTime, useAsync, useJobStream } from "../hooks";
import { navigate } from "../router";
import type { JobSummary, StreamEvent, TraceStep } from "../types";
import { FlowStrip } from "../components/FlowStrip";
import { HitlPanel } from "../components/HitlPanel";
import { NodeResult } from "../components/NodeResult";
import { TraceTimeline } from "../components/TraceTimeline";
import { EventStream } from "../components/EventStream";
import { Alert, Empty, JsonBlock, OutcomePill, Pill, Spinner, StatusPill } from "../components/ui";
import { ExternalIcon, RefreshIcon } from "../components/icons";

const NODE_LABELS: Record<string, string> = {
  prepare_context: "前置准备",
  pr_reader: "PR 理解",
  dispatch_reviewers: "并行分发",
  review_logic: "逻辑审查",
  review_security: "安全审查",
  review_architecture: "架构审查",
  review_tests: "测试审查",
  synthesize_findings: "规则综合",
  coder_agent: "代码修复",
  qa_checker: "QA 对齐",
  verify_gate: "前置验证",
  summary_and_submit: "总结提交",
  merge_gate: "合并门控",
  human_approval: "人工审批",
  merge: "合并",
  persist_memory_and_finalize: "记忆落库",
  ingest: "接入",
  preflight: "预检",
  hybrid_scan: "混合检测",
  verify_issue: "意图/复现验证",
  validate: "沙箱验证",
  review: "审查",
  fix: "修复",
  finalize: "收尾",
};

export function JobDetailPage({ jobId }: { jobId: string }) {
  const [selected, setSelected] = useState("");
  const [tab, setTab] = useState<"result" | "raw">("result");
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const lastFetch = useRef(0);

  const snapshot = useAsync(() => api.snapshot(jobId), [jobId]);
  const trace = useAsync(() => api.trace(jobId), [jobId]);
  const jobs = useAsync(() => api.jobs(), []);

  const values = snapshot.data?.snapshot.values;
  const done = Boolean(snapshot.data?.snapshot.done);
  const interrupt = snapshot.data?.snapshot.interrupt;
  const steps: TraceStep[] = trace.data?.steps || [];
  const running = Boolean(values) && !done;

  const reloadAll = () => {
    snapshot.reload();
    trace.reload();
  };

  useJobStream(jobId, Boolean(snapshot.data) && !done, (event) => {
    setEvents((prev) => [event, ...prev].slice(0, 150));
    const terminal = [
      "prepare_done", "pr_reader_done", "synthesize_done", "coder_done",
      "qa_check_done", "verify_gate_done", "summary_submitted",
      "scan_done", "intent_verified", "issue_verified", "validation_done",
      "review_done", "fix_done", "merge_gate_done", "interrupt", "done", "error",
    ].includes(event.type);
    const now = Date.now();
    if (terminal || now - lastFetch.current > 1500) {
      lastFetch.current = now;
      reloadAll();
    }
  });

  // SSE 断线兜底：运行中的任务每 6s 主动同步一次。
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(reloadAll, 6000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, jobId]);

  useEffect(() => {
    if (selected || !steps.length) return;
    setSelected(steps[steps.length - 1].node);
  }, [steps, selected]);

  const job: JobSummary | undefined = (jobs.data?.jobs || []).find((item) => item.job_id === jobId);
  const currentStep = steps.filter((step) => step.node === selected).slice(-1)[0];
  const headline = values
    ? `${values.repo || job?.repo || ""}#${values.pr_number || job?.pr_number || ""}`
    : job
      ? `${job.repo}#${job.pr_number}`
      : jobId;

  if (snapshot.loading && !snapshot.data) {
    return <div className="card"><div className="card-body" style={{ padding: 40, textAlign: "center" }}><Spinner label="加载任务快照" /></div></div>;
  }

  if (snapshot.error || trace.error) {
    return (
      <div className="section">
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate("/jobs")}>← 返回任务列表</button>
        <Alert tone="error">{snapshot.error || trace.error}</Alert>
      </div>
    );
  }

  return (
    <div className="section">
      <div className="page-head" style={{ marginBottom: 4 }}>
        <div className="grow">
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate("/jobs")}>← 任务</button>
            <span className="mono" style={{ fontSize: 11.5, color: "var(--muted)" }}>{jobId}</span>
            {job?.source ? <Pill tone="muted">{job.source}</Pill> : null}
          </div>
          <h1 style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <span className="mono" style={{ color: "var(--muted)", fontSize: 17 }}>{headline}</span>
            {values?.pr_title || job?.title || ""}
          </h1>
          <p>
            {values?.author ? `${values.author} · ` : ""}
            {values?.profile ? `profile=${values.profile} · ` : ""}
            {values ? `开始 ${relativeTime(values.started_at)}` : ""}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {job ? (job.outcome ? <OutcomePill outcome={job.outcome} /> : <StatusPill status={job.status} />) : null}
          {values?.risk_tier?.tier ? <Pill tone={values.risk_tier.tier === "T2" ? "danger" : "warn"}>{values.risk_tier.tier}</Pill> : null}
          {!done ? <Pill tone="info" dot>实时追踪中</Pill> : null}
          {values?.repo && values?.pr_number ? (
            <a
              className="btn btn-sm"
              href={`https://github.com/${values.repo}/pull/${values.pr_number}`}
              target="_blank"
              rel="noreferrer"
            >
              <ExternalIcon width={13} height={13} /> GitHub
            </a>
          ) : null}
          <button type="button" className="btn btn-sm" onClick={reloadAll}>
            <RefreshIcon width={13} height={13} /> 刷新
          </button>
        </div>
      </div>

      {!values ? (
        <div className="card">
          <Empty
            title="没有可用的检查点"
            hint={job ? `该任务只有报告记录：${job.progress?.status_text || "—"}` : "任务可能已失效或 job_id 不存在"}
          />
        </div>
      ) : (
        <>
          <FlowStrip
            steps={steps}
            current={trace.data?.current || ""}
            interrupt={Boolean(interrupt)}
            done={done}
            failed={done && !values.outcome}
            selected={selected}
            onSelect={(node) => setSelected(node)}
          />

          {!done && !interrupt ? (
            <div className="card" style={{ padding: "10px 14px", display: "flex", alignItems: "center", gap: 12 }}>
              <Spinner />
              <span style={{ fontSize: 13 }}>{snapshot.data?.snapshot.progress.status_text || "执行中"}</span>
              <span style={{ flex: 1 }} />
              <span className="mono" style={{ fontSize: 12, color: "var(--muted)" }}>
                {snapshot.data?.snapshot.progress.percentage ?? 0}%
              </span>
              <div className="progress-line" style={{ width: 180 }}>
                <i style={{ width: `${snapshot.data?.snapshot.progress.percentage ?? 0}%` }} />
              </div>
            </div>
          ) : null}

          {interrupt ? (
            <HitlPanel
              jobId={jobId}
              snapshot={snapshot.data!.snapshot}
              onSubmitted={() => {
                snapshot.reload();
                trace.reload();
              }}
            />
          ) : null}

          <div className="grid detail">
            <div className="section" style={{ gap: 12 }}>
              <div className="card">
                <div className="card-head">
                  <h2>执行轨迹</h2>
                  <span className="sub">{steps.length} 步</span>
                  <span className="spacer" />
                  <span className="sub">快照 {trace.data?.tracked_snapshots ?? 0}</span>
                </div>
                <div className="card-body tight">
                  <TraceTimeline
                    steps={steps}
                    selected={selected}
                    onSelect={setSelected}
                    interrupt={Boolean(interrupt)}
                    failed={done && !values.outcome}
                  />
                </div>
              </div>

              <div className="card">
                <div className="card-head">
                  <h2>实时事件</h2>
                  <span className="spacer" />
                  <span className="sub">SSE</span>
                </div>
                <EventStream events={events} />
              </div>
            </div>

            <div className="card detail-panel">
              <div className="card-head">
                <div className="detail-head">
                  <h2>{NODE_LABELS[selected] || selected || "节点结果"}</h2>
                  {currentStep ? (
                    <>
                      <span className="desc">{currentStep.description}</span>
                      <Pill tone={currentStep.running ? "info" : "ok"}>
                        {currentStep.running ? "进行中" : `耗时 ${formatDuration(currentStep.duration_ms)}`}
                      </Pill>
                      <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>
                        {formatClock(currentStep.started_at)} → {currentStep.finished_at ? formatClock(currentStep.finished_at) : "…"}
                      </span>
                    </>
                  ) : null}
                </div>
                <span className="spacer" />
                <div className="segmented">
                  <button type="button" className={tab === "result" ? "active" : ""} onClick={() => setTab("result")}>结果</button>
                  <button type="button" className={tab === "raw" ? "active" : ""} onClick={() => setTab("raw")}>状态</button>
                </div>
              </div>
              <div className="card-body">
                {!selected ? (
                  <Empty title="选择一个节点" hint="点击左侧时间线或上方流程链查看该节点产出" />
                ) : tab === "result" ? (
                  <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                    {currentStep?.changed_keys.length ? (
                      <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
                        {currentStep.changed_keys.map((key) => <span key={key} className="tag">{key}</span>)}
                      </div>
                    ) : null}
                    <NodeResult node={selected} values={values} />
                  </div>
                ) : (
                  <JsonBlock value={selected === "prepare_context" || selected === "ingest" ? values : pickNodeState(selected, values)} maxHeight={560} />
                )}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/** 按节点挑选相关 state 键，避免整份 values（含 diff_text）刷屏。 */
function pickNodeState(node: string, values: Record<string, any>): Record<string, any> {
  const keys: Record<string, string[]> = {
    prepare_context: [
      "job_id", "repo", "pr_number", "pr_title", "author", "profile",
      "importance_level", "inherited_importance_level", "risk_tier", "policy",
      "code_partition", "changed_files", "diff_stats", "repo_map", "findings",
    ],
    pr_reader: [
      "pr_brief", "importance_level", "risk_tier", "claim",
      "issue_verification", "repro_tests", "reviewer_memories",
    ],
    dispatch_reviewers: ["review_round", "review_round_mode", "reviewer_memories"],
    review_logic: ["reviewer_outputs", "reviewer_memories"],
    review_security: ["reviewer_outputs", "reviewer_memories"],
    review_architecture: ["reviewer_outputs", "reviewer_memories"],
    review_tests: ["reviewer_outputs", "reviewer_memories"],
    synthesize_findings: ["findings", "blocking_findings", "fix_directive", "review_decision"],
    coder_agent: ["fix_round", "fix_files", "fix_patch", "fix_tree_hash", "fix_history"],
    qa_checker: ["qa_report", "qa_history", "escalation_reason"],
    verify_gate: ["validation_report", "verified_tree_hash", "reviewed_sha", "reviewed_tree_hash"],
    summary_and_submit: [
      "review_summary", "importance_level", "inherited_importance_level",
      "fix_pushed", "fix_remote_sha", "submitted_pr_number",
    ],
    merge_gate: ["merge_decision", "importance_level", "risk_tier", "human_approvals", "finding_outcomes", "reviewed_sha"],
    human_approval: ["human_approvals", "human_decision", "finding_outcomes", "risk_tier"],
    merge: ["merge_decision", "merge_commit_sha", "outcome", "error"],
    persist_memory_and_finalize: ["outcome", "status", "error", "memory_records_written", "run_record", "finished_at"],
    ingest: ["job_id", "repo", "pr_number", "pr_title", "author", "profile", "base_ref", "head_sha", "started_at"],
    preflight: ["risk_tier", "policy", "changed_files", "diff_stats", "repo_map", "review_guidance", "workspace_dir", "dry_run"],
    hybrid_scan: ["findings", "blocking_findings", "hybrid_scan_report", "semantic_unavailable"],
    verify_issue: ["claim", "issue_verification", "intent_verification", "repro_tests", "repro_evidence"],
    validate: ["validation_report", "reviewed_sha", "reviewed_tree_hash"],
    review: ["review_decision", "review_round", "review_round_mode", "escalation_reason"],
    fix: ["fix_round", "fix_files", "fix_pushed", "fix_remote_sha", "fix_tree_hash", "fix_patch"],
    finalize: ["outcome", "status", "error", "run_record", "finished_at"],
  };
  const picked: Record<string, any> = {};
  (keys[node] || Object.keys(values)).forEach((key) => {
    if (values[key] !== undefined) picked[key] = values[key];
  });
  return picked;
}
