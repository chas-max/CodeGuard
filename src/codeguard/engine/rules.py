"""确定性规则引擎：路径、大小、依赖、CI、策略、scope 等。"""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from ..models import Finding, FindingCategory, FindingSource, Severity
from .diff import diff_stats
from .policy import Policy
from .repo_map import is_test_path

FORBIDDEN_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519", "secrets*", "**/secrets*"]
DEPENDENCY_PATTERNS = [
    "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "Pipfile",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.mod", "go.sum",
    "Cargo.toml", "pom.xml",
]
CI_PATTERNS = [".github/workflows/**", ".gitlab-ci.yml", ".circleci/**", "Jenkinsfile", "azure-pipelines.yml"]


def _f(rule_id: str, category: FindingCategory, severity: Severity, path: str, message: str,
       evidence: str, *, auto_fixable: bool = False) -> Finding:
    return Finding(
        source=FindingSource.deterministic,
        rule_id=rule_id,
        category=category,
        severity=severity,
        blocking=severity in (Severity.critical, Severity.major),
        auto_fixable=auto_fixable,
        path=path,
        message=message,
        evidence=evidence,
        suggestion="请人工确认该变更" if not auto_fixable else "修复 Agent 可尝试处理",
    )


class RuleEngine:
    def __init__(self, policy: Policy):
        self.policy = policy

    def _enabled(self, rule_id: str) -> bool:
        return not self.policy.disabled(rule_id)

    def evaluate(
        self,
        pr_context: dict,
        changed_files: list[dict],
        diff_text: str,
        *,
        base_root: Path | None = None,
        head_root: Path | None = None,
    ) -> list[Finding]:
        findings: list[Finding] = []
        paths = [f.get("path", "") for f in changed_files if f.get("path")]
        stats = diff_stats(diff_text)
        files_count = len(paths)

        if self._enabled("SIZE-001"):
            if files_count > self.policy.limits.max_files:
                findings.append(_f(
                    "SIZE-001", FindingCategory.policy, Severity.major, "",
                    f"变更文件数超限: {files_count} > {self.policy.limits.max_files}",
                    f"changed_files={files_count}",
                ))
            if stats["lines"] > self.policy.limits.max_changed_lines:
                findings.append(_f(
                    "SIZE-001", FindingCategory.policy, Severity.major, "",
                    f"改动行数超限: {stats['lines']} > {self.policy.limits.max_changed_lines}",
                    f"changed_lines={stats['lines']}",
                ))
            if len(diff_text.encode("utf-8")) > self.policy.limits.max_diff_bytes:
                findings.append(_f(
                    "SIZE-001", FindingCategory.policy, Severity.major, "",
                    "diff 字节数超限", f"diff_bytes={len(diff_text.encode('utf-8'))}",
                ))

        for path in paths:
            if self._enabled("PATH-002") and any(fnmatch(path, p) for p in FORBIDDEN_PATTERNS):
                findings.append(_f(
                    "PATH-002", FindingCategory.security, Severity.critical, path,
                    "修改了禁止提交的敏感/密钥路径", f"{path} 命中禁止路径规则",
                ))
            if self._enabled("PATH-001") and self.policy.is_protected(path):
                findings.append(_f(
                    "PATH-001", FindingCategory.policy, Severity.major, path,
                    "修改了受保护路径", f"{path} 命中 protected_paths",
                ))
            if self._enabled("CI-001") and any(fnmatch(path, p) for p in CI_PATTERNS):
                findings.append(_f(
                    "CI-001", FindingCategory.ci, Severity.major, path,
                    "修改了 CI/构建配置", f"{path} 命中 CI 路径",
                ))
            if self._enabled("DEP-001") and any(fnmatch(path, p) for p in DEPENDENCY_PATTERNS):
                findings.append(_f(
                    "DEP-001", FindingCategory.dependency, Severity.major, path,
                    "修改了依赖/lockfile", f"{path} 命中依赖文件规则",
                ))
            if self._enabled("POL-001") and fnmatch(path, ".codeguard/**"):
                findings.append(_f(
                    "POL-001", FindingCategory.policy, Severity.major, path,
                    "PR 修改了 CodeGuard 策略文件", f"{path} 会影响检测规则，必须人工确认",
                ))

        # 变更范围：非测试文件占绝大多数且改动很大时，提示 scope 风险。
        if self._enabled("SCOPE-001") and files_count >= 10:
            non_test = [p for p in paths if not is_test_path(p)]
            if non_test and len(non_test) >= files_count * 0.8:
                findings.append(_f(
                    "SCOPE-001", FindingCategory.scope, Severity.minor, "",
                    "改动范围较大且与单个 bugfix 关联性存疑",
                    f"{files_count} files, {len(non_test)} non-test",
                    auto_fixable=True,
                ))

        return findings


__all__ = ["RuleEngine", "FORBIDDEN_PATTERNS", "DEPENDENCY_PATTERNS", "CI_PATTERNS"]
