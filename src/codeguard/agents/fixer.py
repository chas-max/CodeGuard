"""Coder / Fixer Agent：支持 FixDirective + RAG 程序化记忆 + Docker 沙箱多轮修复。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..engine.diff import patch_paths, tree_hash, unified_diff, worktree_diff
from ..engine.patch import preserves_existing_tests, validate_fix_patch
from ..engine.repo_map import is_test_path
from ..models import FixDirective, FixExecutionResult, FixProposal
from ..prompts.fixer import coder_prompt, fixer_prompt
from ..prompts.tools import tool_protocol_block
from ..tools.base import normalize_rel, safe_resolve
from ..tools.edit_tools import FORBIDDEN_PREFIXES
from ..tools.spec import ToolBox, ToolLimits
from .base import call_json, call_json_with_tools


class _EditOp(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1)
    search: str = Field(min_length=1)
    replace: str = ""

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        data = dict(raw)
        if "path" not in data and "file" in data:
            data["path"] = data["file"]
        if "search" not in data and "old_text" in data:
            data["search"] = data["old_text"]
        if "replace" not in data and "new_text" in data:
            data["replace"] = data["new_text"]
        return data


class _CoderPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    addressed_finding_ids: list[str] = Field(default_factory=list)
    unfixable_finding_ids: list[str] = Field(default_factory=list)
    edits: list[_EditOp] = Field(default_factory=list)
    patch: str = ""
    files: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    explanation: str = ""
    rag_memory_ids_used: list[str] = Field(default_factory=list)
    self_check_passed: bool = False
    confidence: float = 0.0


def compile_edits_to_patch(
    root: Path,
    edits: list[dict[str, Any] | _EditOp],
    *,
    allowed_source_files: list[str],
    allowed_test_addition_files: list[str] | None = None,
) -> tuple[str, list[str], str]:
    """将结构化 Search-and-Replace `edits` 编译为可应用的 unified diff。"""
    if not edits:
        return "", [], ""
    ops = [_EditOp.model_validate(item) for item in edits]
    allowed_src = {normalize_rel(p) for p in allowed_source_files if str(p).strip()}
    allowed_tests = {normalize_rel(p) for p in (allowed_test_addition_files or []) if str(p).strip()}
    buffers: dict[str, tuple[str, str]] = {}

    for idx, op in enumerate(ops, start=1):
        rel, path = safe_resolve(root, op.path)
        if rel == ".":
            raise ValueError(f"edits[{idx}].path 必须是文件路径")
        if any(rel.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            raise ValueError(f"edits[{idx}] 禁止修改受保护路径: {rel}")
        if is_test_path(rel):
            if rel not in allowed_tests:
                raise ValueError(f"edits[{idx}] 禁止修改测试文件: {rel}")
        elif rel not in allowed_src:
            raise ValueError(f"edits[{idx}] 文件不在允许修复范围内: {rel}")
        if not path.is_file():
            raise ValueError(f"edits[{idx}] 目标文件不存在: {rel}")

        if rel in buffers:
            orig, cur = buffers[rel]
        else:
            orig = path.read_text(encoding="utf-8")
            cur = orig

        needle = op.search.replace("\r\n", "\n")
        replacement = op.replace.replace("\r\n", "\n")
        matches = cur.count(needle)
        if matches == 0:
            raise ValueError(f"edits[{idx}] 在 {rel} 中未找到匹配片段: {needle[:80]!r}")
        if matches > 1:
            raise ValueError(f"edits[{idx}] 在 {rel} 中匹配到 {matches} 处（非唯一）: {needle[:80]!r}")
        buffers[rel] = (orig, cur.replace(needle, replacement, 1))

    chunks: list[str] = []
    changed_files: list[str] = []
    for rel, (orig, updated) in buffers.items():
        if orig == updated:
            continue
        if is_test_path(rel):
            guard_err = preserves_existing_tests(orig, updated, path=rel)
            if guard_err:
                raise ValueError(guard_err)
        diff_chunk = unified_diff(orig, updated, rel)
        if not diff_chunk.strip():
            continue
        if not diff_chunk.endswith("\n"):
            diff_chunk += "\n"
        chunks.append(diff_chunk)
        changed_files.append(rel)

    combined = "".join(chunks)
    if not combined.strip():
        return "", [], "edits 未产生有效代码差异"

    scope = sorted(allowed_src | allowed_tests)
    _, err = validate_fix_patch(
        root, combined, allowed_files=scope, forbidden_prefixes=FORBIDDEN_PREFIXES,
    )
    if err:
        raise ValueError(f"edits 编译后的补丁未通过校验: {err}")
    return combined, changed_files, f"compiled {len(ops)} edit(s)"


def run_coder_agent(
    ctx: Any,
    *,
    fix_directive: FixDirective | dict[str, Any],
    pr_brief: dict[str, Any],
    diff_text: str,
    validation_logs: str = "",
    qa_feedback: dict[str, Any] | None = None,
    repo_context: str = "",
    round_no: int = 1,
    max_rounds: int = 5,
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
    root: Path | None = None,
    tier: str = "T1",
) -> tuple[FixExecutionResult, list[dict], list[dict]]:
    """运行 Coder Agent：结合 FixDirective 与 RAG 记忆修复代码。"""
    directive = (
        fix_directive
        if isinstance(fix_directive, FixDirective)
        else FixDirective.model_validate(fix_directive)
    )
    limits = limits or ToolLimits()
    tools_block = tool_protocol_block(tools.specs(), limits) if tools else ""
    prompt = coder_prompt(
        fix_directive=directive.model_dump(mode="json"),
        pr_brief=pr_brief,
        diff_text=diff_text,
        validation_logs=validation_logs,
        qa_feedback=qa_feedback,
        repo_context=repo_context,
        round_no=round_no,
        max_rounds=max_rounds,
        tools_block=tools_block,
    )
    payload, costs, events = call_json_with_tools(
        ctx,
        "coder",
        prompt,
        _CoderPayload,
        tools=tools,
        limits=limits,
        temperature=0.1,
        tier=tier,
    )

    patch_text = payload.patch
    modified = list(payload.modified_files or payload.files)
    # 优先检查隔离副本中工具落盘的真实 git diff
    wt_patch = worktree_diff(root) if root is not None and (root / ".git").exists() else ""
    if wt_patch.strip():
        patch_text = wt_patch
        modified = patch_paths(wt_patch)
    elif payload.edits and root is not None:
        compiled, c_files, _ = compile_edits_to_patch(
            root,
            list(payload.edits),
            allowed_source_files=directive.allowed_source_files,
            allowed_test_addition_files=directive.allowed_test_addition_files,
        )
        if compiled.strip():
            patch_text = compiled
            modified = c_files

    rag_used = list(dict.fromkeys(
        list(payload.rag_memory_ids_used or [])
        + list(getattr(tools, "used_memory_ids", []) or [])
    ))
    addressed = list(payload.addressed_finding_ids)
    if not addressed and patch_text.strip():
        addressed = [
            str(item.get("directive_item_id") or item.get("id") or "")
            for item in directive.target_findings
            if item.get("directive_item_id") or item.get("id")
        ]

    t_hash = tree_hash(root) if root is not None and root.is_dir() else ""
    result = FixExecutionResult(
        round_no=round_no,
        addressed_finding_ids=addressed,
        unfixable_finding_ids=list(payload.unfixable_finding_ids),
        patch=patch_text,
        modified_files=modified,
        explanation=payload.explanation.strip(),
        rag_memory_ids_used=rag_used,
        self_check_passed=bool(payload.self_check_passed or any(
            ev.get("tool") in ("run_tests", "run_check") and ev.get("status") == "ok"
            for ev in events
        )),
        tree_hash=t_hash,
    )
    return result, costs, events


# ────────────────────────────── 兼容旧接口 ──────────────────────────────


def _proposal(payload: _CoderPayload, round_no: int) -> FixProposal:
    return FixProposal(
        patch=payload.patch,
        files=list(payload.files or payload.modified_files),
        explanation=payload.explanation,
        round=round_no,
        applies_cleanly=False,
    )


def propose_fix(
    ctx,
    *,
    diff_text: str,
    findings: list[dict],
    validation_logs: str,
    repo_context: str,
    allowed_files: list[str],
    round_no: int,
) -> tuple[FixProposal, list[dict]]:
    prompt = fixer_prompt(
        diff_text=diff_text,
        findings=findings,
        validation_logs=validation_logs,
        repo_context=repo_context,
        allowed_files=allowed_files,
        round_no=round_no,
    )
    payload, costs = call_json(ctx, "fixer", prompt, _CoderPayload, temperature=0.1)
    return _proposal(payload, round_no), costs


def propose_fix_with_tools(
    ctx,
    *,
    diff_text: str,
    findings: list[dict],
    validation_logs: str,
    repo_context: str,
    allowed_files: list[str],
    round_no: int,
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
) -> tuple[FixProposal, list[dict], list[dict]]:
    limits = limits or ToolLimits()
    tools_block = tool_protocol_block(tools.specs(), limits) if tools else ""
    prompt = fixer_prompt(
        diff_text=diff_text,
        findings=findings,
        validation_logs=validation_logs,
        repo_context=repo_context,
        allowed_files=allowed_files,
        round_no=round_no,
        tools_block=tools_block,
    )
    payload, costs, events = call_json_with_tools(
        ctx, "fixer", prompt, _CoderPayload,
        tools=tools, limits=limits, temperature=0.1,
    )
    return _proposal(payload, round_no), costs, events


__all__ = [
    "compile_edits_to_patch",
    "run_coder_agent",
    "propose_fix",
    "propose_fix_with_tools",
]
