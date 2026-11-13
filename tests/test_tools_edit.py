"""写工具：只允许 fixer、只写允许文件、禁止测试/受保护路径与大小上限。"""
from __future__ import annotations

from pathlib import Path

from codeguard.config import Settings
from codeguard.engine.diff import tree_hash, unified_diff
from codeguard.engine.policy import Policy
from codeguard.tools.registry import build_toolbox
from tests.fakes import FakeSandbox, init_git


class Ctx:
    def __init__(self):
        self.cfg = Settings()
        self.sandbox = FakeSandbox()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "fix"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".codeguard").mkdir()
    (root / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text("def test_f():\n    assert True\n", encoding="utf-8")
    (root / ".codeguard" / "policy.yml").write_text("mode: internal\n", encoding="utf-8")
    init_git(root)
    return root


def _box(root: Path, allowed: list[str] | None = None):
    return build_toolbox(
        role="fixer", root=root, job_id="j", ctx=Ctx(), policy=Policy(), tier="T1",
        allowed_files=allowed or ["src/a.py"],
    )


def test_write_file_updates_allowed_file(tmp_path: Path):
    root = _repo(tmp_path)
    before = tree_hash(root)
    box = _box(root)
    result = box.dispatch("write_file", {"path": "src/a.py", "content": "def f():\n    return 2\n"})
    assert result.ok
    assert (root / "src" / "a.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"
    assert result.meta["tree_hash"] != before


def test_write_file_rejects_unlisted_and_tests(tmp_path: Path):
    root = _repo(tmp_path)
    box = _box(root, allowed=["src/a.py", "tests/test_a.py", ".codeguard/policy.yml"])
    assert box.dispatch("write_file", {"path": "src/other.py", "content": "x"}).status == "denied"
    assert box.dispatch("write_file", {"path": "tests/test_a.py", "content": "x"}).status == "denied"
    assert box.dispatch("write_file", {"path": ".codeguard/policy.yml", "content": "x"}).status == "denied"


def test_write_file_rejects_oversize(tmp_path: Path):
    root = _repo(tmp_path)
    box = _box(root)
    result = box.dispatch("write_file", {"path": "src/a.py", "content": "x" * 200_001})
    assert result.status in ("denied", "invalid_args")


def test_apply_patch_uses_existing_validation(tmp_path: Path):
    root = _repo(tmp_path)
    old = (root / "src" / "a.py").read_text(encoding="utf-8")
    new = "def f():\n    if True:\n        return 2\n"
    patch = unified_diff(old, new, "src/a.py")
    box = _box(root)
    result = box.dispatch("apply_patch", {"patch": patch})
    assert result.ok, result.error
    assert "return 2" in (root / "src" / "a.py").read_text(encoding="utf-8")
    assert result.meta["files"] == ["src/a.py"]


def test_apply_patch_rejects_out_of_scope(tmp_path: Path):
    root = _repo(tmp_path)
    old = (root / "tests" / "test_a.py").read_text(encoding="utf-8")
    patch = unified_diff(old, "def test_f():\n    assert 1 == 1\n", "tests/test_a.py")
    box = _box(root)
    result = box.dispatch("apply_patch", {"patch": patch})
    assert result.status == "failed"
    assert "未授权" in result.error


def test_non_fixer_has_no_write_tools(tmp_path: Path):
    root = _repo(tmp_path)
    box = build_toolbox(
        role="reviewer", root=root, job_id="j", ctx=Ctx(), policy=Policy(), tier="T1",
        allowed_files=["src/a.py"],
    )
    names = {spec.name for spec in box.specs()}
    assert "write_file" not in names and "apply_patch" not in names
