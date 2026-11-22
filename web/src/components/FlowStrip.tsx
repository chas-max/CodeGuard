import type { TraceStep } from "../types";

const CHAIN = [
  { node: "prepare_context", label: "前置准备" },
  { node: "pr_reader", label: "PR理解" },
  { node: "dispatch_reviewers", label: "4路并行审" },
  { node: "synthesize_findings", label: "规则综合" },
  { node: "verify_gate", label: "前置验证" },
  { node: "summary_and_submit", label: "总结提交" },
  { node: "merge_gate", label: "门控" },
  { node: "human_approval", label: "人工" },
  { node: "merge", label: "合并" },
  { node: "persist_memory_and_finalize", label: "记忆收尾" },
];

const PARALLEL_REVIEWERS = [
  "review_logic",
  "review_security",
  "review_architecture",
  "review_tests",
];

type NodeState = "done" | "active" | "waiting" | "failed" | "pending";

function shortDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`;
}

export function FlowStrip({
  steps, current, interrupt, done, failed, selected, onSelect,
}: {
  steps: TraceStep[];
  current: string;
  interrupt: boolean;
  done: boolean;
  failed: boolean;
  selected: string;
  onSelect: (node: string) => void;
}) {
  const byNode = new Map<string, TraceStep>();
  steps.forEach((step) => byNode.set(step.node, step));
  const coderSteps = steps.filter((step) => step.node === "coder_agent");
  const qaSteps = steps.filter((step) => step.node === "qa_checker");

  const stateOf = (node: string): NodeState => {
    if (node === "dispatch_reviewers") {
      const parallelSteps = PARALLEL_REVIEWERS
        .map((n) => byNode.get(n))
        .filter((s): s is TraceStep => Boolean(s));
      if (parallelSteps.some((s) => s.running) || (!done && PARALLEL_REVIEWERS.includes(current))) {
        return "active";
      }
      if (parallelSteps.length) {
        return "done";
      }
    }
    const step = byNode.get(node);
    if (step?.running) return "active";
    if (step) {
      const isLast = steps[steps.length - 1]?.node === node;
      if (failed && isLast && !done) return "failed";
      return "done";
    }
    if (interrupt && node === "human_approval") return "waiting";
    if (!done && current === node) return "active";
    return "pending";
  };

  return (
    <div className="card" style={{ padding: "12px 14px" }}>
      <div className="flow">
        {CHAIN.map((item, index) => {
          const state = stateOf(item.node);
          const step = byNode.get(item.node);
          const isParallelActive =
            item.node === "dispatch_reviewers" && PARALLEL_REVIEWERS.includes(selected);
          return (
            <div key={item.node} style={{ display: "flex", alignItems: "center" }}>
              {index > 0 ? <span className="flow-arrow">›</span> : null}
              <button
                type="button"
                className={`flow-node ${state}`}
                style={
                  selected === item.node || isParallelActive
                    ? { borderColor: "var(--accent)", boxShadow: "0 0 0 3px rgba(53,214,164,0.09)" }
                    : undefined
                }
                onClick={() => onSelect(item.node)}
                title={step?.summary || item.label}
              >
                <span className="fn-label">{item.label}</span>
                <span className="fn-meta">
                  {state === "waiting" ? "待介入" : state === "active" ? "进行中" : shortDuration(step?.duration_ms)}
                </span>
              </button>
            </div>
          );
        })}
        {coderSteps.length ? (
          <div style={{ display: "flex", alignItems: "center" }}>
            <span className="flow-arrow">↺</span>
            <button
              type="button"
              className={`flow-node ${selected === "coder_agent" ? "active" : "done"}`}
              style={{ borderStyle: "dashed", minWidth: 78 }}
              onClick={() => onSelect("coder_agent")}
              title="coder_agent 修复后经 qa_checker 与 verify_gate 再回流 4 路增量复审"
            >
              <span className="fn-label">修复 ×{coderSteps.length}</span>
              <span className="fn-meta">{shortDuration(coderSteps[coderSteps.length - 1]?.duration_ms)}</span>
            </button>
          </div>
        ) : null}
        {qaSteps.length ? (
          <div style={{ display: "flex", alignItems: "center" }}>
            <span className="flow-arrow">›</span>
            <button
              type="button"
              className={`flow-node ${selected === "qa_checker" ? "active" : "done"}`}
              style={{ borderStyle: "dashed", minWidth: 74 }}
              onClick={() => onSelect("qa_checker")}
              title="qa_checker 校验 Coder diff 与 FixDirective 主题对齐"
            >
              <span className="fn-label">QA ×{qaSteps.length}</span>
              <span className="fn-meta">{shortDuration(qaSteps[qaSteps.length - 1]?.duration_ms)}</span>
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
