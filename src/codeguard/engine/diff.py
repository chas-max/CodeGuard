"""Diff、路径、哈希与补丁应用工具。"""
from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

from ..errors import NonRetryableError
from ..models import validate_repo_path

IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", "dist", "build"}


def is_binary_bytes(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return True
    sample = data[:4096]
    if not sample:
        return False
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(sample) < 0.75


def is_probably_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return is_binary_bytes(fh.read(8192))
    except OSError:
        return True


def read_text(path: Path, *, max_chars: int | None = None) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    return text[:max_chars] if max_chars is not None else text


def tree_hash(root: Path, *, ignore_dirs: set[str] | None = None) -> str:
    """对目录内容做稳定哈希，用于确认验证对象与审查对象一致。"""
    ignore = ignore_dirs or IGNORE_DIRS
    digest = hashlib.sha256()
    if not root.is_dir():
        return ""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in ignore for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        try:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            continue
    return digest.hexdigest()


def diff_stats(diff_text: str) -> dict:
    additions = deletions = files = 0
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git"):
            files += 1
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return {"files": files, "additions": additions, "deletions": deletions, "lines": additions + deletions}


_PATCH_PATH_RE = re.compile(r"^(?:\+\+\+|---)\s+[ab]/(.+)$")


def patch_paths(patch_text: str) -> list[str]:
    """从 unified diff 提取目标/源路径（去重、规范化）。"""
    paths: list[str] = []
    for line in (patch_text or "").splitlines():
        m = _PATCH_PATH_RE.match(line.strip())
        if not m:
            continue
        raw = m.group(1).strip()
        if raw == "/dev/null":
            continue
        try:
            normalized = validate_repo_path(raw)
        except ValueError as exc:
            raise NonRetryableError(f"补丁包含非法路径 {raw!r}: {exc}") from exc
        if normalized not in paths:
            paths.append(normalized)
    return paths


def apply_patch(root: Path, patch_text: str, *, check_only: bool = False) -> tuple[bool, str]:
    """在 root 内用 git apply 应用补丁。返回 (ok, output)。

    调用方必须保证 root 是受控工作区；补丁路径已由 patch_paths 校验。
    """
    if not patch_text.strip():
        return False, "空补丁"
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as fh:
        fh.write(patch_text)
        patch_file = Path(fh.name)
    try:
        # 仓库文件可能是 CRLF；模型产出的补丁通常是 LF，忽略空白差异才能正常匹配。
        cmd = ["git", "apply", "--ignore-whitespace",
               "--check" if check_only else "--whitespace=nowarn", str(patch_file)]
        proc = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git apply 失败: {exc}"
    finally:
        try:
            patch_file.unlink()
        except OSError:
            pass


def worktree_diff(root: Path, *, max_chars: int = 200_000) -> str:
    """返回工作区相对基线提交的 unified diff（包含新增的未跟踪文件）。

    只用于 job 私有副本（fix_root）；先 `git add -A` 再读 cached diff，
    因此 Agent 用 write_file 新建的文件也会进入补丁。
    """
    if not (root / ".git").exists():
        return ""
    try:
        add = subprocess.run(
            ["git", "-C", str(root), "add", "-A", "--", "."],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if add.returncode != 0:
            return ""
        proc = subprocess.run(
            ["git", "-C", str(root), "-c", "core.hooksPath=", "diff", "--cached", "--no-color", "-U3"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "")[:max_chars]


def unified_diff(old: str, new: str, path: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


__all__ = [
    "IGNORE_DIRS", "is_binary_bytes", "is_probably_binary", "read_text", "tree_hash",
    "diff_stats", "patch_paths", "apply_patch", "unified_diff", "worktree_diff",
]
