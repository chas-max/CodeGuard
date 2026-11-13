"""审查任务 harness：把 PR diff 直接喂给唯一 reviewer，产出 blocking findings。

ReviewBench 只提供 diff 与 gold findings，不提供可执行测试；因此这里不需要
Docker，只需要 LLM provider。离线或无 key 时由调用方标记 skipped。
"""
from __future__ import annotations

from pathlib import Path

from ..models import TaskSpec


def run_review_task(task: TaskSpec, *, provider, work_root: Path | str) -> tuple[str, dict]:
    if provider is None:
        return "skipped", {"stage": "provider", "error": "review 任务需要 LLM provider"}

    from codeguard.agents.reviewer import review
    from codeguard.config import load_settings
    from codeguard.engine.diff import patch_paths
    from codeguard.nodes import ReviewNodes
    from codeguard.sandbox.fake_backend import FakeSandboxBackend

    work = Path(work_root)
    work.mkdir(parents=True, exist_ok=True)
    settings = load_settings(
        data_dir=work / "data", checkpoint_path=work / "ckpt.db",
        sandbox_backend="fake", reports_dir=work / "reports",
    )
    ctx = ReviewNodes(settings, llm_provider=provider, sandbox=FakeSandboxBackend())
    diff = str(task.input.diff or "")
    title = str(task.input.title or "").strip()
    statement = str(task.input.problem_statement or "").strip()
    profile = str(task.input.profile or "change").strip() or "change"
    claim = (
        {"kind": profile, "title": title, "statement": statement}
        if (title or statement)
        else {}
    )
    decision, costs, _ = review(
        ctx, claim=claim, hybrid_report={}, validation_report={"status": "passed"},
        diff_text=diff, changed_paths=patch_paths(diff), profile=profile,
    )
    findings = [
        {
            "path": str(item.path),
            "line": int(item.line),
            "severity": item.severity.value,
            "category": item.category.value if hasattr(item.category, "value") else str(item.category),
            "blocking": bool(item.blocking),
        }
        for item in decision.findings
        if item.blocking
    ]
    return "ok", {
        "findings": findings,
        "verdict": decision.verdict.value,
        "cost_cny": round(sum(float(item.get("cost_cny", 0.0)) for item in costs), 4),
        "llm_calls": len(costs),
    }


__all__ = ["run_review_task"]
