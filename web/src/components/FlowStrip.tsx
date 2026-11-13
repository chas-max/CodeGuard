import type { TraceStep } from "../types";

const CHAIN = [
  { node: "ingest", label: "接入" },
  { node: "preflight", label: "预检" },
  { node: "hybrid_scan", label: "混合检测" },
  { node: "verify_issue", label: "验证" },
  { node: "validate", label: "沙箱" },
  { node: "review", label: "审查" },
  { node: "merge_gate", label: "门控" },
  { node: "human_approval", label: "人工" },
  { node: "merge", label: "合并" },
  { node: "finalize", label: "收尾" },
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
  const fixSteps = steps.filter((step) => step.node === "fix");

  const stateOf = (node: string): NodeState => {
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
          return (
            <div key={item.node} style={{ display: "flex", alignItems: "center" }}>
              {index > 0 ? <span className="flow-arrow">›</span> : null}
              <button
                type="button"
                className={`flow-node ${state}`}
                style={selected === item.node ? { borderColor: "var(--accent)", boxShadow: "0 0 0 3px rgba(53,214,164,0.09)" } : undefined}
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
        {fixSteps.length ? (
          <div style={{ display: "flex", alignItems: "center" }}>
            <span className="flow-arrow">↺</span>
            <button
              type="button"
              className={`flow-node ${selected === "fix" ? "active" : "done"}`}
              style={{ borderStyle: "dashed", minWidth: 74 }}
              onClick={() => onSelect("fix")}
              title="fix 会回到 hybrid_scan 重新检测"
            >
              <span className="fn-label">修复 ×{fixSteps.length}</span>
              <span className="fn-meta">{shortDuration(fixSteps[fixSteps.length - 1]?.duration_ms)}</span>
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
