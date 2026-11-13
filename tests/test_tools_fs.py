"""只读检索工具：路径边界、大小/二进制限制与截断。"""
from __future__ import annotations

from pathlib import Path

from codeguard.engine.repo_map import build_repo_map
from codeguard.tools.fs_tools import (
    GrepArgs,
    ListDirArgs,
    ReadFileArgs,
    RelatedFilesArgs,
    grep,
    list_dir,
    read_file,
    related_files,
)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".git").mkdir()
    (root / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("import a\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text("from src.a import f\n", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return root


def test_read_file_returns_line_range(tmp_path: Path):
    root = _repo(tmp_path)
    result = read_file(root, ReadFileArgs(path="src/a.py", start_line=2, end_line=2))
    assert result.ok
    assert "return 1" in result.content
    assert "def f" not in result.content
    assert result.meta["total_lines"] == 2


def test_read_file_rejects_traversal_and_absolute(tmp_path: Path):
    root = _repo(tmp_path)
    assert read_file(root, ReadFileArgs(path="../secret.txt")).status == "denied"
    assert read_file(root, ReadFileArgs(path="/etc/passwd")).status == "denied"
    assert read_file(root, ReadFileArgs(path="C:/Windows/win.ini")).status == "denied"


def test_read_file_rejects_ignore_dirs_and_missing(tmp_path: Path):
    root = _repo(tmp_path)
    assert read_file(root, ReadFileArgs(path=".git/config")).status == "denied"
    assert read_file(root, ReadFileArgs(path="src/missing.py")).status == "failed"


def test_read_file_rejects_binary_and_huge(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "blob.bin").write_bytes(b"\x00\x01\x02")
    result = read_file(root, ReadFileArgs(path="blob.bin"))
    assert result.status == "failed"
    (root / "huge.py").write_text("x = 1\n" * 200_000, encoding="utf-8")
    assert read_file(root, ReadFileArgs(path="huge.py")).status == "failed"


def test_read_file_truncates_long_files(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "long.py").write_text("\n".join(f"line{i}" for i in range(1000)), encoding="utf-8")
    result = read_file(root, ReadFileArgs(path="long.py"), max_chars=200)
    assert result.meta["end_line"] == 400
    assert result.meta["truncated"] is True
    assert result.output_chars <= 200


def test_list_dir_depth_and_ignore(tmp_path: Path):
    root = _repo(tmp_path)
    result = list_dir(root, ListDirArgs(path=".", depth=2))
    assert "src/" in result.content
    assert "src/a.py" in result.content
    assert ".git" not in result.content


def test_grep_matches_and_invalid_regex(tmp_path: Path):
    root = _repo(tmp_path)
    repo = build_repo_map(root)
    result = grep(root, GrepArgs(pattern=r"def f"), repo_map=repo)
    assert result.ok and "src/a.py:1:" in result.content
    bad = grep(root, GrepArgs(pattern="["), repo_map=repo)
    assert bad.status == "invalid_args"


def test_grep_glob_and_limit(tmp_path: Path):
    root = _repo(tmp_path)
    repo = build_repo_map(root)
    only_tests = grep(root, GrepArgs(pattern="import", glob="tests/**"), repo_map=repo)
    assert "tests/test_a.py" in only_tests.content
    assert "src/b.py" not in only_tests.content
    limited = grep(root, GrepArgs(pattern="import", max_matches=1), repo_map=repo)
    assert limited.meta["matches"] == 1


def test_related_files_selects_importers(tmp_path: Path):
    root = _repo(tmp_path)
    repo = build_repo_map(root)
    result = related_files(root, RelatedFilesArgs(paths=["src/a.py"]), repo_map=repo)
    assert result.ok
    assert "src/b.py" in result.content
    assert related_files(root, RelatedFilesArgs(paths=[])).status == "invalid_args"
