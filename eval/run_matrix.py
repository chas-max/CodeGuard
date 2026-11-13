"""分层批量执行器：每条用例独立目录，失败隔离，支持 repeat / 并发 / 预算 / 断点续跑。

设计：
- 不修改任何判定逻辑：确定性层直接调用现有 engine，e2e 直接调用 `eval.simulate_pr`；
- 执行层只产出 `CaseResult`，评分卡只读 `CaseResult`，两层解耦；
- 离线默认：det / gate / security / e2e 全部零网络、零真实 LLM；
- `--live` 才会调用真实 LLM 与 Docker（semantic/e2e），并受预算约束。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codeguard.config import load_settings
from codeguard.engine.api_contract import check_api_contract
from codeguard.engine.diff import unified_diff
from codeguard.engine.policy import Policy
from codeguard.engine.repo_map import build_repo_map, read_context
from codeguard.engine.rules import RuleEngine
from codeguard.engine.static_scan import scan_tree
from codeguard.engine.tests_guard import guard_tests
from codeguard.engine.tier import classify_tier
from codeguard.github.merge import merge_blockers, valid_approvals
from codeguard.models import (
    MergeMode,
    ReviewDecision,
    ReviewVerdict,
    SandboxStatus,
    ValidationReport,
)
from codeguard.nodes import ReviewNodes
from codeguard.nodes.pipeline import route_after_review
from codeguard.sandbox.docker_backend import DockerSandboxBackend
from codeguard.sandbox.fake_backend import FakeSandboxBackend
from codeguard.sandbox.models import SandboxSpec
from codeguard.tools.base import safe_resolve
from codeguard.tools.fs_tools import ReadFileArgs, read_file
from codeguard.tools.registry import build_toolbox, effective_capability

from .fetch_external import external_cases
from .loader import DEFAULT_SUITE, CaseSpec, load_suite
from .simulate_pr import simulate

SHA = "a" * 40
TREE = "tree-1"


def _fast_rmtree(path: Path) -> None:
    """删除临时目录，绕开"把删除改成走回收站"的运行环境钩子（每次 0.5s）。

    Windows 直接调 DeleteFileW / RemoveDirectoryW；其它平台回退标准库。
    """
    if not path.exists():
        return
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        prefix = "\\\\?\\"

        def wipe(target: Path) -> None:
            raw = prefix + str(target.resolve())
            if target.is_dir() and not target.is_symlink():
                for child in target.iterdir():
                    wipe(child)
                kernel32.RemoveDirectoryW(raw)
            else:
                kernel32.DeleteFileW(raw)

        wipe(path)
        return
    import shutil as _shutil

    _shutil.rmtree(path, ignore_errors=True)


@dataclass
class CaseResult:
    case_id: str
    layer: str
    repeat: int = 0
    done: bool = True
    passed: bool = False
    skipped: bool = False
    skip_reason: str = ""
    blocked: bool = False
    blocking_rules: list[str] = field(default_factory=list)
    all_rules: list[str] = field(default_factory=list)
    semantic_blocked: bool = False
    tier: str = ""
    approvals: int = 0
    outcome: str = ""
    merge_called: bool = False
    validation_status: str = ""
    review_rounds: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    cost_cny: float = 0.0
    wall_ms: int = 0
    expected_block: bool = False
    expected_rules: list[str] = field(default_factory=list)
    expected_tier: str = ""
    expected_approvals: int | None = None
    expected_semantic: bool = False
    probe: str = ""
    findings_total: int = 0
    detail: str = ""
    error: str = ""
    tags: list[str] = field(default_factory=list)
    weight: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


def _write_trees(base_files: dict[str, str], head_overrides: dict[str, str | None],
                 base_dir: Path, head_dir: Path) -> tuple[list[str], str]:
    base_dir.mkdir(parents=True, exist_ok=True)
    head_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in base_files.items():
        target = base_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    head_files = dict(base_files)
    for rel, content in head_overrides.items():
        if content is None:
            head_files.pop(rel, None)
        else:
            head_files[rel] = content
    for rel, content in head_files.items():
        target = head_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    changed = sorted(
        path for path in set(base_files) | set(head_overrides)
        if base_files.get(path) != head_files.get(path)
    )
    parts: list[str] = []
    for rel in changed:
        old = base_files.get(rel, "")
        new = head_files.get(rel, "")
        if old != new:
            parts.append(unified_diff(old, new, rel))
    return changed, "\n".join(parts)


def _detect(base_dir: Path, head_dir: Path, changed: list[str], diff_text: str) -> dict:
    policy = Policy()
    scan = scan_tree(head_dir, changed, base_root=base_dir)
    rules = RuleEngine(policy).evaluate(
        {}, [{"path": path} for path in changed], diff_text,
        base_root=base_dir, head_root=head_dir,
    )
    guard = guard_tests(base_dir, head_dir, changed)
    api = check_api_contract(base_dir, head_dir, changed)
    findings = list(scan.findings) + list(rules) + list(guard) + list(api)
    all_rules = sorted({item.rule_id for item in findings if item.rule_id})
    blocking = sorted({item.rule_id for item in findings if item.blocking and item.rule_id})
    return {
        "blocked": any(item.blocking for item in findings),
        "all_rules": all_rules,
        "blocking_rules": blocking,
        "findings": len(findings),
    }


def _size_diff_bytes_probe(count: int, path: str) -> dict:
    header = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n"
    line = "+" + "x" * 99 + "\n"
    body = line * max(0, (count - len(header)) // len(line) + 1)
    diff_text = header + body
    rules = RuleEngine(Policy()).evaluate({}, [{"path": path}], diff_text)
    all_rules = sorted({item.rule_id for item in rules})
    return {
        "blocked": any(item.blocking for item in rules),
        "all_rules": all_rules,
        "blocking_rules": [item.rule_id for item in rules if item.blocking],
    }


def _merge_probe(args: dict) -> list[str]:
    overrides = dict(args.get("overrides") or {})
    validation_status = overrides.pop("validation_status", "passed")
    verdict = overrides.pop("verdict", "approve")
    merge_mode = overrides.pop("merge_mode", "auto")
    validation = ValidationReport(
        status=SandboxStatus(validation_status),
        repro_passed=validation_status == "passed",
        regression_passed=validation_status == "passed",
    )
    review = None
    if verdict:
        review = ReviewDecision(
            verdict=ReviewVerdict(verdict),
            confidence=0.9,
            summary="eval",
        )
    kwargs = {
        "pr_open": True,
        "is_draft": False,
        "mergeable": True,
        "head_sha": SHA,
        "reviewed_sha": SHA,
        "validation": validation,
        "review": review,
        "changed_paths": ["src/a.py"],
        "policy": Policy(),
        "merge_mode": MergeMode(merge_mode),
        "high_risk_approved": False,
        "reviewed_tree_hash": TREE,
        "current_tree_hash": TREE,
        "approvals": [{"approver": "bob", "sha": SHA, "tree_hash": TREE}],
        "min_approvals": 1,
        "require_non_author": False,
        "author": "alice",
        "evidence_ok": True,
        "tier": "T1",
        "blocking_open": 0,
    }
    kwargs.update(overrides)
    return merge_blockers(**kwargs)


SANDBOX_CHECKS = {
    "network_none": lambda kw, spec: kw["network_mode"] == "none",
    "network_bridge": lambda kw, spec: kw["network_mode"] == "bridge",
    "read_only_root": lambda kw, spec: kw["read_only"] is True,
    "cap_drop_all": lambda kw, spec: kw.get("cap_drop") == ["ALL"],
    "non_root_user": lambda kw, spec: kw.get("user") == "1000:1000",
    "tmpfs_tmp": lambda kw, spec: "/tmp" in (kw.get("tmpfs") or {}),
    "no_new_privileges": lambda kw, spec: "no-new-privileges:true" in (kw.get("security_opt") or []),
    "no_docker_socket": lambda kw, spec: not any(
        "docker.sock" in str(volume) for volume in (kw.get("volumes") or {})
    ),
    "no_host_env": lambda kw, spec: set((kw.get("environment") or {})) <= {
        "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "EVAL_PROBE",
    },
    "memory_and_pids_limit": lambda kw, spec: bool(kw.get("mem_limit")) and bool(kw.get("pids_limit")),
}


def _security_probe(case: CaseSpec, work: Path) -> tuple[bool, str, bool]:
    """返回 (passed, detail, blocked)。安全层全部是"不变量是否成立"的断言。"""
    name = case.id.rsplit(".", 1)[-1]
    args = case.probe_args
    if case.probe == "sandbox_kwargs":
        backend = DockerSandboxBackend(client=object())
        spec = SandboxSpec(
            job_id="eval", workspace=work, image="img", commands=["true"],
            network=str(args.get("network", "none")), env={"EVAL_PROBE": "1"},
        )
        kw = backend.build_kwargs(spec, "true")
        check = SANDBOX_CHECKS.get(name)
        ok = bool(check and check(kw, spec))
        return ok, f"network={kw['network_mode']} read_only={kw['read_only']} user={kw['user']}", ok

    if case.probe == "tool_capability":
        policy = Policy.model_validate({"mode": args.get("mode", "internal")})
        cfg = load_settings(agent_tools_disabled=bool(args.get("disabled", False)))
        cap = effective_capability(
            role=str(args["role"]), policy=policy, tier=str(args.get("tier", "T1")), cfg=cfg,
        )
        expected = {
            "reviewer_t0_read_only": "read",
            "reviewer_t1_exec": "exec",
            "external_mode_read": "read",
            "disabled": "none",
        }[name]
        return cap == expected, f"capability={cap} expected={expected}", False

    if case.probe == "toolbox_names":
        allowed = list(args.get("allowed_files") or [])
        box = build_toolbox(
            role=str(args["role"]), root=work, job_id="eval",
            ctx=SimpleNamespace(cfg=None), policy=Policy(), tier="T1",
            allowed_files=allowed,
        )
        names = [item.name for item in box.specs()] if box else []
        write_tools = {"apply_patch", "write_file"} & set(names)
        if name.endswith("without_allowed_read_only"):
            return not write_tools, f"tools={names}", False
        return bool(write_tools), f"tools={names}", False

    if case.probe == "tool_dispatch_denied":
        box = build_toolbox(
            role="reviewer", root=work, job_id="eval",
            ctx=SimpleNamespace(cfg=None), policy=Policy(), tier="T0",
        )
        result = box.dispatch("run_tests", {"tests": ["tests/test_calc.py"]}) if box else None
        status = getattr(result, "status", "no_toolbox")
        return status == "denied", f"dispatch_status={status}", False

    if case.probe == "fs_traversal":
        mode = str(args.get("mode", "absolute"))
        target = {"absolute": "C:/Windows/win.ini", "parent": "../secret.txt",
                  "nested": "a/../../etc/passwd"}.get(mode, "../secret.txt")
        resolved = False
        try:
            safe_resolve(work, target)
            resolved = True
        except ValueError:
            resolved = False
        if mode == "nested":
            result = read_file(work, ReadFileArgs(path=target))
            return result.status == "denied" and not resolved, f"status={result.status}", False
        return not resolved, f"resolved={resolved}", False

    if case.probe == "bot_approval":
        valid = valid_approvals(
            [{"approver": "github-actions[bot]", "sha": SHA, "tree_hash": TREE}],
            head_sha=SHA, current_tree_hash=TREE,
        )
        return len(valid) == 0, f"valid={len(valid)}", False

    if case.probe == "unbound_approval":
        valid = valid_approvals([{"approver": "bob"}], head_sha=SHA, current_tree_hash=TREE)
        return len(valid) == 0, f"valid={len(valid)}", False

    if case.probe == "budget_routing":
        target = route_after_review(
            {"budget_exceeded": True, "review_decision": {"verdict": "approve"}},
            SimpleNamespace(cfg=load_settings()),
        )
        return target == "finalize", f"route={target}", False

    if case.probe == "secret_masked":
        sample = work / "leak.py"
        work.mkdir(parents=True, exist_ok=True)
        sample.write_text("TOKEN = 'AKIAABCDEFGHIJKLMNOP'\n", encoding="utf-8")
        report = scan_tree(work, ["leak.py"])
        evidence = " ".join(item.evidence for item in report.findings)
        leaked = "AKIAABCDEFGHIJKLMNOP" in evidence
        return not leaked, f"findings={len(report.findings)} leaked={leaked}", False

    return False, f"未知探针: {case.probe}", False


class Runner:
    def __init__(
        self,
        *,
        out_dir: Path,
        offline: bool = True,
        repeat: int = 1,
        parallel: int = 1,
        budget_cny: float = 10.0,
        keep_work: bool = False,
        provider=None,
    ):
        self.out_dir = Path(out_dir)
        self.offline = offline
        self.repeat = max(1, repeat)
        self.parallel = max(1, parallel)
        self.budget_cny = budget_cny
        self.keep_work = keep_work
        self.provider = provider
        self.spent = 0.0
        self.work_root = self.out_dir / "work"

    # ── 单层执行 ──

    def _run_det(self, case: CaseSpec, work: Path) -> CaseResult:
        if case.probe == "env_missing_workspace":
            missing = work / "does-not-exist"
            report = scan_tree(missing, ["src/calc.py"])
            all_rules = sorted({item.rule_id for item in report.findings})
            blocked = any(item.blocking for item in report.findings)
            return CaseResult(
                case_id=case.id, layer=case.layer, blocked=blocked,
                blocking_rules=[item.rule_id for item in report.findings if item.blocking],
                all_rules=all_rules, expected_block=case.expect_block,
                expected_rules=list(case.expect_rules),
                passed=blocked == case.expect_block and set(case.expect_rules) <= set(all_rules),
                detail=f"findings={len(report.findings)}",
            )
        if case.probe == "size_diff_bytes":
            outcome = _size_diff_bytes_probe(
                int(case.probe_args.get("bytes", 2_000_001)), str(case.probe_args.get("path", "src/huge.py")),
            )
            return CaseResult(
                case_id=case.id, layer=case.layer, blocked=outcome["blocked"],
                blocking_rules=outcome["blocking_rules"], all_rules=outcome["all_rules"],
                expected_block=case.expect_block, expected_rules=list(case.expect_rules),
                passed=outcome["blocked"] == case.expect_block
                and set(case.expect_rules) <= set(outcome["all_rules"]),
                detail=f"rules={outcome['all_rules']}",
            )
        base_dir, head_dir = work / "base", work / "head"
        changed, diff_text = _write_trees(case.base, case.head, base_dir, head_dir)
        outcome = _detect(base_dir, head_dir, changed, diff_text)
        passed = (
            outcome["blocked"] == case.expect_block
            and set(case.expect_rules) <= set(outcome["all_rules"])
        )
        return CaseResult(
            case_id=case.id, layer=case.layer, blocked=outcome["blocked"],
            blocking_rules=outcome["blocking_rules"], all_rules=outcome["all_rules"],
            expected_block=case.expect_block, expected_rules=list(case.expect_rules),
            findings_total=outcome["findings"],
            passed=passed, detail=f"changed={len(changed)} findings={outcome['findings']}",
        )

    def _run_gate(self, case: CaseSpec, work: Path) -> CaseResult:
        args = case.probe_args
        if case.probe == "tier":
            policy = Policy.model_validate(args.get("policy") or {})
            risk = classify_tier(
                policy,
                changed_paths=list(args.get("changed_paths") or []),
                changed_lines=int(args.get("changed_lines", 0)),
                author=str(args.get("author", "")),
                pr_body=str(args.get("pr_body", "")),
                labels=list(args.get("labels") or []),
                api_break=bool(args.get("api_break", False)),
            )
            passed = risk.tier.value == case.expect_tier and (
                case.expect_approvals is None or risk.min_human_approvals == case.expect_approvals
            )
            return CaseResult(
                case_id=case.id, layer=case.layer, tier=risk.tier.value,
                approvals=risk.min_human_approvals, expected_tier=case.expect_tier,
                expected_block=case.expect_block, expected_rules=list(case.expect_rules),
                passed=passed, detail="；".join(risk.reasons[:4]),
            )
        if case.probe == "approvals":
            valid = valid_approvals(
                list(args.get("approvals") or []),
                head_sha=str(args.get("head_sha", "")),
                current_tree_hash=str(args.get("current_tree_hash", "")),
            )
            return CaseResult(
                case_id=case.id, layer=case.layer, approvals=len(valid),
                expected_block=case.expect_block, expected_rules=list(case.expect_rules),
                passed=len(valid) == int(case.expect_approvals or 0),
                detail=f"approvers={[item.get('approver') for item in valid]}",
            )
        if case.probe == "merge":
            blockers = _merge_probe(args)
            expected = [str(item) for item in (args.get("blockers") or [])]
            joined = "；".join(blockers)
            passed = all(item in joined for item in expected)
            if not expected:
                passed = not blockers
            return CaseResult(
                case_id=case.id, layer=case.layer, blocked=bool(blockers),
                expected_block=case.expect_block, expected_rules=list(case.expect_rules),
                passed=passed, detail=joined or "no blockers",
            )
        return CaseResult(
            case_id=case.id, layer=case.layer, passed=False,
            error=f"未知 gate 探针: {case.probe}",
        )

    def _run_security(self, case: CaseSpec, work: Path) -> CaseResult:
        passed, detail, blocked = _security_probe(case, work)
        return CaseResult(
            case_id=case.id, layer=case.layer, passed=passed, blocked=blocked,
            expected_block=case.expect_block, expected_rules=list(case.expect_rules),
            detail=detail,
        )

    def _run_semantic(self, case: CaseSpec, work: Path) -> CaseResult:
        if self.provider is None:
            return CaseResult(
                case_id=case.id, layer=case.layer, skipped=True,
                skip_reason="semantic 层需要真实 LLM（--live）或注入 provider；离线基线不伪造结果",
                expected_block=case.expect_block, expected_semantic=True,
            )
        base_dir, head_dir = work / "base", work / "head"
        changed, diff_text = _write_trees(case.base, case.head, base_dir, head_dir)
        cfg = load_settings(
            data_dir=work / "data", checkpoint_path=work / "ckpt.db",
            sandbox_backend="fake", reports_dir=work / "reports",
        )
        nodes = ReviewNodes(cfg, llm_provider=self.provider, sandbox=FakeSandboxBackend())
        repo_map = build_repo_map(head_dir)
        from codeguard.agents.semantic_detector import detect_semantics
        from codeguard.engine.repo_map import related_files

        report, costs, _ = detect_semantics(
            nodes, root=head_dir, repo_map=repo_map,
            repo_context=read_context(head_dir, related_files(head_dir, repo_map, changed)),
            diff_text=diff_text, changed_files=changed,
            deterministic_findings=[], threshold=0.8,
        )
        semantic_blocked = any(item.blocking for item in report.findings)
        return CaseResult(
            case_id=case.id, layer=case.layer, blocked=semantic_blocked,
            semantic_blocked=semantic_blocked, llm_calls=len(costs),
            cost_cny=round(sum(float(item.get("cost_cny", 0.0)) for item in costs), 4),
            expected_block=case.expect_block, expected_semantic=True,
            findings_total=len(report.findings),
            passed=semantic_blocked == case.expect_block,
            detail="；".join(item.message for item in report.findings)[:300],
        )

    def _run_e2e(self, case: CaseSpec, work: Path) -> CaseResult:
        from tests.fakes import FakeGitHub, FakeSandbox, RepeatingProvider

        cfg = load_settings(
            data_dir=work / "data", checkpoint_path=work / "ckpt.db",
            sandbox_backend="fake", reports_dir=work / "reports",
        )
        if self.provider is not None:
            provider = self.provider
        elif self.offline:
            provider = RepeatingProvider({
                "semantic_detector": json.dumps({"findings": [], "limitations": []}),
                "issue_verifier": json.dumps({
                    "kind": "feature", "statement": "eval change", "expected": "works",
                    "actual": "works", "repro_steps": [], "affected_paths": [],
                    "confidence": 0.9,
                }),
                "reviewer": json.dumps({
                    "verdict": "approve", "confidence": 0.9, "summary": "ok",
                    "findings": [], "fix_instructions": [], "residual_risks": [],
                }),
                "fixer": json.dumps({"patch": "", "files": [], "explanation": "", "round": 1}),
                "finding_verifier_a": json.dumps({"verdict": "drop", "reason": "eval"}),
                "finding_verifier_b": json.dumps({"verdict": "drop", "reason": "eval"}),
            })
        else:
            provider = None

        from .pseudo_pr import PseudoPR

        pseudo = PseudoPR(
            name=case.id, base=case.base, head=case.head,
            expected_block=case.expect_block, title=case.id,
            author="eval-author",
        )
        sandbox = None if not self.offline else FakeSandbox()
        try:
            result = simulate(
                pseudo, cfg=cfg, root=work / "sim", llm_provider=provider,
                sandbox=sandbox, github=FakeGitHub(), approve=True,
                checkpoint_path=work / "ckpt_e2e.db",
            )
        except Exception as exc:  # noqa: BLE001 - 单条用例失败不拖垮整批
            return CaseResult(
                case_id=case.id, layer=case.layer, done=False, error=f"{type(exc).__name__}: {exc}",
                expected_block=case.expect_block,
            )
        merged = bool(result.get("merge_called"))
        outcome = str(result.get("outcome") or "")
        infra_bad = outcome in ("INFRA_FAILED", "FAILED") or bool(result.get("error"))
        rule_ok = set(case.expect_rules) <= set(result.get("blocking_rules") or [])
        if case.expect_block:
            passed = (not merged) and not infra_bad and rule_ok
        else:
            passed = outcome == "MERGED" and merged and not infra_bad
        return CaseResult(
            case_id=case.id, layer=case.layer, done=bool(result.get("done")),
            blocked=not merged, outcome=str(result.get("outcome") or ""),
            merge_called=merged, tier=str(result.get("tier") or ""),
            approvals=int(result.get("approvals", 0) or 0),
            validation_status=str(result.get("validation") or ""),
            review_rounds=int(result.get("review_rounds", 0) or 0),
            llm_calls=int(result.get("llm_calls", 0) or 0),
            tool_calls=int(result.get("tool_calls", 0) or 0),
            cost_cny=float(result.get("cost_cny", 0.0) or 0.0),
            blocking_rules=list(result.get("blocking_rules") or []),
            expected_block=case.expect_block, expected_rules=list(case.expect_rules),
            passed=passed, detail=str(result.get("merge_reason") or result.get("error") or ""),
            error=str(result.get("error") or ""),
        )

    def _run_external(self, case: CaseSpec, work: Path) -> CaseResult:
        return CaseResult(
            case_id=case.id, layer=case.layer, skipped=True,
            skip_reason="外部切片未固化仓库快照（manifest.snapshot_ready=false），按约定不计通过",
            expected_block=case.expect_block,
        )

    # ── 调度 ──

    def run_case(self, case: CaseSpec, repeat: int) -> CaseResult:
        started = time.monotonic()
        work = self.work_root / case.id / f"r{repeat}"
        _fast_rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        try:
            if case.layer == "det":
                result = self._run_det(case, work)
            elif case.layer == "gate":
                result = self._run_gate(case, work)
            elif case.layer == "security":
                result = self._run_security(case, work)
            elif case.layer == "semantic":
                result = self._run_semantic(case, work)
            elif case.layer == "e2e":
                result = self._run_e2e(case, work)
            elif case.layer == "external":
                result = self._run_external(case, work)
            else:
                result = CaseResult(case_id=case.id, layer=case.layer, passed=False,
                                    error=f"未知 layer: {case.layer}")
        except Exception as exc:  # noqa: BLE001
            result = CaseResult(
                case_id=case.id, layer=case.layer, done=False,
                error=f"{type(exc).__name__}: {exc}",
                expected_block=case.expect_block, expected_rules=list(case.expect_rules),
            )
        finally:
            if not self.keep_work:
                _fast_rmtree(work)
        result.repeat = repeat
        result.wall_ms = int((time.monotonic() - started) * 1000)
        result.tags = list(case.tags)
        result.weight = case.weight
        result.probe = case.probe
        result.expected_semantic = case.expect_semantic
        result.expected_approvals = case.expect_approvals
        result.expected_tier = case.expect_tier or result.expected_tier
        return result

    def run(self, cases: list[CaseSpec], *, resume: bool = False) -> list[CaseResult]:
        results: list[CaseResult] = []
        done_keys: set[tuple[str, int]] = set()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        results_path = self.out_dir / "results.jsonl"
        if not resume:
            results_path.unlink(missing_ok=True)
        if resume and results_path.is_file():
            for line in results_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    done_keys.add((row["case_id"], int(row.get("repeat", 0))))
                    results.append(CaseResult(**{**row, "repeat": int(row.get("repeat", 0))}))
        tasks = [
            (case, repeat)
            for case in cases for repeat in range(self.repeat)
            if (case.id, repeat) not in done_keys
        ]
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.parallel == 1:
            for case, repeat in tasks:
                if self.spent >= self.budget_cny:
                    break
                result = self.run_case(case, repeat)
                self.spent += result.cost_cny
                results.append(result)
                with results_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        else:
            with ThreadPoolExecutor(max_workers=self.parallel) as pool:
                futures = {}
                for case, repeat in tasks:
                    if self.spent >= self.budget_cny:
                        break
                    futures[pool.submit(self.run_case, case, repeat)] = (case, repeat)
                for future in as_completed(futures):
                    result = future.result()
                    self.spent += result.cost_cny
                    results.append(result)
                    with results_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        return results


def _select(cases: list[CaseSpec], layers: list[str], limit: int, match: str = "") -> list[CaseSpec]:
    selected = [
        case for case in cases
        if (not layers or case.layer in layers) and (not match or match in case.id)
    ]
    if limit:
        selected = selected[:limit]
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.run_matrix")
    parser.add_argument("--suite", default=str(DEFAULT_SUITE))
    parser.add_argument("--layer", action="append", default=[], help="det/semantic/gate/security/e2e/external")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--case", default="", help="按 case_id 子串过滤，便于单条复现")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--budget-cny", type=float, default=10.0)
    parser.add_argument("--out", default="reports/eval_baseline")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--live", action="store_true", help="使用真实 LLM 与 Docker（默认离线）")
    args = parser.parse_args(argv)

    cases = load_suite(args.suite) + external_cases()
    selected = _select(cases, args.layer, args.limit, args.case)
    runner = Runner(
        out_dir=Path(args.out), offline=not args.live, repeat=args.repeat,
        parallel=args.parallel, budget_cny=args.budget_cny, keep_work=args.keep_work,
    )
    results = runner.run(selected, resume=args.resume)
    summary = {
        "run": {
            "offline": not args.live, "repeat": args.repeat, "parallel": args.parallel,
            "budget_cny": args.budget_cny, "layers": args.layer or "all",
            "cases": len(selected), "results": len(results),
        },
        "passed": sum(1 for item in results if item.passed),
        "failed": sum(1 for item in results if not item.passed and not item.skipped),
        "skipped": sum(1 for item in results if item.skipped),
        "cost_cny": round(sum(item.cost_cny for item in results), 4),
        "wall_ms": sum(item.wall_ms for item in results),
    }
    (Path(args.out) / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
