from __future__ import annotations

import json
from pathlib import Path

from codeguard.agents.fixer import run_coder_agent
from codeguard.agents.qa_checker import run_qa_checker
from codeguard.config import Settings
from codeguard.engine.diff import unified_diff
from codeguard.engine.policy import Policy
from codeguard.memory import LocalHashEmbedder, ProceduralMemoryStore
from codeguard.models import FixDirective, ProceduralMemoryRecord
from codeguard.tools.registry import build_toolbox
from tests.fakes import FakeProvider, FakeSandbox, init_git


class _Ctx:
    def __init__(self, provider: FakeProvider, memory_store=None):
        self.cfg = Settings(sandbox_backend="fake")
        self.sandbox = FakeSandbox()
        self.memory_store = memory_store
        self._provider = provider
        self._llms: dict[str, object] = {}

    def llm(self, role: str):
        if role not in self._llms:
            self._llms[role] = self._provider(role)
        return self._llms[role]


def test_qa_checker_blocks_unauthorized_file_drift():
    directive = FixDirective(
        directive_id="dir_1",
        round_no=1,
        needs_modification=True,
        target_findings=[{"directive_item_id": "F-001", "path": "src/a.py", "message": "fix a"}],
        allowed_source_files=["src/a.py"],
        allowed_test_addition_files=[],
    )
    # Coder 越界修改了 src/unrelated.py
    drift_patch = unified_diff("x = 1\n", "x = 2\n", "src/unrelated.py")
    provider = FakeProvider({
        "qa_checker": [json.dumps({
            "verdict": "pass",
            "is_aligned": True,
            "has_scope_drift": False,
            "alignment_score": 0.9,
            "summary": "looks fine",
        }, ensure_ascii=False)],
    })
    report, _, _ = run_qa_checker(
        _Ctx(provider),
        fix_directive=directive,
        fix_result={"round_no": 1, "explanation": "edited"},
        actual_patch=drift_patch,
        round_no=1,
        max_fix_rounds=5,
    )
    # 确定性越界护栏必须强行覆盖为 revise 并指出偏航
    assert report.verdict == "revise"
    assert report.has_scope_drift is True
    assert any("未授权" in d for d in report.drift_details)


def test_coder_queries_rag_memory_and_qa_passes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def div(a, b):\n    return a / b\n", encoding="utf-8")
    init_git(repo)

    store = ProceduralMemoryStore(tmp_path / "mem.db", embedder=LocalHashEmbedder(dim=64))
    try:
        mid = store.upsert(ProceduralMemoryRecord(
            repo="acme/calc",
            pr_number=1,
            categories=["correctness"],
            file_patterns=["calc.py"],
            problem_pattern="div 函数缺少 b == 0 除零检查",
            fix_strategy="添加 if b == 0: return 0.0",
            patch_summary="+ if b == 0: return 0.0",
            qa_verdict="pass",
            ci_passed=True,
            human_approved=True,
            final_outcome="MERGED",
            memory_type="fix_pattern",
        ))
        provider = FakeProvider({
            "coder": [
                json.dumps({
                    "tool": "search_procedural_memory",
                    "args": {"query": "除零检查 calc.py"},
                }, ensure_ascii=False),
                json.dumps({
                    "tool": "edit_file",
                    "args": {
                        "path": "calc.py",
                        "old_text": "    return a / b",
                        "new_text": "    if b == 0:\n        return 0.0\n    return a / b",
                    },
                }, ensure_ascii=False),
                json.dumps({
                    "addressed_finding_ids": ["F-001"],
                    "explanation": "参考历史记忆补齐 b == 0 守卫",
                    "self_check_passed": True,
                }, ensure_ascii=False),
            ],
            "qa_checker": [json.dumps({
                "verdict": "pass",
                "is_aligned": True,
                "has_scope_drift": False,
                "alignment_score": 1.0,
                "item_checks": [{"finding_id": "F-001", "addressed": True, "aligned": True, "note": "精准修复"}],
                "summary": "一致通过",
            }, ensure_ascii=False)],
        })
        ctx = _Ctx(provider, memory_store=store)
        box = build_toolbox(
            role="coder",
            root=repo,
            job_id="j-coder",
            ctx=ctx,
            policy=Policy(),
            tier="T1",
            allowed_files=["calc.py"],
            pr_context={"repo": "acme/calc"},
        )
        directive = FixDirective(
            directive_id="dir_1",
            round_no=1,
            needs_modification=True,
            target_findings=[{"directive_item_id": "F-001", "path": "calc.py", "message": "div 未检查 b == 0"}],
            allowed_source_files=["calc.py"],
        )
        fix_res, _, _ = run_coder_agent(
            ctx,
            fix_directive=directive,
            pr_brief={"problem_summary": "fix div"},
            diff_text="",
            tools=box,
            root=repo,
        )
        assert mid in fix_res.rag_memory_ids_used
        assert "if b == 0:" in fix_res.patch

        qa_report, _, _ = run_qa_checker(
            ctx,
            fix_directive=directive,
            fix_result=fix_res.model_dump(mode="json"),
            actual_patch=fix_res.patch,
            round_no=1,
            max_fix_rounds=5,
        )
        assert qa_report.verdict == "pass"
        assert qa_report.is_aligned is True
    finally:
        store.close()
