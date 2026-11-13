import type { TraceStep } from "../types";
import { formatClock, formatDuration } from "../hooks";
import { Empty, Pill } from "./ui";

export function TraceTimeline({
  steps, selected, onSelect, interrupt, failed,
}: {
  steps: TraceStep[];
  selected: string;
  onSelect: (node: string) => void;
  interrupt: boolean;
  failed: boolean;
}) {
  if (!steps.length) {
    return <Empty title="暂无执行轨迹" hint="任务刚入队，等待第一个节点产出快照" />;
  }
  const lastNode = steps[steps.length - 1]?.node;
  return (
    <div className="timeline">
      {steps.map((step, index) => {
        const running = Boolean(step.running);
        const state = running
          ? "active"
          : failed && index === steps.length - 1 && step.node === lastNode
            ? "failed"
            : "done";
        return (
          <div
            key={`${step.node}-${index}`}
            className={`tl-item ${state} ${selected === step.node ? "selected" : ""}`}
          >
            <div className="tl-rail">
              <span className="tl-dot" />
              <span className="tl-line" />
            </div>
            <div className="tl-card" onClick={() => onSelect(step.node)}>
              <div className="tl-head">
                <span className="mono" style={{ fontSize: 10.5, color: "#5c687b" }}>
                  {String(step.index).padStart(2, "0")}
                </span>
                <span className="tl-name">{step.label}</span>
                {running ? <Pill tone="info">运行中</Pill> : null}
                {(step.tool_calls_count ?? 0) > 0 ? (
                  <Pill tone="info">工具 ×{step.tool_calls_count}</Pill>
                ) : null}
                {(step.mcp_calls_count ?? 0) > 0 ? (
                  <Pill tone="violet">MCP ×{step.mcp_calls_count}</Pill>
                ) : null}
                <span className="tl-time">
                  {formatClock(step.started_at)} · {formatDuration(step.duration_ms)}
                </span>
              </div>
              <div className="tl-summary">{step.summary || step.description}</div>
              {step.next.length ? (
                <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginTop: 5 }}>
                  {step.changed_keys.slice(0, 6).map((key) => (
                    <span key={key} className="tag">
                      {key}
                    </span>
                  ))}
                  {step.changed_keys.length > 6 ? (
                    <span className="tag">+{step.changed_keys.length - 6}</span>
                  ) : null}
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
      {interrupt && steps[steps.length - 1]?.node !== "human_approval" ? (
        <div className="tl-item waiting">
          <div className="tl-rail">
            <span className="tl-dot" />
            <span className="tl-line" />
          </div>
          <div className="tl-card" style={{ cursor: "default" }}>
            <div className="tl-head">
              <span className="tl-name">等待人工审批</span>
              <span className="tl-time">已中断，等待回答</span>
            </div>
            <div className="tl-summary">在右侧面板提交审批结论后流程继续</div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
