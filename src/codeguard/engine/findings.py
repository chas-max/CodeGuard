"""findings 生命周期与收敛工具。

设计要点：
- `findings` 在 state 中是**权威快照**（不是只增不减的 reducer），每轮扫描按 id 对账；
- 上一轮 open、本轮消失的 finding 标记为 `resolved`，只进 `finding_history` 审计；
- 只有 `lifecycle == open and blocking` 才阻塞合并、才进入修复指令。
"""
from __future__ import annotations

from ..models import FindingLifecycle, Severity

SEVERITY_RANK = {
    Severity.critical.value: 3,
    Severity.major.value: 2,
    Severity.minor.value: 1,
    Severity.info.value: 0,
}

MAX_HISTORY = 200


def finding_key(item: dict) -> str:
    """稳定 id；与 models.Finding/ SemanticFinding 的 id 生成保持一致。"""
    if item.get("id"):
        return str(item["id"])
    return "|".join(
        str(item.get(k, "")) for k in ("rule_id", "category", "path", "line", "message")
    )


def _severity(item: dict) -> str:
    return str(item.get("severity", "minor"))


def reconcile(
    previous: list[dict] | None,
    current: list[dict] | None,
    *,
    max_history: int = MAX_HISTORY,
) -> tuple[list[dict], list[dict]]:
    """按 id 对账：保留本轮 findings，并把上一轮消失的 open 项标记为 resolved。

    返回 `(findings, newly_resolved)`；`newly_resolved` 用于写入 `finding_history`，
    不会重复记录同一项（只有上一轮仍是 open 的才会被转成 resolved）。
    """
    prev = [dict(item) for item in (previous or []) if isinstance(item, dict)]
    cur = [dict(item) for item in (current or []) if isinstance(item, dict)]
    current_keys = {finding_key(item) for item in cur}

    resolved: list[dict] = []
    for item in prev:
        if finding_key(item) in current_keys:
            continue
        if str(item.get("lifecycle") or FindingLifecycle.open.value) != FindingLifecycle.open.value:
            continue
        resolved.append({**item, "lifecycle": FindingLifecycle.resolved.value})

    findings = [
        {**item, "lifecycle": str(item.get("lifecycle") or FindingLifecycle.open.value)}
        for item in cur
    ]
    # 本轮不再出现的已解决项仍保留在当前快照里，便于报告展示；下一轮对账后会自然淘汰。
    findings.extend(resolved)
    findings.sort(key=finding_key)
    return findings, resolved[-max_history:]


def open_findings(findings: list[dict] | None) -> list[dict]:
    return [
        dict(item) for item in (findings or [])
        if str(item.get("lifecycle") or FindingLifecycle.open.value) == FindingLifecycle.open.value
    ]


def blocking_findings(findings: list[dict] | None) -> list[dict]:
    """唯一阻塞判定入口：open 且 blocking。"""
    return [item for item in open_findings(findings) if item.get("blocking")]


def group_by_class(findings: list[dict] | None) -> list[dict]:
    """把同类问题聚合为一个条目：一次修完所有实例，而不是逐行来回。"""
    groups: dict[str, dict] = {}
    for item in findings or []:
        if str(item.get("lifecycle") or FindingLifecycle.open.value) != FindingLifecycle.open.value:
            continue
        class_id = str(item.get("rule_id") or item.get("category") or "other")
        group = groups.setdefault(class_id, {
            "class_id": class_id,
            "id": class_id,
            "category": item.get("category", "other"),
            "severity": _severity(item),
            "blocking": False,
            "message": item.get("message", ""),
            "suggestion": item.get("suggestion", ""),
            "paths": [],
            "instances": [],
        })
        if SEVERITY_RANK.get(_severity(item), 0) > SEVERITY_RANK.get(group["severity"], 0):
            group["severity"] = _severity(item)
        group["blocking"] = bool(group["blocking"] or item.get("blocking"))
        path = item.get("path", "")
        if path and path not in group["paths"]:
            group["paths"].append(path)
        group["instances"].append({
            "path": path,
            "line": item.get("line", 0),
            "message": item.get("message", ""),
            "evidence": item.get("evidence", ""),
        })
    ordered = sorted(groups.values(), key=lambda g: (g["class_id"]))
    for group in ordered:
        group["path"] = group["paths"][0] if group["paths"] else ""
        group["instance_count"] = len(group["instances"])
    return ordered


def apply_nit_cap(findings: list[dict] | None, max_nits: int) -> list[dict]:
    """blocking 永远保留；非 blocking 只保留最严重的 max_nits 条。"""
    items = [dict(item) for item in (findings or [])]
    if max_nits < 0:
        max_nits = 0
    blocking = [item for item in items if item.get("blocking")]
    advisory = [item for item in items if not item.get("blocking")]
    advisory.sort(key=lambda item: SEVERITY_RANK.get(_severity(item), 0), reverse=True)
    kept = {finding_key(item) for item in blocking}
    kept.update(finding_key(item) for item in advisory[:max_nits])
    return [item for item in items if finding_key(item) in kept]


__all__ = [
    "SEVERITY_RANK", "finding_key", "reconcile", "open_findings", "blocking_findings",
    "group_by_class", "apply_nit_cap",
]
