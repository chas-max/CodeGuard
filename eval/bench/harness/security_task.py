"""安全任务 harness：函数级漏洞样本 → 确定性扫描。

只使用本地静态扫描，不调 LLM、不触网；结论用于 F2 检测能力的补充证据。
"""
from __future__ import annotations

from pathlib import Path

from ..models import TaskSpec

_SEVERITY_RANK = {
    "critical": 4, "blocker": 4, "high": 3, "major": 3,
    "medium": 2, "minor": 1, "low": 1, "info": 0,
}


def run_security_task(task: TaskSpec, *, work_root: Path | str) -> tuple[str, dict]:
    payload = task.input or {}
    instance = payload.get("instance") if isinstance(payload.get("instance"), dict) else {}
    code = str(payload.get("code") or instance.get("code") or "")
    if not code.strip():
        return "skipped", {"stage": "code", "error": "样本没有代码"}

    from codeguard.engine.static_scan import scan_tree

    root = Path(work_root) / task.task_id.replace("/", "_")
    root.mkdir(parents=True, exist_ok=True)
    (root / "sample.py").write_text(code, encoding="utf-8")
    report = scan_tree(root, ["sample.py"])
    findings = [
        {
            "rule_id": str(item.rule_id),
            "severity": item.severity.value if hasattr(item.severity, "value") else str(item.severity),
            "message": str(item.message),
        }
        for item in report.findings
    ]
    severity = "low"
    if findings:
        severity = max(
            (item["severity"] for item in findings),
            key=lambda value: _SEVERITY_RANK.get(str(value).lower(), 0),
        )
    return "ok", {
        "vulnerable": bool(findings),
        "findings": findings,
        "severity": severity,
    }


__all__ = ["run_security_task"]
