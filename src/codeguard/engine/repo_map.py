"""仓库地图：文件索引、语言识别、测试识别与相关文件选择。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from .diff import IGNORE_DIRS, is_probably_binary, read_text

LANGUAGE_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".md": "markdown",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".json": "json",
}

MANIFESTS = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "requirements-dev.txt",
    "Pipfile", "poetry.lock", "package.json", "package-lock.json", "pnpm-lock.yaml",
    "yarn.lock", "go.mod", "go.sum", "Cargo.toml", "pom.xml", "build.gradle",
}

TEST_RE = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*\.py$|_test\.py$|\.test\.(js|ts|tsx)$")


def language_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    return LANGUAGE_BY_EXT.get(ext, "unknown")


def is_test_path(path: str) -> bool:
    return bool(TEST_RE.search(path))


def build_repo_map(
    root: Path,
    *,
    max_files: int = 20000,
    max_file_bytes: int = 200_000,
) -> dict:
    files: list[dict] = []
    test_files: list[str] = []
    manifests: list[str] = []
    languages: dict[str, int] = {}
    if not root.is_dir():
        return {"root": str(root), "files": [], "test_files": [], "manifests": [], "languages": {}, "truncated": False}
    truncated = False
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if len(files) >= max_files:
            truncated = True
            break
        lang = language_for(rel)
        binary = is_probably_binary(path)
        item = {
            "path": rel,
            "language": lang,
            "size": size,
            "is_test": is_test_path(rel),
            "is_binary": binary or size > max_file_bytes,
        }
        files.append(item)
        if item["is_test"]:
            test_files.append(rel)
        if path.name in MANIFESTS:
            manifests.append(rel)
        languages[lang] = languages.get(lang, 0) + 1
    return {
        "root": str(root),
        "files": files,
        "test_files": test_files,
        "manifests": manifests,
        "languages": languages,
        "truncated": truncated,
    }


def _python_imports(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def related_files(root: Path, repo_map: dict, changed_paths: list[str], *, limit: int = 20) -> list[str]:
    """选择变更文件的一跳导入方、同目录测试与同名测试，作为 LLM 上下文。"""
    selected: list[str] = []
    for p in changed_paths:
        if p not in selected:
            selected.append(p)
    all_py = [
        item["path"] for item in repo_map.get("files", [])
        if item.get("language") == "python" and not item.get("is_binary")
    ]
    reverse: dict[str, list[str]] = {}
    for rel in all_py:
        text = read_text(root / rel, max_chars=20000)
        for imp in _python_imports(text):
            module = imp.replace(".", "/")
            reverse.setdefault(module, []).append(rel)
    for p in changed_paths:
        stem = Path(p).stem
        module = p[:-3].replace("/", ".") if p.endswith(".py") else stem
        for importer in reverse.get(module, []) + reverse.get(stem, []):
            if importer not in selected:
                selected.append(importer)
        parent = str(Path(p).parent.as_posix())
        for test in repo_map.get("test_files", []):
            if test not in selected and (
                test.startswith(parent + "/") or Path(test).stem in (f"test_{stem}", f"{stem}_test")
            ):
                selected.append(test)
    return selected[:limit]


def read_context(root: Path, paths: list[str], *, max_chars_per_file: int = 6000, max_total: int = 40000) -> str:
    parts: list[str] = []
    total = 0
    for rel in paths:
        path = root / rel
        if not path.is_file():
            continue
        text = read_text(path, max_chars=max_chars_per_file)
        remaining = max_total - total
        if remaining <= 0:
            break
        text = text[:remaining]
        total += len(text)
        parts.append(f"=== {rel} ===\n{text}")
    return "\n\n".join(parts)


__all__ = [
    "LANGUAGE_BY_EXT", "MANIFESTS", "language_for", "is_test_path",
    "build_repo_map", "related_files", "read_context",
]
