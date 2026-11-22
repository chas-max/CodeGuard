import type { Finding, ReviewValues } from "../types";
import { DiffBlock, Empty, JsonBlock, KeyValue, Pill, SeverityPill, SubCard } from "./ui";

function asArray(value: unknown): any[] {
  return Array.isArray(value) ? value : [];
}

function bool(value: unknown): string {
  if (value === true) return "是";
  if (value === false) return "否";
  return "—";
}

export function FindingsList({ findings, dense }: { findings: Finding[]; dense?: boolean }) {
  if (!findings.length) return <Empty title="没有 finding" hint="该阶段未发现问题" />;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {findings.map((finding) => (
        <div key={finding.id} className={`finding ${finding.blocking ? "blocking" : ""}`}>
          <div className="finding-head">
            <SeverityPill severity={finding.severity} blocking={finding.blocking} />
            <span className="rule">{finding.rule_id || finding.source}</span>
            {finding.category ? <span className="tag">{finding.category}</span> : null}
            {finding.lifecycle && finding.lifecycle !== "open" ? (
              <Pill tone={finding.lifecycle === "resolved" ? "ok" : "muted"}>{finding.lifecycle}</Pill>
            ) : null}
            <span className="where">
              {finding.path || "—"}
              {finding.line ? `:${finding.line}` : ""}
            </span>
          </div>
          <div className="msg">{finding.message}</div>
          {!dense && finding.evidence ? <div className="evidence">{finding.evidence}</div> : null}
          {!dense && finding.suggestion ? (
            <div style={{ fontSize: 12, color: "var(--muted)" }}>建议：{finding.suggestion}</div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function Logs({ text }: { text: string }) {
  if (!text) return null;
  return (
    <SubCard title="执行日志" extra={<Pill tone="muted">{text.length} 字符</Pill>}>
      <pre className="code" style={{ maxHeight: 260 }}>{text}</pre>
    </SubCard>
  );
}

const NODE_ROLE_PREFIX: Record<string, string[]> = {
  pr_reader: ["pr_reader"],
  dispatch_reviewers: ["review_logic", "review_security", "review_architecture", "review_tests"],
  review_logic: ["review_logic"],
  review_security: ["review_security"],
  review_architecture: ["review_architecture"],
  review_tests: ["review_tests"],
  coder_agent: ["coder"],
  qa_checker: ["qa_checker"],
  hybrid_scan: ["semantic_detector", "finding_verifier"],
  verify_issue: ["issue_verifier"],
  review: ["reviewer"],
  fix: ["fixer"],
};

function ToolEventsSection({ node, events }: { node: string; events: any[] }) {
  const prefixes = NODE_ROLE_PREFIX[node] || [];
  const filtered = prefixes.length
    ? events.filter((ev) => prefixes.some((p) => String(ev?.role || "").startsWith(p)))
    : events;
  if (!filtered.length) return null;
  return (
    <SubCard
      title="Agent 工具与 MCP 调用审计"
      extra={<Pill tone="info">{filtered.length} 次</Pill>}
    >
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>角色</th>
              <th>工具</th>
              <th>来源</th>
              <th>协议</th>
              <th>状态</th>
              <th>耗时</th>
            </tr>
          </thead>
          <tbody>
            {filtered.slice(0, 30).map((ev, idx) => (
              <tr key={idx} style={{ cursor: "default" }}>
                <td className="mono" style={{ fontSize: 11.5 }}>{ev.role || "—"}</td>
                <td className="mono" style={{ fontSize: 11.5 }}>{ev.tool || "—"}</td>
                <td>
                  {ev.source === "mcp"
                    ? <Pill tone="violet">MCP · {ev.server || "external"}</Pill>
                    : <Pill tone="muted">builtin</Pill>}
                </td>
                <td className="mono" style={{ fontSize: 11 }}>{ev.protocol || "json"}</td>
                <td>
                  <Pill tone={ev.status === "ok" ? "ok" : "warn"}>{ev.status || "—"}</Pill>
                </td>
                <td className="mono" style={{ fontSize: 11 }}>{ev.duration_ms ?? 0}ms</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </SubCard>
  );
}

export function NodeResult({ node, values }: { node: string; values: ReviewValues }) {
  const findings = asArray(values.findings) as Finding[];
  const blocking = asArray(values.blocking_findings) as Finding[];

  if (node === "prepare_context") {
    const risk = values.risk_tier || {};
    const partition = values.code_partition || {};
    const files = asArray(values.changed_files);
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="PR 与分级下限" extra={<Pill tone={risk.tier === "T2" ? "danger" : risk.tier === "T1" ? "warn" : "ok"}>{values.importance_level || "medium"} / {risk.tier || "—"}</Pill>}>
          <KeyValue
            items={[
              ["仓库 / PR", <span className="mono">{values.repo || "—"}#{values.pr_number || "—"}</span>],
              ["标题", values.pr_title || "—"],
              ["审批门槛", `${risk.min_human_approvals ?? 0} 人${risk.require_non_author ? "（需非作者）" : ""}`],
              ["代码分区", `源码 ${asArray(partition.source_files).length} · 测试 ${asArray(partition.test_files).length} · 配置 ${asArray(partition.config_dep_files).length} · 文档 ${asArray(partition.doc_files).length}`],
              ["分级理由", asArray(risk.reasons).length ? asArray(risk.reasons).join("；") : "—"],
            ]}
          />
        </SubCard>
        <SubCard title="确定性快扫 findings" extra={<Pill tone={blocking.length ? "danger" : "ok"}>blocking {blocking.length}</Pill>}>
          <FindingsList findings={findings} dense />
        </SubCard>
        <SubCard title="改动文件" extra={<Pill tone="muted">{files.length} 个</Pill>}>
          {files.length ? <JsonBlock value={partition} maxHeight={200} /> : <Empty title="无文件变更" />}
        </SubCard>
      </div>
    );
  }

  if (node === "pr_reader") {
    const brief = values.pr_brief || {};
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="PR 语义简报" extra={<Pill tone="info">{values.importance_level || brief.suggested_importance || "medium"}</Pill>}>
          <KeyValue
            items={[
              ["问题摘要", brief.problem_summary || "—"],
              ["修改前", brief.before_behavior || "—"],
              ["修改后", brief.after_behavior || "—"],
              ["背景", brief.background || "—"],
              ["模型理由", brief.importance_reason || "—"],
            ]}
          />
        </SubCard>
        <ToolEventsSection node={node} events={asArray(values.tool_events)} />
      </div>
    );
  }

  if (node === "dispatch_reviewers" || node.startsWith("review_")) {
    const outputs = values.reviewer_outputs || {};
    const targetRoles = node === "dispatch_reviewers" ? Object.keys(outputs) : [node];
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {targetRoles.length ? targetRoles.map((roleKey) => {
          const out = outputs[roleKey] || {};
          return (
            <SubCard
              key={roleKey}
              title={roleKey}
              extra={<Pill tone={out.needs_modification ? "warn" : "ok"}>{out.needs_modification ? "需修改" : "通过"}</Pill>}
            >
              <KeyValue
                items={[
                  ["置信度", String(out.confidence ?? "—")],
                  ["摘要", out.summary || "—"],
                ]}
              />
              <FindingsList findings={asArray(out.findings) as Finding[]} />
            </SubCard>
          );
        }) : <Empty title="等待审查视角产出" />}
        <ToolEventsSection node={node} events={asArray(values.tool_events)} />
      </div>
    );
  }

  if (node === "synthesize_findings") {
    const directive = values.fix_directive || {};
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard
          title="规则综合与 FixDirective"
          extra={<Pill tone={directive.needs_modification ? "warn" : "ok"}>{directive.needs_modification ? "需修复" : "通过"}</Pill>}
        >
          <KeyValue
            items={[
              ["总 findings / 阻塞项", `${findings.length} / ${blocking.length}`],
              ["可修复文件范围", asArray(directive.allowed_fix_files).join(", ") || "—"],
              ["允许补测文件", asArray(directive.allowed_test_addition_files).join(", ") || "无"],
              ["不可自动修复", asArray(directive.non_fixable_blockers).join("；") || "无"],
            ]}
          />
        </SubCard>
        <SubCard title="综合问题列表">
          <FindingsList findings={findings} />
        </SubCard>
      </div>
    );
  }

  if (node === "coder_agent") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard
          title={`Coder Agent · 第 ${values.fix_round ?? 0} 轮`}
          extra={<Pill tone={values.fix_patch ? "ok" : "muted"}>{values.fix_files?.length || 0} 文件</Pill>}
        >
          <KeyValue
            items={[
              ["修改文件", asArray(values.fix_files).join(", ") || "—"],
              ["树哈希", <span className="mono">{values.fix_tree_hash || "—"}</span>],
            ]}
          />
        </SubCard>
        {values.fix_patch ? <DiffBlock text={values.fix_patch} /> : <Empty title="无补丁内容" />}
        <ToolEventsSection node={node} events={asArray(values.tool_events)} />
      </div>
    );
  }

  if (node === "qa_checker") {
    const report = values.qa_report || {};
    const tone = report.verdict === "pass" ? "ok" : report.verdict === "revise" ? "warn" : "danger";
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="QA Checker 对齐结果" extra={<Pill tone={tone}>{report.verdict || "—"}</Pill>}>
          <KeyValue
            items={[
              ["作用域一致", bool(report.scope_ok)],
              ["偏移警告", bool(report.drift_detected)],
              ["摘要", report.summary || "—"],
              ["返工指示", report.revise_instructions || "无"],
            ]}
          />
        </SubCard>
        <ToolEventsSection node={node} events={asArray(values.tool_events)} />
      </div>
    );
  }

  if (node === "summary_and_submit") {
    const summary = values.review_summary || {};
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="总结与 PR 提交" extra={<Pill tone="info">{summary.effective_level || values.importance_level || "medium"}</Pill>}>
          <KeyValue
            items={[
              ["继承等级 / 最终等级", `${values.inherited_importance_level || "—"} → ${summary.effective_level || values.importance_level || "—"}`],
              ["提交方式", summary.submitted_mode || "comment"],
              ["标题", summary.headline || "—"],
              ["是否推送修复", bool(values.fix_pushed)],
            ]}
          />
        </SubCard>
        {summary.markdown_body ? (
          <SubCard title="提交说明">
            <pre className="code plain" style={{ maxHeight: 280 }}>{summary.markdown_body}</pre>
          </SubCard>
        ) : null}
      </div>
    );
  }

  if (node === "ingest") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="PR 上下文">
          <KeyValue
            items={[
              ["仓库", <span className="mono">{values.repo || "—"}</span>],
              ["PR", <span className="mono">#{values.pr_number || "—"}</span>],
              ["标题", values.pr_title || "—"],
              ["作者", values.author || "—"],
              ["profile", <Pill tone={values.profile === "bugfix" ? "violet" : "info"}>{values.profile || "change"}</Pill>],
              ["分支", <span className="mono">{values.base_ref || "main"} ← {values.head_ref || "—"}</span>],
              ["head SHA", <span className="mono">{values.head_sha || "—"}</span>],
              ["接收时间", values.started_at || "—"],
            ]}
          />
        </SubCard>
        {values.pr_body ? (
          <SubCard title="PR 描述">
            <pre className="code plain" style={{ maxHeight: 220 }}>{values.pr_body}</pre>
          </SubCard>
        ) : null}
      </div>
    );
  }

  if (node === "preflight") {
    const risk = values.risk_tier || {};
    const policy = values.policy || {};
    const files = asArray(values.changed_files);
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="风险分级" extra={<Pill tone={risk.tier === "T2" ? "danger" : risk.tier === "T1" ? "warn" : "ok"}>{risk.tier || "—"}</Pill>}>
          <KeyValue
            items={[
              ["审批人数要求", `${risk.min_human_approvals ?? 0} 人${risk.require_non_author ? "（需非作者）" : ""}`],
              ["审查深度", risk.depth || "—"],
              ["可信作者", bool(risk.trusted_author)],
              ["Agent 署名", bool(risk.agent_authored)],
              ["分级理由", asArray(risk.reasons).length ? asArray(risk.reasons).join("；") : "—"],
            ]}
          />
        </SubCard>
        <SubCard title="改动概览" extra={<Pill tone="muted">{files.length} 个文件</Pill>}>
          {files.length ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              {files.slice(0, 24).map((file: any) => (
                <div key={file.path} style={{ display: "flex", gap: 8, fontSize: 12 }}>
                  <span className="mono" style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>{file.path}</span>
                  <span className="tag">{file.status || "modified"}</span>
                  <span style={{ color: "var(--accent)", fontFamily: "var(--mono)", fontSize: 11 }}>+{file.additions ?? 0}</span>
                  <span style={{ color: "var(--red)", fontFamily: "var(--mono)", fontSize: 11 }}>-{file.deletions ?? 0}</span>
                </div>
              ))}
              {files.length > 24 ? <div style={{ fontSize: 12, color: "var(--muted)" }}>还有 {files.length - 24} 个文件…</div> : null}
            </div>
          ) : <Empty title="无文件变更" />}
        </SubCard>
        <SubCard title="生效策略" extra={<Pill tone="muted">{policy.mode || "默认"}</Pill>}>
          <JsonBlock value={policy} maxHeight={260} />
        </SubCard>
      </div>
    );
  }

  if (node === "hybrid_scan") {
    const report = values.hybrid_scan_report || {};
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {values.semantic_unavailable ? (
          <div className="alert warn">语义检测不可用：本次结果仅来自确定性检测，任务不会自动合并。</div>
        ) : null}
        <SubCard title="finding 汇总" extra={<Pill tone={blocking.length ? "danger" : "ok"}>blocking {blocking.length}</Pill>}>
          <KeyValue
            items={[
              ["finding 总数", String(findings.length)],
              ["阻塞项", String(blocking.length)],
              ["确定性 / 语义", `${asArray(report.deterministic?.findings).length} / ${asArray(report.semantic?.findings).length}`],
              ["冲突", asArray(report.conflicts).length ? asArray(report.conflicts).join("；") : "—"],
            ]}
          />
        </SubCard>
        <SubCard title="findings">
          <FindingsList findings={findings} />
        </SubCard>
        <ToolEventsSection node={node} events={asArray(values.tool_events)} />
      </div>
    );
  }

  if (node === "verify_issue") {
    const claim = values.claim || {};
    const issue = values.issue_verification || {};
    const intent = values.intent_verification || {};
    const evidence = asArray(issue.evidence);
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {intent.status ? (
          <SubCard title="意图验证（advisory）" extra={<Pill tone={intent.status === "SUFFICIENT" ? "ok" : "warn"}>{intent.status}</Pill>}>
            <KeyValue
              items={[
                ["声称类型", intent.claimed_kind || "—"],
                ["置信度", String(intent.confidence ?? "—")],
                ["结论", intent.reason || "—"],
                ["证据", asArray(intent.evidence).length ? asArray(intent.evidence).join("；") : "—"],
              ]}
            />
          </SubCard>
        ) : null}
        {issue.status ? (
          <SubCard title="复现验证" extra={<Pill tone={issue.status === "CONFIRMED" ? "ok" : issue.status === "NOT_REPRODUCED" ? "warn" : "danger"}>{issue.status}</Pill>}>
            <KeyValue
              items={[
                ["置信度", String(issue.confidence ?? "—")],
                ["结论", issue.reason || "—"],
                ["测试来源", issue.test_source || "—"],
                ["被执行测试", values.repro_tests?.length ? values.repro_tests.join(", ") : "—"],
              ]}
            />
            <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
              {evidence.map((item: any, index: number) => (
                <div key={index} className="finding">
                  <div className="finding-head">
                    <span className="rule">{item.test_file || item.test_source || "evidence"}</span>
                    <Pill tone={item.base_failed ? "danger" : "muted"}>base exit {item.base_exit_code ?? "—"}</Pill>
                    <Pill tone={item.head_passed ? "ok" : "muted"}>head exit {item.head_exit_code ?? "—"}</Pill>
                  </div>
                  {item.log ? <pre className="evidence">{item.log}</pre> : null}
                </div>
              ))}
            </div>
          </SubCard>
        ) : null}
        {claim.statement ? (
          <SubCard title="提取的 claim">
            <KeyValue
              items={[
                ["kind", claim.kind || "—"],
                ["statement", claim.statement || "—"],
                ["expected", claim.expected || "—"],
                ["actual", claim.actual || "—"],
                ["affected", asArray(claim.affected_paths).join(", ") || "—"],
              ]}
            />
          </SubCard>
        ) : null}
        {!intent.status && !issue.status && !claim.statement ? <Empty title="本节点无产出（可能被跳过）" /> : null}
        <ToolEventsSection node={node} events={asArray(values.tool_events)} />
      </div>
    );
  }

  if (node === "validate" || node === "verify_gate") {
    const report = values.validation_report || {};
    const tone = report.status === "passed" ? "ok" : report.status === "failed" ? "danger" : "warn";
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="前置验证与沙箱门禁" extra={<Pill tone={tone}>{report.status || "—"}</Pill>}>
          <KeyValue
            items={[
              ["复现测试", bool(report.repro_passed)],
              ["回归测试", bool(report.regression_passed)],
              ["耗时", report.duration_sec ? `${report.duration_sec}s` : "—"],
              ["命令", asArray(report.commands).length ? asArray(report.commands).join(" && ") : "—"],
              ["新增失败", asArray(report.new_failures).length ? asArray(report.new_failures).join("；") : "无"],
              ["tree hash", <span className="mono">{report.tree_hash || values.reviewed_tree_hash || "—"}</span>],
              ["异常", report.error || "无"],
            ]}
          />
        </SubCard>
        {asArray(report.checks).length ? (
          <SubCard title="策略检查">
            <FindingsList findings={asArray(report.checks) as Finding[]} dense />
          </SubCard>
        ) : null}
        <Logs text={String(report.logs || "")} />
      </div>
    );
  }

  if (node === "review") {
    const decision = values.review_decision || {};
    const verdict = decision.verdict;
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard
          title="审查结论"
          extra={<Pill tone={verdict === "approve" ? "ok" : "warn"}>{verdict || "—"}</Pill>}
        >
          <KeyValue
            items={[
              ["第几轮", `${values.review_round ?? 0} · ${values.review_round_mode || "full"}`],
              ["置信度", String(decision.confidence ?? "—")],
              ["摘要", decision.summary || "—"],
              ["残余风险", asArray(decision.residual_risks).length ? asArray(decision.residual_risks).join("；") : "无"],
            ]}
          />
        </SubCard>
        {asArray(decision.fix_instructions).length ? (
          <SubCard title="修复指令">
            <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: "var(--text-dim)" }}>
              {asArray(decision.fix_instructions).map((item, index) => <li key={index}>{String(item)}</li>)}
            </ol>
          </SubCard>
        ) : null}
        <SubCard title="审查发现">
          <FindingsList findings={asArray(decision.findings) as Finding[]} />
        </SubCard>
        {values.escalation_reason ? (
          <div className="alert warn">升级人工原因：{values.escalation_reason}</div>
        ) : null}
        <ToolEventsSection node={node} events={asArray(values.tool_events)} />
      </div>
    );
  }

  if (node === "fix") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard
          title={`修复补丁 · 第 ${values.fix_round ?? 0} 轮`}
          extra={<Pill tone={values.fix_pushed ? "ok" : "muted"}>{values.fix_pushed ? "已推送" : "未推送"}</Pill>}
        >
          <KeyValue
            items={[
              ["改动文件", asArray(values.fix_files).length ? asArray(values.fix_files).join(", ") : "—"],
              ["远端 SHA", <span className="mono">{values.fix_remote_sha || "—"}</span>],
              ["树哈希", <span className="mono">{values.fix_tree_hash || "—"}</span>],
            ]}
          />
        </SubCard>
        {values.fix_patch ? <DiffBlock text={values.fix_patch} /> : <Empty title="无补丁内容" />}
        <ToolEventsSection node={node} events={asArray(values.tool_events)} />
      </div>
    );
  }

  if (node === "merge_gate") {
    const decision = values.merge_decision || {};
    const openBlocking = blocking.filter((item) => item.lifecycle === "open");
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="门控结论" extra={<Pill tone={decision.outcome === "MERGE_READY" ? "ok" : decision.outcome === "MERGED" ? "ok" : "warn"}>{decision.outcome || "—"}</Pill>}>
          <KeyValue
            items={[
              ["原因", decision.reason || "—"],
              ["需要人工", bool(decision.requires_human)],
              ["动作", asArray(decision.actions).length ? asArray(decision.actions).join("；") : "—"],
              ["未解决 blocking", String(openBlocking.length)],
              ["已审查 SHA", <span className="mono">{values.reviewed_sha || "—"}</span>],
              ["当前 head", <span className="mono">{values.head_sha || "—"}</span>],
            ]}
          />
        </SubCard>
        {openBlocking.length ? (
          <SubCard title="阻塞项">
            <FindingsList findings={openBlocking} />
          </SubCard>
        ) : null}
      </div>
    );
  }

  if (node === "human_approval") {
    const approvals = asArray(values.human_approvals);
    const decision = values.human_decision || {};
    const required = values.risk_tier?.min_human_approvals ?? 0;
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="审批进度" extra={<Pill tone={approvals.length >= required && required > 0 ? "ok" : "warn"}>{approvals.length}/{required}</Pill>}>
          {approvals.length ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {approvals.map((item: any, index: number) => (
                <div key={index} className="finding-head" style={{ display: "flex", gap: 8, fontSize: 12 }}>
                  <Pill tone="ok">{item.approver || "operator"}</Pill>
                  <span className="mono" style={{ color: "var(--muted)" }}>sha {String(item.sha || "").slice(0, 10) || "—"}</span>
                  <span className="mono" style={{ color: "var(--muted)" }}>tree {String(item.tree_hash || "").slice(0, 10) || "—"}</span>
                  <span style={{ marginLeft: "auto", color: "var(--muted)" }}>{item.at || ""}</span>
                </div>
              ))}
            </div>
          ) : <Empty title="尚未收集到审批" />}
        </SubCard>
        <SubCard title="人工结论">
          <KeyValue
            items={[
              ["结果", decision.approved === true ? "通过" : decision.approved === false ? "驳回" : "—"],
              ["审批人", decision.approver || "—"],
              ["反馈", decision.feedback || "—"],
              ["findings 处置", Object.keys(values.finding_outcomes || {}).length
                ? Object.entries(values.finding_outcomes).map(([key, value]) => `${key.slice(0, 8)}→${value}`).join("；")
                : "—"],
            ]}
          />
        </SubCard>
      </div>
    );
  }

  if (node === "merge") {
    const decision = values.merge_decision || {};
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="合并结果" extra={<Pill tone={values.outcome === "MERGED" ? "ok" : "warn"}>{values.outcome || "—"}</Pill>}>
          <KeyValue
            items={[
              ["merge commit", <span className="mono">{values.merge_commit_sha || "—"}</span>],
              ["原因", decision.reason || "—"],
              ["错误", values.error || "无"],
            ]}
          />
        </SubCard>
      </div>
    );
  }

  if (node === "finalize" || node === "persist_memory_and_finalize") {
    const record = values.run_record || {};
    const mems = asArray(values.memory_records_written);
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <SubCard title="终态与记忆入库" extra={<Pill tone={record.outcome === "MERGED" ? "ok" : "warn"}>{record.outcome || values.outcome || "—"}</Pill>}>
          <KeyValue
            items={[
              ["说明", record.message || "—"],
              ["重要等级 / tier", `${record.importance_level || values.importance_level || "—"} / ${record.tier || "—"}`],
              ["写入向量记忆 ID", mems.length ? mems.join(", ") : "未写入"],
              ["耗时", record.duration_sec ? `${record.duration_sec}s` : "—"],
              ["审查轮次 / 修复轮次", `${record.review_rounds ?? 0} / ${record.fix_rounds ?? 0}`],
              ["findings（总/阻塞/已解决）", `${record.findings_total ?? 0} / ${record.findings_blocking ?? 0} / ${record.findings_resolved ?? 0}`],
              ["成本", record.cost_cny !== undefined ? `¥${record.cost_cny}` : "—"],
              ["LLM / 工具调用", `${record.llm_calls ?? 0} / ${record.tool_calls ?? 0}`],
            ]}
          />
        </SubCard>
        <SubCard title="job record">
          <JsonBlock value={record} maxHeight={360} />
        </SubCard>
      </div>
    );
  }

  return <JsonBlock value={values} />;
}
