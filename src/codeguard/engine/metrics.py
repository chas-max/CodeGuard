"""审查指标：把 job record 聚合成可对比的口径。

口径（对齐文章的目标函数，但换成内部场景）：
- human actions per PR：每个 PR 需要的人类动作数（审批次数）；
- approval latency：PR 创建到第一次人工批准的时间；
- rounds-to-pass：review 轮次；
- findings precision：被确认修掉的 finding / 被发布过的 finding；
- cost per PR：LLM + 沙箱成本。
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Iterable

OUTCOMES = (
    "MERGED", "MERGE_READY", "CHANGES_REQUESTED", "BLOCKED_HUMAN",
    "REJECTED_NO_ISSUE", "REJECTED_OUT_OF_SCOPE", "INFRA_FAILED", "STALE", "FAILED",
)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return float(ordered[index])


def _precision(records: list[dict]) -> float:
    fixed = dismissed = 0
    for record in records:
        outcomes = record.get("finding_outcomes") or {}
        fixed += sum(1 for value in outcomes.values() if value == "fixed")
        dismissed += sum(
            1 for value in outcomes.values() if value in ("dismissed", "intentional", "false_positive")
        )
    total = fixed + dismissed
    return round(fixed / total, 3) if total else 0.0


def summarize(records: Iterable[dict]) -> dict:
    items = [record for record in records if isinstance(record, dict)]
    latencies = [float(r["approval_latency_sec"]) for r in items if r.get("approval_latency_sec") is not None]
    costs = [float(r.get("cost_cny", 0.0) or 0.0) for r in items]
    rounds = [int(r.get("review_rounds", 0) or 0) for r in items]
    approvals = [int(r.get("approvals", 0) or 0) for r in items]
    merged = [r for r in items if r.get("outcome") == "MERGED"]
    auto_merged = [r for r in merged if int(r.get("approvals", 0) or 0) == 0]
    by_outcome = {name: 0 for name in OUTCOMES}
    for record in items:
        outcome = str(record.get("outcome") or "FAILED")
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
    return {
        "total": len(items),
        "by_outcome": by_outcome,
        "merged": len(merged),
        "auto_merge_share": round(len(auto_merged) / len(merged), 3) if merged else 0.0,
        "human_actions_per_pr": {
            "median": median(approvals) if approvals else 0,
            "p90": percentile([float(x) for x in approvals], 90),
        },
        "approval_latency_sec": {
            "median": median(latencies) if latencies else 0.0,
            "p90": percentile(latencies, 90),
        },
        "rounds_to_pass": {
            "median": median(rounds) if rounds else 0,
            "p90": percentile([float(x) for x in rounds], 90),
        },
        "findings_precision": _precision(items),
        "cost_cny": {
            "total": round(sum(costs), 4),
            "median": round(median(costs), 4) if costs else 0.0,
        },
    }


def load_records(directory: Path | str) -> list[dict]:
    root = Path(directory)
    if not root.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def render_markdown(report: dict) -> str:
    lines = [
        "# CodeGuard 指标",
        "",
        f"- PR 总数: {report['total']}",
        f"- 合并: {report['merged']}（零人工合并占比 {report['auto_merge_share']}）",
        f"- 人工动作数（中位数 / P90）: {report['human_actions_per_pr']['median']} / "
        f"{report['human_actions_per_pr']['p90']}",
        f"- 审批延迟秒（中位数 / P90）: {report['approval_latency_sec']['median']} / "
        f"{report['approval_latency_sec']['p90']}",
        f"- review 轮次（中位数 / P90）: {report['rounds_to_pass']['median']} / "
        f"{report['rounds_to_pass']['p90']}",
        f"- finding precision: {report['findings_precision']}",
        f"- 成本（总 / 中位数 CNY）: {report['cost_cny']['total']} / {report['cost_cny']['median']}",
        "",
        "## 终态分布",
        "",
    ]
    lines.extend(f"- {name}: {count}" for name, count in report["by_outcome"].items() if count)
    return "\n".join(lines) + "\n"


__all__ = ["percentile", "summarize", "load_records", "render_markdown", "OUTCOMES"]
