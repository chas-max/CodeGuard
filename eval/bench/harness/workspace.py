"""外部仓库工作区：复制快照、应用补丁、计算树哈希、清理。

快照目录只读；所有写操作发生在临时副本里，避免污染 `snapshots/`。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", "dist", "build",
)


def _force_rmtree(path: Path) -> None:
    """git 对象文件是只读的，Windows 上直接 rmtree 会残留目录。"""
    if not path.exists():
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in [*files, *dirs]:
            try:
                os.chmod(os.path.join(root, name), 0o700)
            except OSError:
                continue
    shutil.rmtree(path, ignore_errors=True)


@dataclass
class Workspace:
    root: Path
    snapshot: Path

    def cleanup(self) -> None:
        _force_rmtree(self.root)


def prepare_workspace(snapshot: Path | str, root: Path | str) -> Workspace:
    snapshot_path = Path(snapshot)
    if not snapshot_path.is_dir():
        raise FileNotFoundError(f"快照不存在: {snapshot_path}")
    root_path = Path(root)
    if root_path.exists():
        _force_rmtree(root_path)
    root_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(snapshot_path, root_path, ignore=_IGNORE)
    # git apply 需要一个工作树；不复制 .git，直接初始化一个空仓库即可。
    subprocess.run(
        ["git", "init", "-q"], cwd=str(root_path), check=False, capture_output=True,
    )
    # 关掉换行转换，避免不同开发机的 autocrlf 让全新索引立刻显示“已修改”。
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=str(root_path), check=False, capture_output=True,
    )
    # 把快照内容登记进索引，`git status --porcelain` 才只反映补丁带来的变更（F1 探针依赖）。
    subprocess.run(
        ["git", "add", "-A"], cwd=str(root_path), check=False, capture_output=True,
    )
    return Workspace(root=root_path, snapshot=snapshot_path)


def apply_candidate_patch(workspace: Workspace, patch_text: str, *,
                          check_only: bool = False) -> tuple[bool, str]:
    from codeguard.engine.diff import apply_patch

    return apply_patch(workspace.root, patch_text, check_only=check_only)


def workspace_tree_hash(workspace: Workspace) -> str:
    from codeguard.engine.diff import tree_hash

    return tree_hash(workspace.root)


__all__ = ["Workspace", "prepare_workspace", "apply_candidate_patch", "workspace_tree_hash"]
