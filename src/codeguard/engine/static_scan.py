"""确定性静态扫描：语法、危险调用、secret、冲突标记、二进制/大文件。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from ..models import (
    DeterministicReport,
    Finding,
    FindingCategory,
    FindingSource,
    Severity,
)
from .diff import is_probably_binary, read_text, tree_hash
from .repo_map import language_for

FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "os.system", "os.popen",
    "subprocess.call", "subprocess.run", "subprocess.Popen", "pickle.loads",
    "marshal.loads", "shutil.rmtree",
}

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("generic_token", re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"][^'\"]{16,}['\"]")),
]

CONFLICT_RE = re.compile(r"^(<<<<<<<|=======|>>>>>>>)", re.MULTILINE)


def _finding(
    rule_id: str,
    category: FindingCategory,
    severity: Severity,
    path: str,
    message: str,
    *,
    line: int = 0,
    evidence: str = "",
    blocking: bool | None = None,
    auto_fixable: bool = False,
) -> Finding:
    return Finding(
        source=FindingSource.deterministic,
        rule_id=rule_id,
        category=category,
        severity=severity,
        blocking=(severity == Severity.critical) if blocking is None else blocking,
        auto_fixable=auto_fixable,
        path=path,
        line=line,
        message=message,
        evidence=evidence or message,
    )


def _scan_python(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        return [_finding(
            "SYN-001", FindingCategory.syntax, Severity.critical, path,
            f"语法错误: {exc.msg}", line=exc.lineno or 0,
            evidence=f"{path}:{exc.lineno or 0} SyntaxError: {exc.msg}",
            auto_fixable=True,
        )]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        try:
            name = ast.unparse(node.func)
        except Exception:  # noqa: BLE001
            continue
        if name in FORBIDDEN_CALLS:
            findings.append(_finding(
                "SEC-002", FindingCategory.security, Severity.major, path,
                f"检测到高风险调用: {name}", line=getattr(node, "lineno", 0),
                evidence=f"{path}:{getattr(node, 'lineno', 0)} call {name}",
                auto_fixable=True,
            ))
    return findings


def _scan_secrets(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for kind, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(_finding(
                    "SEC-001", FindingCategory.security, Severity.critical, path,
                    f"疑似泄露 secret: {kind}", line=line_no,
                    evidence=f"{path}:{line_no} 命中 {kind}（内容已隐藏）",
                    blocking=True,
                ))
                break
    return findings


def scan_tree(
    root: Path,
    changed_paths: list[str],
    *,
    base_root: Path | None = None,
) -> DeterministicReport:
    """扫描候选树的变更文件；base_root 保留给后续基线对比扩展。"""
    findings: list[Finding] = []
    scanned = 0
    notes: list[str] = []
    if not root.is_dir():
        return DeterministicReport(
            findings=[_finding(
                "ENV-001", FindingCategory.other, Severity.critical, "",
                "候选工作区不存在", evidence=f"missing workspace: {root}",
            )],
            tree_hash="",
        )
    for rel in changed_paths:
        path = root / rel
        if not path.is_file():
            continue
        scanned += 1
        if is_probably_binary(path):
            notes.append(f"跳过二进制/超大文件: {rel}")
            continue
        text = read_text(path)
        if CONFLICT_RE.search(text):
            findings.append(_finding(
                "SYN-001", FindingCategory.syntax, Severity.critical, rel,
                "文件包含未解决的合并冲突标记", line=0,
                evidence=f"{rel} 含 <<<<<<< / ======= / >>>>>>> 标记",
                auto_fixable=True,
            ))
        findings.extend(_scan_secrets(rel, text))
        if language_for(rel) == "python":
            findings.extend(_scan_python(rel, text))
    return DeterministicReport(findings=findings, tree_hash=tree_hash(root), scanned_files=scanned, notes=notes)


__all__ = ["FORBIDDEN_CALLS", "SECRET_PATTERNS", "scan_tree"]
