"""外部基准执行 harness：工作区、pytest、补丁来源与按任务类型的执行器。"""
from __future__ import annotations

from .fix_task import build_f1_probe, run_fix_task, workspace_changed_paths
from .patch_source import PatchResult, resolve_patch
from .pytest_runner import TestRun, prepare_environment, run_tests
from .review_task import run_review_task
from .security_task import run_security_task
from .workspace import (
    Workspace,
    apply_candidate_patch,
    prepare_workspace,
    workspace_tree_hash,
)

__all__ = [
    "Workspace", "prepare_workspace", "apply_candidate_patch", "workspace_tree_hash",
    "TestRun", "prepare_environment", "run_tests",
    "PatchResult", "resolve_patch",
    "run_fix_task", "run_review_task", "run_security_task",
    "build_f1_probe", "workspace_changed_paths",
]
