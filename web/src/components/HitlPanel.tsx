import { useMemo, useState } from "react";
import { api } from "../api";
import type { Finding, Snapshot } from "../types";
import { DiffBlock, Alert, Pill } from "./ui";
import { CheckIcon, XIcon } from "./icons";

const OUTCOME_OPTIONS = [
  { value: "", label: "未处置" },
  { value: "resolved", label: "已修复" },
  { value: "accepted_risk", label: "接受风险" },
  { value: "false_positive", label: "误报" },
];

export function HitlPanel({
  jobId, snapshot, onSubmitted,
}: { jobId: string; snapshot: Snapshot; onSubmitted: () => void }) {
  const payload = snapshot.interrupt;
  const values = snapshot.values || ({} as Snapshot["values"]);
  const [approved, setApproved] = useState(true);
  const [approver, setApprover] = useState("");
  const [feedback, setFeedback] = useState("");
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tip, setTip] = useState("");

  const blockingFindings = useMemo(() => {
    const source = (values.blocking_findings?.length ? values.blocking_findings : values.findings || []) as Finding[];
    return source.filter((item) => item.blocking && (item.lifecycle || "open") === "open");
  }, [values]);

  if (!payload) return null;

  const review = (payload.review || {}) as Record<string, any>;
  const validation = (payload.validation || {}) as Record<string, any>;
  const collected = payload.collected_approvals || [];
  const missing = Math.max(0, (payload.required_approvals || 0) - collected.length);
  const selfApproval = payload.require_non_author && approver.trim() && approver.trim() === payload.author;

  const submit = async () => {
    setError("");
    setTip("");
    if (approved && !approver.trim()) {
      setError("通过审批必须填写审批人");
      return;
    }
    if (!approved && !feedback.trim()) {
      setError("驳回时请填写原因，便于修复 Agent 理解");
      return;
    }
    setBusy(true);
    try {
      const cleaned: Record<string, string> = {};
      Object.entries(outcomes).forEach(([key, value]) => {
        if (value) cleaned[key] = value;
      });
      await api.resume(jobId, {
        approved,
        approver: approver.trim() || "operator",
        feedback: feedback.trim(),
        finding_outcomes: cleaned,
      });
      setTip("已提交，流程继续执行中…");
      onSubmitted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="hitl">
      <div className="hitl-head">
        <Pill tone="warn" dot>HITL</Pill>
        <h3>需要人工回答</h3>
        <span style={{ marginLeft: "auto", fontSize: 12, color: "#d8b982" }}>
          tier {payload.tier} · 已收集 {collected.length}/{payload.required_approvals}
        </span>
      </div>
      <div className="hitl-body">
        {payload.reason ? <Alert tone="warn">门控原因：{payload.reason}</Alert> : null}
        {missing > 0 ? (
          <Alert tone="info">还需 {missing} 位审批人；审批绑定当前 head SHA 与树哈希，代码变动后审批失效。</Alert>
        ) : null}

        <div className="hitl-grid">
          <div className="subcard">
            <div className="subcard-head">审查结论</div>
            <div className="subcard-body" style={{ fontSize: 12.5, color: "var(--text-dim)" }}>
              <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
                <Pill tone={review.verdict === "approve" ? "ok" : "warn"}>{review.verdict || "—"}</Pill>
                <span className="mono" style={{ fontSize: 11 }}>confidence {review.confidence ?? "—"}</span>
              </div>
              {review.summary || "无摘要"}
            </div>
          </div>
          <div className="subcard">
            <div className="subcard-head">沙箱验证</div>
            <div className="subcard-body" style={{ fontSize: 12.5, color: "var(--text-dim)" }}>
              <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
                <Pill tone={validation.status === "passed" ? "ok" : "warn"}>{validation.status || "—"}</Pill>
                <span className="mono" style={{ fontSize: 11 }}>{validation.duration_sec ? `${validation.duration_sec}s` : ""}</span>
              </div>
              repro {String(validation.repro_passed ?? "—")} · regression {String(validation.regression_passed ?? "—")}
            </div>
          </div>
        </div>

        {blockingFindings.length ? (
          <div className="subcard">
            <div className="subcard-head">
              阻塞 findings 处置
              <span style={{ marginLeft: "auto", fontSize: 11.5, color: "var(--muted)" }}>填「误报/接受风险」可解除阻塞</span>
            </div>
            <div className="subcard-body">
              <div className="outcome-grid">
                {blockingFindings.map((finding) => (
                  <div key={finding.id} className="outcome-row">
                    <div className="msg">
                      <span className="mono" style={{ fontSize: 11, color: "var(--muted)", marginRight: 6 }}>
                        {finding.rule_id || finding.source}
                      </span>
                      {finding.message}
                      <span className="mono" style={{ fontSize: 11, color: "var(--muted)", marginLeft: 6 }}>
                        {finding.path}:{finding.line}
                      </span>
                    </div>
                    <select
                      className="select"
                      value={outcomes[finding.id] || ""}
                      onChange={(event) => setOutcomes((prev) => ({ ...prev, [finding.id]: event.target.value }))}
                    >
                      {OUTCOME_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : null}

        <div className="field-row">
          <div className="field">
            <label>审批人</label>
            <input
              className="input"
              placeholder="GitHub login 或姓名"
              value={approver}
              onChange={(event) => setApprover(event.target.value)}
            />
          </div>
          <div className="field">
            <label>结论</label>
            <div className="segmented" style={{ width: "fit-content" }}>
              <button type="button" className={approved ? "active" : ""} onClick={() => setApproved(true)}>通过</button>
              <button type="button" className={!approved ? "active" : ""} onClick={() => setApproved(false)}>驳回</button>
            </div>
          </div>
        </div>

        <div className="field">
          <label>反馈{approved ? "（可选，会写入审计）" : "（驳回必填）"}</label>
          <textarea
            className="textarea"
            placeholder={approved ? "补充说明，例如：已确认残余风险可接受" : "说明必须修改的点，修复 Agent 会据此生成补丁"}
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
          />
        </div>

        {selfApproval ? <Alert tone="warn">审批人是 PR 作者，tier 要求非作者审批，门控仍会阻塞。</Alert> : null}
        {error ? <Alert tone="error">{error}</Alert> : null}
        {tip ? <Alert tone="info">{tip}</Alert> : null}

        <div className="hitl-actions">
          <button type="button" className="btn btn-primary" disabled={busy} onClick={submit}>
            {approved ? <CheckIcon width={14} height={14} /> : <XIcon width={14} height={14} />}
            {busy ? "提交中…" : approved ? "提交通过" : "提交驳回"}
          </button>
          <span className="spacer" />
          {payload.diff ? (
            <details>
              <summary style={{ fontSize: 12, color: "var(--muted)", cursor: "pointer" }}>查看待合并 diff</summary>
              <div style={{ marginTop: 8, width: "min(760px, 78vw)" }}>
                <DiffBlock text={payload.diff} maxHeight={320} />
              </div>
            </details>
          ) : null}
        </div>
      </div>
    </div>
  );
}
