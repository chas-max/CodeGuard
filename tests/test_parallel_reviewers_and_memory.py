from __future__ import annotations

import json
from pathlib import Path

from codeguard.agents.reviewers import run_perspective_review
from codeguard.engine.synthesis import finding_fingerprint, synthesize_reviewer_outputs
from codeguard.models import ReviewerMemory
from tests.fakes import FakeProvider


class _FakeCtx:
    def __init__(self, provider: FakeProvider):
        self._provider = provider

    def llm(self, role: str):
        return self._provider(role)


def test_short_term_memory_suppresses_repeated_issue_in_incremental_round():
    repeat_finding = {
        "category": "correctness",
        "severity": "major",
        "blocking": True,
        "path": "calc.py",
        "line": 2,
        "message": "divide_safe 未处理 b == 0 除零异常",
        "evidence": "calc.py:2 return a / b",
        "suggestion": "增加 b == 0 守卫",
        "confidence": 0.9,
    }
    fp = finding_fingerprint(repeat_finding)
    mem = ReviewerMemory(
        role="review_logic",
        rounds_completed=1,
        raised_fingerprints=[fp],
    )
    provider = FakeProvider({
        "review_logic": [json.dumps({
            "role": "review_logic",
            "needs_modification": True,
            "summary": "重复旧问题",
            "findings": [repeat_finding],
            "memory_notes": [],
            "confidence": 0.9,
        }, ensure_ascii=False)],
    })
    # 增量 diff 修改的是 utils.py，而非 calc.py
    inc_diff = "diff --git a/utils.py b/utils.py\n+++ b/utils.py\n@@ -1,1 +1,2 @@\n+# ok\n"
    out, _, _ = run_perspective_review(
        _FakeCtx(provider),
        role="review_logic",
        pr_brief={"problem_summary": "fix"},
        diff_text=inc_diff,
        repo_context="",
        guidance="",
        deterministic_findings=[],
        memory=mem,
        round_no=2,
        round_mode="incremental",
    )
    assert out.findings == []
    assert out.needs_modification is False


def test_synthesize_grounds_dedups_and_authorizes_tests_only_when_requested(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("def run(x):\n    return 10 / x\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    repo_files = {"src/app.py", "tests/test_app.py"}

    outputs = {
        "review_logic": {
            "role": "review_logic",
            "needs_modification": True,
            "findings": [
                {
                    "category": "correctness",
                    "severity": "major",
                    "blocking": True,
                    "path": "src/app.py",
                    "line": 2,
                    "message": "`run` 缺少 `x == 0` 除零检查",
                    "evidence": "src/app.py:2 return 10 / x",
                    "suggestion": "在 x == 0 时抛出 ValueError",
                    "confidence": 0.9,
                },
                {
                    # 幻觉路径应被 Grounding 硬校验过滤
                    "category": "correctness",
                    "severity": "critical",
                    "blocking": True,
                    "path": "src/nonexistent.py",
                    "line": 99,
                    "message": "幻觉问题",
                    "evidence": "fake",
                    "confidence": 0.95,
                },
            ],
        },
        "review_security": {
            "role": "review_security",
            "needs_modification": True,
            "findings": [
                {
                    # 与 review_logic 指向同一位置同一问题，应融合去重并叠加 reported_by
                    "category": "correctness",
                    "severity": "critical",
                    "blocking": True,
                    "path": "src/app.py",
                    "line": 2,
                    "message": "`run` 函数 `x == 0` 时未做除零校验导致异常崩溃",
                    "evidence": "src/app.py:2 10 / x",
                    "suggestion": "校验 x != 0",
                    "confidence": 0.85,
                },
            ],
        },
        "review_architecture": {"role": "review_architecture", "needs_modification": False, "findings": []},
        "review_tests": {
            "role": "review_tests",
            "needs_modification": True,
            "findings": [
                {
                    "category": "tests",
                    "severity": "major",
                    "blocking": True,
                    "path": "tests/test_app.py",
                    "line": 1,
                    "message": "缺少 run(0) 边界单元测试覆盖",
                    "evidence": "tests/test_app.py 仅有 test_ok",
                    "suggestion": "在 tests/test_app.py 补充 test_run_zero",
                    "confidence": 0.9,
                },
            ],
        },
    }

    reconciled, _, directive, notes = synthesize_reviewer_outputs(
        reviewer_outputs=outputs,
        deterministic_findings=[],
        previous_findings=[],
        root=tmp_path,
        repo_files=repo_files,
        max_nits=5,
        block_threshold=0.8,
        round_no=1,
        allowed_source_files=["src/app.py"],
        candidate_test_files=["tests/test_app.py"],
    )
    assert any("nonexistent.py" in n for n in notes)
    # 两条同根因问题融合为 1 条，加上 1 条测试问题，共 2 条
    assert len(reconciled) == 2
    app_finding = next(f for f in reconciled if f["path"] == "src/app.py")
    assert sorted(app_finding["reported_by"]) == ["review_logic", "review_security"]
    assert app_finding["severity"] == "critical"
    assert directive.needs_modification is True
    assert "src/app.py" in directive.allowed_source_files
    # 因为 review_tests 提出了阻塞项，授权测试文件追加
    assert "tests/test_app.py" in directive.allowed_test_addition_files
