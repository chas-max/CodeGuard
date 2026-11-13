"""补丁来源：gold 用于自证 harness，system 才衡量 CodeGuard 的修复能力。"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import TaskSpec


@dataclass
class PatchResult:
    ok: bool
    patch: str = ""
    source: str = ""
    error: str = ""
    costs: list[dict] = field(default_factory=list)


def _instance(task: TaskSpec) -> dict:
    payload = task.input or {}
    instance = payload.get("instance")
    return instance if isinstance(instance, dict) else {}


def gold_patch(task: TaskSpec) -> PatchResult:
    payload = task.input or {}
    patch = str(payload.get("patch") or _instance(task).get("patch") or "")
    if not patch.strip():
        return PatchResult(False, source="gold", error="gold patch 为空")
    return PatchResult(True, patch=patch, source="gold")


def system_patch(task: TaskSpec, provider) -> PatchResult:
    if provider is None:
        return PatchResult(False, source="system", error="system 补丁来源需要 LLM provider")
    from codeguard.agents.fixer import propose_fix

    instance = _instance(task)
    problem = str(instance.get("problem_statement") or "")
    findings = [{
        "id": "eval-issue-1",
        "category": "correctness",
        "severity": "major",
        "blocking": True,
        "path": "",
        "line": 0,
        "message": problem or "修复该 Issue",
        "evidence": problem[:1000] or "no problem statement",
        "suggestion": "",
        "source": "eval",
    }]

    class _Ctx:
        def llm(self, role: str):
            return provider(role)

    try:
        proposal, costs = propose_fix(
            _Ctx(), diff_text="", findings=findings, validation_logs="",
            repo_context=problem[:4000], allowed_files=[], round_no=1,
        )
    except Exception as exc:  # noqa: BLE001 - Agent 失败是评测结果的一部分
        return PatchResult(False, source="system", error=f"{type(exc).__name__}: {exc}")
    patch = str(getattr(proposal, "patch", "") or "")
    if not patch.strip():
        return PatchResult(False, source="system", error="fixer 未产出 patch", costs=costs)
    return PatchResult(True, patch=patch, source="system", costs=costs)


def resolve_patch(task: TaskSpec, source: str = "gold", provider=None) -> PatchResult:
    if source == "gold":
        return gold_patch(task)
    if source == "system":
        return system_patch(task, provider)
    return PatchResult(False, source=str(source), error=f"未知补丁来源: {source!r}")


__all__ = ["PatchResult", "gold_patch", "system_patch", "resolve_patch"]
