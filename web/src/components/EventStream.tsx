import type { StreamEvent } from "../types";
import { Empty } from "./ui";

const TYPE_LABELS: Record<string, string> = {
  progress: "progress",
  scan_done: "scan_done",
  intent_verified: "intent_verified",
  issue_verified: "issue_verified",
  validation_done: "validation_done",
  review_done: "review_done",
  fix_done: "fix_done",
  fix_baseline_failed: "fix_failed",
  merge_gate_done: "merge_gate",
  interrupt: "interrupt",
  done: "done",
  error: "error",
};

export function EventStream({ events }: { events: StreamEvent[] }) {
  if (!events.length) {
    return <Empty title="暂无实时事件" hint="任务运行时会在这里滚动输出节点事件" />;
  }
  return (
    <div className="event-stream">
      {[...events].reverse().map((event) => {
        const body = Object.entries(event.data)
          .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : String(value)}`)
          .join(" ");
        return (
          <div key={`${event.at}-${event.id}`} className="event-row">
            <span className="ts">
              {new Date(event.at).toLocaleTimeString("zh-CN", { hour12: false })}
            </span>
            <span className="type" style={event.type === "error" ? { color: "var(--red)" } : event.type === "done" ? { color: "var(--accent)" } : undefined}>
              {TYPE_LABELS[event.type] || event.type}
            </span>
            <span className="body" title={body}>{body || "—"}</span>
          </div>
        );
      })}
    </div>
  );
}
