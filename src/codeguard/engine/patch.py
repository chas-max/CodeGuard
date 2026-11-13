"""修复补丁校验与应用。"""
from __future__ import annotations

from pathlib import Path

from ..models import FixProposal, validate_repo_path
from .diff import apply_patch, patch_paths


def validate_fix_patch(
    root: Path,
    patch_text: str,
    *,
    allowed_files: list[str],
    forbidden_prefixes: tuple[str, ...] = (".github/workflows/", ".codeguard/"),
) -> tuple[FixProposal, str]:
    """校验补丁路径、大小与可应用性；返回 (proposal, error)。"""
    if not patch_text.strip():
        return FixProposal(patch="", files=[], applies_cleanly=False), "补丁为空"
    try:
        paths = patch_paths(patch_text)
    except Exception as exc:  # noqa: BLE001
        return FixProposal(patch=patch_text, applies_cleanly=False), str(exc)
    allowed = {validate_repo_path(p) for p in allowed_files}
    for path in paths:
        if path not in allowed:
            return (
                FixProposal(patch=patch_text, files=paths, applies_cleanly=False),
                f"补丁修改了未授权文件: {path}",
            )
        if any(path.startswith(prefix) for prefix in forbidden_prefixes):
            return (
                FixProposal(patch=patch_text, files=paths, applies_cleanly=False),
                f"补丁修改了受保护路径: {path}",
            )
    if len(patch_text) > 200_000:
        return FixProposal(patch=patch_text, files=paths, applies_cleanly=False), "补丁过大"
    ok, output = apply_patch(root, patch_text, check_only=True)
    proposal = FixProposal(
        patch=patch_text,
        files=paths,
        applies_cleanly=ok,
        explanation=output if not ok else "",
    )
    return proposal, "" if ok else output


__all__ = ["validate_fix_patch"]
