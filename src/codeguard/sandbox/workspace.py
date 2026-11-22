"""Job 工作区：从本地/GitHub 源码物化可写副本，避免污染宿主仓库。"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from ..engine.diff import IGNORE_DIRS, tree_hash

_TOKEN_IN_URL = re.compile(r"(x-access-token:)[^@\s]+@")
_TOKEN_LITERAL = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")


def redact_secrets(text: str) -> str:
    """克隆/推送失败信息可能带出 token，落 job report 前必须脱敏。"""
    masked = _TOKEN_IN_URL.sub(r"\1***@", str(text))
    return _TOKEN_LITERAL.sub("gh?_***", masked)


def _ignore(_dir: str, names: list[str]) -> list[str]:
    return [n for n in names if n in IGNORE_DIRS]


def materialize(source: Path, dest: Path) -> Path:
    """复制源码到 job 目录；跳过 .git/缓存/虚拟环境。"""
    if not source.is_dir():
        raise FileNotFoundError(f"源码目录不存在: {source}")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, dirs_exist_ok=True, ignore=_ignore)
    return dest


def clone_repo(repo_url: str, dest: Path, *, ref: str = "", token: str | None = None,
               timeout_sec: int = 120) -> Path:
    """浅克隆指定 ref（生产路径；测试通常用 materialize）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = repo_url
    if token and url.startswith("https://"):
        url = url.replace("https://", f"https://x-access-token:{token}@", 1)
    cmd = ["git", "-c", "http.postBuffer=16777216", "clone", "--no-checkout", "--filter=blob:none", url, str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    if proc.returncode != 0:
        raise RuntimeError(f"git clone 失败: {redact_secrets((proc.stderr or proc.stdout)[:500])}")
    if ref:
        checkout = subprocess.run(
            ["git", "-C", str(dest), "-c", "http.postBuffer=16777216", "fetch", "--depth=1", "origin", ref],
            capture_output=True, text=True, timeout=timeout_sec,
        )
        if checkout.returncode != 0:
            raise RuntimeError(f"git fetch 失败: {redact_secrets((checkout.stderr or checkout.stdout)[:500])}")
        subprocess.run(
            ["git", "-C", str(dest), "-c", "core.hooksPath=", "checkout", "--detach", "FETCH_HEAD"],
            capture_output=True, text=True, timeout=timeout_sec, check=True,
        )
    return dest


def workspace_hash(root: Path) -> str:
    return tree_hash(root)


def init_baseline_repo(root: Path) -> str:
    """在 job 私有副本里建立 git 基线提交，供 Agent 改码后用 git diff 生成补丁。

    返回错误信息，空串表示成功；已存在 .git 时直接返回（幂等）。
    """
    if not root.is_dir():
        return f"目录不存在: {root}"
    if (root / ".git").exists():
        return ""
    try:
        init = subprocess.run(
            ["git", "init", "-q"], cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        if init.returncode != 0:
            return f"git init 失败: {(init.stderr or init.stdout).strip()[:300]}"
        # 让 pytest/编译缓存不进入 Agent 生成的补丁。
        excludes = [f"{name}/" for name in sorted(IGNORE_DIRS)] + ["*.pyc", ".pytest_cache/"]
        try:
            exclude_file = root / ".git" / "info" / "exclude"
            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            exclude_file.write_text("\n".join(excludes) + "\n", encoding="utf-8")
        except OSError:
            pass
        commands = [
            ["git", "config", "user.email", "codeguard@local"],
            ["git", "config", "user.name", "CodeGuard"],
            ["git", "add", "-A"],
            ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "codeguard baseline", "--allow-empty"],
        ]
        for command in commands:
            proc = subprocess.run(
                command, cwd=str(root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
            if proc.returncode != 0:
                return f"{' '.join(command)} 失败: {(proc.stderr or proc.stdout).strip()[:300]}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"git 初始化失败: {exc}"
    return ""


__all__ = [
    "materialize", "clone_repo", "workspace_hash", "init_baseline_repo",
    "redact_secrets", "IGNORE_DIRS",
]
