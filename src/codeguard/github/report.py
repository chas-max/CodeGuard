"""审查结论发布：review 摘要 + 逐条 inline 评论，带 marker 保证幂等。

设计取舍：
- 不冒充人类审批：review event 固定 `COMMENT`，机器人批准永远不算人工批准；
- 只对 blocking 且能定位到 `path:line` 的 finding 发 inline 评论，nit 只进摘要；
- 同一 finding 用 marker 去重，内容变了才更新，避免每轮重复刷屏。
"""
from __future__ import annotations

import hashlib

SUMMARY_MARKER = "<!-- codeguard:summary:{job_id} -->"
FINDING_MARKER = "<!-- codeguard:finding:{fid} -->"

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}


def finding_marker(finding: dict) -> str:
    fid = str(finding.get("id") or hashlib.sha1(
        f"{finding.get('path')}:{finding.get('line')}:{finding.get('message')}".encode("utf-8")
    ).hexdigest()[:16])
    return FINDING_MARKER.format(fid=fid)


def build_summary(
    *,
    job_id: str,
    outcome: str = "",
    profile: str = "change",
    tier: str = "T1",
    required_approvals: int = 1,
    findings: list[dict] | None = None,
    verdict: str = "",
    reason: str = "",
) -> str:
    counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    blocking = 0
    for item in findings or []:
        severity = str(item.get("severity", "minor"))
        counts[severity] = counts.get(severity, 0) + 1
        if item.get("blocking"):
            blocking += 1
    head = f"{counts['critical']} critical, {counts['major']} major, {counts['minor']} minor"
    verdict_line = f"结论：{verdict or outcome or '未完成'}"
    approval_line = f"风险等级 {tier}，需要 {required_approvals} 名人工批准"
    if blocking:
        body = "存在 blocking 问题，需要修复或逐条回应。"
    else:
        body = "未发现 blocking 问题。"
    lines = [
        SUMMARY_MARKER.format(job_id=job_id),
        f"**CodeGuard 审查：{verdict or outcome or 'review'}**",
        "",
        f"- 计数：{head}（blocking {blocking}）",
        f"- {approval_line}",
        f"- profile：{profile}",
        f"- {verdict_line}: {body}",
    ]
    if reason:
        lines.append(f"- 门控说明：{reason[:1000]}")
    return "\n".join(lines)


def _finding_body(item: dict) -> str:
    label = "pre-existing" if item.get("introduced_by_pr") is False else "本次引入"
    lines = [
        finding_marker(item),
        f"[{str(item.get('severity', 'minor')).upper()} | {label}] {item.get('message', '')}",
    ]
    if item.get("evidence"):
        lines.append(f"证据：{str(item['evidence'])[:800]}")
    if item.get("suggestion"):
        lines.append(f"建议：{str(item['suggestion'])[:800]}")
    if item.get("demoted_by"):
        lines.append(f"（已复核降级：{item['demoted_by']}）")
    return "\n".join(lines)


def publish_review(
    github,
    *,
    repo: str,
    number: int,
    head_sha: str,
    job_id: str,
    findings: list[dict] | None = None,
    outcome: str = "",
    profile: str = "change",
    tier: str = "T1",
    required_approvals: int = 1,
    verdict: str = "",
    reason: str = "",
) -> dict:
    """发布 review；返回 `{created, updated, skipped, summary_posted}`。"""
    stats = {"created": 0, "updated": 0, "skipped": 0, "summary_posted": False}
    inline = [
        item for item in (findings or [])
        if item.get("blocking") and item.get("path") and int(item.get("line") or 0) > 0
    ]
    inline.sort(key=lambda item: (SEVERITY_ORDER.get(str(item.get("severity")), 9), item.get("path", "")))

    existing: dict[str, dict] = {}
    if hasattr(github, "list_review_comments") and inline:
        try:
            for comment in github.list_review_comments(repo, number) or []:
                body = str(comment.get("body", ""))
                marker = body.splitlines()[0] if body else ""
                if marker.startswith("<!-- codeguard:finding:"):
                    existing[marker] = comment
        except Exception:  # noqa: BLE001 - 拉列表失败不影响主流程
            existing = {}

    for item in inline:
        body = _finding_body(item)
        marker = body.splitlines()[0]
        prior = existing.get(marker)
        try:
            if prior is None:
                github.create_review_comment(
                    repo, number, body=body, commit_id=head_sha,
                    path=str(item["path"]), line=int(item["line"]),
                )
                stats["created"] += 1
            elif str(prior.get("body", "")) != body:
                github.update_review_comment(repo, int(prior.get("id", 0)), body=body)
                stats["updated"] += 1
            else:
                stats["skipped"] += 1
        except Exception:  # noqa: BLE001 - 单条评论失败不阻断
            continue

    summary = build_summary(
        job_id=job_id, outcome=outcome, profile=profile, tier=tier,
        required_approvals=required_approvals, findings=findings,
        verdict=verdict, reason=reason,
    )
    if hasattr(github, "submit_review"):
        try:
            github.submit_review(repo, number, event="COMMENT", body=summary)
            stats["summary_posted"] = True
        except Exception:  # noqa: BLE001
            stats["summary_posted"] = False
    return stats


def write_local_report(path, *, summary: str, findings: list[dict] | None = None) -> None:
    """dry-run / 本地模式：把同样的结论写到文件里。"""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# CodeGuard 审查报告", "", summary, ""]
    blocking = [item for item in (findings or []) if item.get("blocking")]
    if blocking:
        lines.append("## Blocking findings")
        for item in blocking:
            lines.append(f"- `{item.get('path', '')}:{item.get('line', 0)}` "
                         f"{item.get('message', '')}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = [
    "SUMMARY_MARKER", "FINDING_MARKER", "finding_marker", "build_summary",
    "publish_review", "write_local_report",
]
