"""测试完整性守卫：删除测试、跳过测试、删断言、降低覆盖率门槛。"""
from __future__ import annotations

import re
from pathlib import Path

from ..models import Finding, FindingCategory, FindingSource, Severity
from .diff import read_text
from .repo_map import is_test_path

TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(", re.MULTILINE)
ASSERT_RE = re.compile(r"^\s*assert\b", re.MULTILINE)
SKIP_RE = re.compile(r"pytest\.mark\.(?:skip|xfail)|pytest\.skip\(", re.IGNORECASE)
COVERAGE_RE = re.compile(r"(?:fail_under|--cov-fail-under|COVERAGE_MIN_PCT)\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)")


def _tests(text: str) -> set[str]:
    return set(TEST_DEF_RE.findall(text))


def _asserts(text: str) -> int:
    return len(ASSERT_RE.findall(text))


def _finding(path: str, message: str, evidence: str) -> Finding:
    return Finding(
        source=FindingSource.deterministic,
        rule_id="TEST-001",
        category=FindingCategory.tests,
        severity=Severity.critical,
        blocking=True,
        auto_fixable=False,
        path=path,
        message=message,
        evidence=evidence,
        suggestion="恢复/保留原测试；不得通过删除、跳过或减弱断言来通过检查",
    )


def guard_tests(base_root: Path, head_root: Path, changed_paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    if not base_root.is_dir():
        return findings
    for rel in changed_paths:
        if not is_test_path(rel):
            continue
        base_path = base_root / rel
        head_path = head_root / rel
        base_text = read_text(base_path) if base_path.is_file() else ""
        head_text = read_text(head_path) if head_path.is_file() else ""
        if base_path.is_file() and not head_path.is_file():
            findings.append(_finding(rel, "测试文件被删除", f"base 存在 {rel}，head 中不存在"))
            continue
        if not base_path.is_file() and head_path.is_file():
            continue
        base_tests, head_tests = _tests(base_text), _tests(head_text)
        removed = base_tests - head_tests
        if removed:
            findings.append(_finding(
                rel, f"测试函数被删除/重命名: {', '.join(sorted(removed)[:5])}",
                f"{rel} 删除测试: {', '.join(sorted(removed)[:5])}",
            ))
        if _asserts(head_text) < _asserts(base_text):
            findings.append(_finding(
                rel, "测试断言数量减少",
                f"{base_text.count('assert')} -> {head_text.count('assert')}（{rel}）",
            ))
        if SKIP_RE.search(head_text) and not SKIP_RE.search(base_text):
            findings.append(_finding(
                rel, "新增 skip/xfail 标记",
                f"{rel} 新增 pytest skip/xfail，疑似绕过测试",
            ))
        for m in COVERAGE_RE.finditer(head_text):
            new_val = float(m.group(1))
            old = [float(x.group(1)) for x in COVERAGE_RE.finditer(base_text)]
            if old and new_val < min(old):
                findings.append(_finding(
                    rel, "覆盖率门槛被降低",
                    f"{rel}: {min(old)} -> {new_val}",
                ))
    return findings


__all__ = ["guard_tests"]
