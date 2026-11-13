import { useState, type ReactNode } from "react";
import { CopyIcon } from "./icons";

export type Tone = "ok" | "warn" | "danger" | "info" | "violet" | "muted";

export function Pill({ tone = "muted", children, dot }: { tone?: Tone; children: ReactNode; dot?: boolean }) {
  return (
    <span className={`pill ${tone}`}>
      {dot ? <i className="dot" /> : null}
      {children}
    </span>
  );
}

const OUTCOME_LABELS: Record<string, { text: string; tone: Tone }> = {
  MERGE_READY: { text: "门控通过", tone: "ok" },
  MERGED: { text: "已合并", tone: "ok" },
  REJECTED_NO_ISSUE: { text: "未复现问题", tone: "muted" },
  REJECTED_OUT_OF_SCOPE: { text: "超出范围", tone: "muted" },
  CHANGES_REQUESTED: { text: "需要修改", tone: "warn" },
  BLOCKED_HUMAN: { text: "待人工确认", tone: "warn" },
  INFRA_FAILED: { text: "基础设施异常", tone: "danger" },
  STALE: { text: "base 已移动", tone: "warn" },
  FAILED: { text: "失败", tone: "danger" },
};

const STATUS_LABELS: Record<string, { text: string; tone: Tone }> = {
  queued: { text: "排队中", tone: "muted" },
  running: { text: "运行中", tone: "info" },
  waiting_human: { text: "待人工介入", tone: "warn" },
  done: { text: "已完成", tone: "ok" },
  failed: { text: "失败", tone: "danger" },
};

const SEVERITY_LABELS: Record<string, { text: string; tone: Tone }> = {
  critical: { text: "critical", tone: "danger" },
  major: { text: "major", tone: "warn" },
  minor: { text: "minor", tone: "info" },
  info: { text: "info", tone: "muted" },
};

export function outcomeMeta(outcome: string | null | undefined) {
  if (!outcome) return { text: "—", tone: "muted" as Tone };
  return OUTCOME_LABELS[outcome] ?? { text: outcome, tone: "muted" as Tone };
}

export function OutcomePill({ outcome, status }: { outcome?: string | null; status?: string }) {
  if (!outcome) {
    const meta = STATUS_LABELS[status || ""] ?? { text: status || "—", tone: "muted" as Tone };
    return <Pill tone={meta.tone} dot>{meta.text}</Pill>;
  }
  const meta = outcomeMeta(outcome);
  return <Pill tone={meta.tone} dot>{meta.text}</Pill>;
}

export function StatusPill({ status }: { status: string }) {
  const meta = STATUS_LABELS[status] ?? { text: status, tone: "muted" as Tone };
  return <Pill tone={meta.tone} dot>{meta.text}</Pill>;
}

export function SeverityPill({ severity, blocking }: { severity: string; blocking?: boolean }) {
  const meta = SEVERITY_LABELS[severity] ?? { text: severity, tone: "muted" as Tone };
  return (
    <Pill tone={blocking ? "danger" : meta.tone}>
      {meta.text}
      {blocking ? " · blocking" : ""}
    </Pill>
  );
}

export function Stat({
  label, value, hint, tone,
}: { label: ReactNode; value: ReactNode; hint?: ReactNode; tone?: "accent" | "warn" | "info" | "danger" }) {
  return (
    <div className={`stat ${tone || ""}`}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {hint ? <span className="stat-hint">{hint}</span> : null}
    </div>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="emoji">◎</div>
      <div>{title}</div>
      {hint ? <div style={{ fontSize: 12 }}>{hint}</div> : null}
      {action}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <span className="spinner" />
      {label ? <span style={{ fontSize: 12.5, color: "var(--muted)" }}>{label}</span> : null}
    </span>
  );
}

export function Alert({ tone, children }: { tone: "error" | "warn" | "info"; children: ReactNode }) {
  return <div className={`alert ${tone}`}>{children}</div>;
}

export function CopyButton({ text, label = "复制" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-sm btn-ghost"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          window.setTimeout(() => setDone(false), 1200);
        } catch {
          setDone(false);
        }
      }}
    >
      <CopyIcon width={13} height={13} />
      {done ? "已复制" : label}
    </button>
  );
}

export function JsonBlock({ value, maxHeight }: { value: unknown; maxHeight?: number }) {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? null, null, 2);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <CopyButton text={text} />
      </div>
      <pre className="code" style={maxHeight ? { maxHeight } : undefined}>{text}</pre>
    </div>
  );
}

export function DiffBlock({ text, maxHeight = 460 }: { text: string; maxHeight?: number }) {
  const lines = (text || "").split("\n");
  return (
    <pre className="code" style={{ maxHeight }}>
      {lines.map((line, index) => {
        const cls = line.startsWith("@@")
          ? "hunk"
          : line.startsWith("+") && !line.startsWith("+++")
            ? "add"
            : line.startsWith("-") && !line.startsWith("---")
              ? "del"
              : "";
        return (
          <span key={index} className={`diff-line ${cls}`}>
            {line || " "}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}

export function KeyValue({ items }: { items: Array<[ReactNode, ReactNode]> }) {
  return (
    <dl className="kv">
      {items.map(([key, value], index) => (
        <div key={index} style={{ display: "contents" }}>
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function SubCard({
  title, extra, children,
}: { title: ReactNode; extra?: ReactNode; children: ReactNode }) {
  return (
    <div className="subcard">
      <div className="subcard-head">
        <span>{title}</span>
        <span className="spacer" />
        {extra}
      </div>
      <div className="subcard-body">{children}</div>
    </div>
  );
}
