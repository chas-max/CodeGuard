"""伪 PR：把「一次 PR」降级成本地 base/head 双树 + 伪造的 PR 元数据。

真 PR 的创建、评论、合并只是流水线末端的一步，评测关心的是「审查判定」，所以
这里不连 GitHub、不跑 docker：数据集样本 → `PseudoPR` → 本地双树 +
`PullRequestContext`，再交给现有判定链路（确定性四件套或整张图）。

夹具是数据而不是代码：新增场景只要加一条 `PseudoPR`，不要改判定逻辑。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from codeguard.models import FileChange, PullRequestContext

# 最小可运行仓库骨架：任一 pseudo PR 都叠加在它之上，避免每个场景重复写样板。
DEFAULT_FILES: dict[str, str] = {
    "src/calc.py": "def divide(a, b):\n    return a / b\n",
    "tests/test_calc.py": "from src.calc import divide\n\ndef test_ok():\n    assert divide(4, 2) == 2\n",
}

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


@dataclass(frozen=True)
class PseudoPR:
    """一次「被模仿」的 PR：base 树、head 树、期望判定。

    `head` 里值为 `None` 表示该文件在 head 中被删除（用于 TEST-001 等场景）。
    """

    name: str
    source: str = "synthetic"
    profile: str = "change"
    base: dict[str, str] = field(default_factory=dict)
    head: dict[str, str | None] = field(default_factory=dict)
    expected_block: bool = False
    expected_rules: tuple[str, ...] = ()
    title: str = ""
    body: str = ""
    author: str = "eval"
    repo: str = "eval/pseudo"
    number: int = 1

    # ── 树 ──

    def base_files(self) -> dict[str, str]:
        return {**DEFAULT_FILES, **self.base}

    def head_files(self) -> dict[str, str]:
        files = self.base_files()
        for path, content in self.head.items():
            if content is None:
                files.pop(path, None)
            else:
                files[path] = content
        return files

    def changed(self) -> list[str]:
        base, head = self.base_files(), self.head_files()
        paths = set(base) | set(head)
        return sorted(p for p in paths if base.get(p) != head.get(p))

    # ── 仿真的 PR 元数据 ──

    def pull_request(self) -> PullRequestContext:
        base, head = self.base_files(), self.head_files()
        status = []
        for path in self.changed():
            if path not in head:
                status.append(FileChange(path=path, status="removed", deletions=1))
            elif path not in base:
                status.append(FileChange(path=path, status="added", additions=1))
            else:
                status.append(FileChange(path=path, status="modified", additions=1))
        return PullRequestContext(
            repo=self.repo,
            number=self.number,
            base_ref="main",
            base_sha=BASE_SHA,
            head_ref=f"feature/{self.name}"[:60],
            head_sha=HEAD_SHA,
            title=self.title or self.name,
            body=self.body,
            author=self.author,
            profile=self.profile,
            changed_files=status,
            changed_file_count=len(status),
        )

    def materialize(self, root: Path) -> "MaterializedPR":
        return materialize(self, root)


@dataclass(frozen=True)
class MaterializedPR:
    pseudo: PseudoPR
    base_dir: Path
    head_dir: Path
    changed: tuple[str, ...]

    @property
    def pr(self) -> PullRequestContext:
        return self.pseudo.pull_request()

    def overrides(self) -> dict:
        """交给 `CodeGuardRuntime.start` 的本地覆盖项：不克隆、不触网。"""
        return {"repo_root": str(self.head_dir), "base_root": str(self.base_dir)}


def _git(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), capture_output=True, check=False)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "eval@example.com"],
                   capture_output=True, check=False)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "eval"],
                   capture_output=True, check=False)
    subprocess.run(["git", "-C", str(path), "add", "-A"], capture_output=True, check=False)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], capture_output=True, check=False)


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def materialize(pseudo: PseudoPR, root: Path) -> MaterializedPR:
    """把伪 PR 落成两个本地目录（各自一个 git 仓库），不涉及任何远程。"""
    base_dir = root / "base"
    head_dir = root / "head"
    _write_tree(base_dir, pseudo.base_files())
    _write_tree(head_dir, pseudo.head_files())
    _git(base_dir)
    _git(head_dir)
    return MaterializedPR(
        pseudo=pseudo, base_dir=base_dir, head_dir=head_dir,
        changed=tuple(pseudo.changed()),
    )


def from_case(case: dict) -> PseudoPR:
    """把 `eval/fixtures/cases.json` 的一条记录转成伪 PR。"""
    expected = case.get("expected") or {}
    return PseudoPR(
        name=str(case["name"]),
        source=str(case.get("source", "synthetic")),
        profile=str(case.get("profile", "change")),
        base=dict(case.get("base") or {}),
        head=dict(case.get("head") or {}),
        expected_block=bool(expected.get("block", False)),
        expected_rules=tuple(str(r) for r in expected.get("rules") or ()),
        title=str(case.get("title", "")),
        body=str(case.get("body", "")),
    )


__all__ = [
    "DEFAULT_FILES", "BASE_SHA", "HEAD_SHA",
    "PseudoPR", "MaterializedPR", "materialize", "from_case",
]
