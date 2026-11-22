"""CodeGuard v2 LangGraph 端到端集成测试：覆盖分级、双层 5 轮闭环、QA/Verify 前置、PR 提交与向量记忆。"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from codeguard.config import load_settings
from codeguard.graph import make_checkpointer
from codeguard.memory import LocalHashEmbedder, ProceduralMemoryStore
from codeguard.models import FileChange, PullRequestContext, SandboxStatus
from codeguard.nodes import ReviewNodes
from codeguard.runtime import CodeGuardRuntime
from codeguard.sandbox.models import SandboxResult
from tests.fakes import (
    PR_BRIEF_CRITICAL_JSON,
    PR_BRIEF_LOW_JSON,
    PR_BRIEF_MEDIUM_JSON,
    QA_PASS_JSON,
    REVIEW_PASS_JSON,
    FakeGitHub,
    FakeProvider,
    FakeResponse,
    FakeSandbox,
    init_git,
    make_fixture_repo,
    v2_provider_map,
)


class _ScriptedLLM:
    """支持按调用次数动态生成响应的测试 LLM 替身（可校验 prompt 内容）。"""

    def __init__(self, handler):
        self._handler = handler
        self.prompts: list[str] = []

    def invoke(self, messages):
        prompt = messages[0].content
        self.prompts.append(prompt)
        idx = len(self.prompts) - 1
        return FakeResponse(self._handler(idx, prompt))


class _DelegatingProvider:
    """结合默认静态映射与自定义角色 LLM 实例。"""

    def __init__(self, base: dict[str, list[str]], custom: dict[str, object]):
        self._fallback = FakeProvider(base)
        self._custom = custom

    def __call__(self, role: str):
        if role in self._custom:
            obj = self._custom[role]
            return obj() if callable(obj) and not hasattr(obj, "invoke") else obj
        return self._fallback(role)


def _blocking_finding(
    *,
    path: str,
    line: int,
    message: str,
    evidence: str,
    category: str = "correctness",
) -> str:
    return json.dumps({
        "needs_modification": True,
        "summary": message,
        "findings": [{
            "category": category,
            "severity": "major",
            "blocking": True,
            "path": path,
            "line": line,
            "message": message,
            "evidence": evidence,
            "suggestion": "按审查要求修复该问题",
            "confidence": 0.95,
        }],
        "memory_notes": [f"reported {path}:{line}"],
        "confidence": 0.95,
    }, ensure_ascii=False)


def test_low_importance_auto_merges_zero_human(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    for root in (base, head):
        (root / "docs").mkdir(exist_ok=True)
    (base / "docs" / "guide.md").write_text("# 指南\n", encoding="utf-8")
    (head / "docs" / "guide.md").write_text("# 指南\n新增使用说明。\n", encoding="utf-8")

    cfg = load_settings(
        data_dir=tmp_path / "data",
        checkpoint_path=tmp_path / "ckpt.db",
        sandbox_backend="fake",
        merge_mode="auto",
        max_fix_rounds=5,
        max_review_rounds=5,
    )
    provider = FakeProvider(v2_provider_map(pr_brief=PR_BRIEF_LOW_JSON))
    github = FakeGitHub()
    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox())
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ckpt.db"))
    pr = PullRequestContext(
        repo="acme/repo",
        number=101,
        base_ref="main",
        base_sha="a" * 40,
        head_ref="docs/update",
        head_sha="b" * 40,
        title="docs: 更新文档",
        body="补充使用说明",
        author="doc-writer",
        changed_files=[FileChange(path="docs/guide.md", status="modified")],
    )
    snap = runtime.start(pr, "job-v2-low", repo_root=str(head), base_root=str(base))
    values = snap["values"]
    assert snap["done"], values.get("error")
    assert values["importance_level"] == "low"
    assert values["risk_tier"]["tier"] == "T0"
    assert values["risk_tier"]["min_human_approvals"] == 0
    assert values["outcome"] == "MERGED"
    assert len(github.merged) == 1


def test_monotonic_risk_floor_blocks_llm_downgrade_and_allows_escalation(tmp_path: Path):
    # 子用例 A：敏感鉴权目录命中 T2/critical 下限，即使 pr_reader 模型建议 low 也不允许降级
    base_a, head_a = make_fixture_repo(tmp_path / "a")
    (base_a / "src" / "auth").mkdir(parents=True)
    (head_a / "src" / "auth").mkdir(parents=True)
    (base_a / "src" / "auth" / "login.py").write_text("def verify(t):\n    return bool(t)\n", encoding="utf-8")
    (head_a / "src" / "auth" / "login.py").write_text("def verify(t):\n    return len(t) > 3\n", encoding="utf-8")

    cfg_a = load_settings(
        data_dir=tmp_path / "data_a",
        checkpoint_path=tmp_path / "ck_a.db",
        sandbox_backend="fake",
        merge_mode="auto",
    )
    provider_a = FakeProvider(v2_provider_map(pr_brief=PR_BRIEF_LOW_JSON))
    github_a = FakeGitHub()
    nodes_a = ReviewNodes(cfg_a, llm_provider=provider_a, github=github_a, sandbox=FakeSandbox())
    rt_a = CodeGuardRuntime(cfg_a, nodes=nodes_a, checkpointer=make_checkpointer(tmp_path / "ck_a.db"))
    pr_a = PullRequestContext(
        repo="acme/repo",
        number=102,
        base_ref="main",
        base_sha="a" * 40,
        head_ref="feat/auth-tweak",
        head_sha="b" * 40,
        title="chore: tweak login",
        body="小改",
        author="alice",
        changed_files=[FileChange(path="src/auth/login.py")],
    )
    snap_a = rt_a.start(pr_a, "job-floor-a", repo_root=str(head_a), base_root=str(base_a))
    assert not snap_a["done"]
    assert snap_a["values"]["importance_level"] == "critical"
    assert snap_a["values"]["risk_tier"]["min_human_approvals"] == 2
    assert snap_a["values"]["risk_tier"]["require_non_author"] is True
    assert not github_a.merged

    # 子用例 B：普通代码原本是 medium/T1，pr_reader 允许向上抬升到 critical/T2（2 人审批）
    base_b, head_b = make_fixture_repo(tmp_path / "b")
    cfg_b = load_settings(
        data_dir=tmp_path / "data_b",
        checkpoint_path=tmp_path / "ck_b.db",
        sandbox_backend="fake",
        merge_mode="auto",
    )
    provider_b = FakeProvider(v2_provider_map(pr_brief=PR_BRIEF_CRITICAL_JSON))
    github_b = FakeGitHub()
    nodes_b = ReviewNodes(cfg_b, llm_provider=provider_b, github=github_b, sandbox=FakeSandbox())
    rt_b = CodeGuardRuntime(cfg_b, nodes=nodes_b, checkpointer=make_checkpointer(tmp_path / "ck_b.db"))
    pr_b = PullRequestContext(
        repo="acme/repo",
        number=103,
        base_ref="main",
        base_sha="a" * 40,
        head_ref="feat/calc",
        head_sha="b" * 40,
        title="feat: critical payment rounding",
        body="涉及账务金额计算",
        author="bob",
        changed_files=[FileChange(path="calc.py")],
    )
    snap_b = rt_b.start(pr_b, "job-floor-b", repo_root=str(head_b), base_root=str(base_b))
    assert not snap_b["done"]
    assert snap_b["values"]["importance_level"] == "critical"
    assert snap_b["values"]["risk_tier"]["min_human_approvals"] == 2
    # 1 名非作者仍不足 2 人，必须继续等待第 2 名审批
    snap_b2 = rt_b.resume("job-floor-b", {"approved": True, "approver": "carol"})
    assert not snap_b2["done"]
    snap_b3 = rt_b.resume("job-floor-b", {"approved": True, "approver": "dave"})
    assert snap_b3["done"]
    assert snap_b3["values"]["outcome"] == "MERGED"


def test_qa_revise_and_verify_syntax_failure_loop_before_submit(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    (base / "calc.py").write_text("def divide_safe(a: float, b: float) -> float:\n    return a / b\n", encoding="utf-8")
    (head / "calc.py").write_text("def divide_safe(a: float, b: float) -> float:\n    return a / b\n", encoding="utf-8")
    init_git(head)

    broken_syntax = "def divide_safe(a: float, b: float) -> float:\n    if b == 0:\n"
    fixed_valid = (
        "def divide_safe(a: float, b: float) -> float:\n"
        "    if b == 0:\n"
        "        return 0.0\n"
        "    return a / b\n"
    )
    issue_r1 = _blocking_finding(
        path="calc.py",
        line=2,
        message="未检查除数 b == 0",
        evidence="return a / b",
    )
    qa_revise = json.dumps({
        "verdict": "revise",
        "is_aligned": False,
        "has_scope_drift": True,
        "alignment_score": 0.25,
        "drift_details": ["首轮修改遗漏除零分支"],
        "redirect_instructions": ["请补充 if b == 0: return 0.0"],
        "summary": "修复偏离 FixDirective，打回重修",
    }, ensure_ascii=False)

    coder_llm = FakeProvider({
        "coder": [
            # 第 1 次尝试：改个无关注释，触发 QA revise
            json.dumps({
                "tool": "write_file",
                "args": {"path": "calc.py", "content": "def divide_safe(a: float, b: float) -> float:\n    return a / b  # todo\n"},
            }, ensure_ascii=False),
            json.dumps({"patch": "", "modified_files": ["calc.py"], "explanation": "尝试1"}),
            # 第 2 次尝试：写入 Python 语法错误，QA 放行但 Verify Gate 应拦下并打回
            json.dumps({
                "tool": "write_file",
                "args": {"path": "calc.py", "content": broken_syntax},
            }, ensure_ascii=False),
            json.dumps({"patch": "", "modified_files": ["calc.py"], "explanation": "尝试2 误写语法错"}),
            # 第 3 次尝试：写入合法修复
            json.dumps({
                "tool": "write_file",
                "args": {"path": "calc.py", "content": fixed_valid},
            }, ensure_ascii=False),
            json.dumps({"patch": "", "modified_files": ["calc.py"], "explanation": "尝试3 正确修复"}),
        ],
    })("coder")

    provider = _DelegatingProvider(
        v2_provider_map(
            pr_brief=PR_BRIEF_MEDIUM_JSON,
            rounds=2,
            overrides={
                "review_logic": [issue_r1, REVIEW_PASS_JSON],
                "qa_checker": [qa_revise, QA_PASS_JSON, QA_PASS_JSON],
            },
        ),
        {"coder": coder_llm},
    )

    cfg = load_settings(
        data_dir=tmp_path / "data",
        checkpoint_path=tmp_path / "ckpt.db",
        sandbox_backend="fake",
        merge_mode="auto",
        max_fix_rounds=5,
        max_review_rounds=5,
    )
    github = FakeGitHub()
    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox())
    rt = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ckpt.db"))
    pr = PullRequestContext(
        repo="acme/repo",
        number=104,
        base_ref="main",
        base_sha="a" * 40,
        head_ref="fix/divide",
        head_sha="b" * 40,
        title="fix: calc",
        body="更新 calc",
        author="dev",
        changed_files=[FileChange(path="calc.py")],
    )
    snap = rt.start(pr, "job-qa-verify", repo_root=str(head), base_root=str(base))
    if not snap["done"]:
        snap = rt.resume("job-qa-verify", {"approved": True, "approver": "reviewer-b"})
    values = snap["values"]
    assert snap["done"], values.get("error")
    assert values["fix_round"] == 3
    assert values["review_round"] == 2
    assert len(github.pushed) == 1
    assert "return 0.0" in github.pushed[0]["files"]["calc.py"]
    assert values["outcome"] == "MERGED"


def test_outer_multi_round_review_loop_preserves_multi_file_fixes_and_short_circuits(tmp_path: Path):
    base = tmp_path / "base"
    head = tmp_path / "head"
    for root in (base, head):
        (root / "tests").mkdir(parents=True, exist_ok=True)
        (root / "a.py").write_text("def fa(x):\n    return x + 1\n", encoding="utf-8")
        (root / "b.py").write_text("def fb(y):\n    return y * 2\n", encoding="utf-8")
        (root / "tests" / "test_ab.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (head / "a.py").write_text("def fa(x):\n    return x + 2\n", encoding="utf-8")
    init_git(base)
    init_git(head)

    issue_a = _blocking_finding(path="a.py", line=2, message="a.py 未判 None", evidence="return x + 2")
    issue_b = _blocking_finding(path="b.py", line=2, message="b.py 未判 None", evidence="return y * 2")

    round_memory_prompts: list[str] = []

    def logic_handler(idx: int, prompt: str) -> str:
        round_memory_prompts.append(prompt)
        if idx == 0:
            return issue_a
        if idx == 1:
            return issue_b
        return REVIEW_PASS_JSON

    coder_llm = FakeProvider({
        "coder": [
            json.dumps({
                "tool": "write_file",
                "args": {"path": "a.py", "content": "def fa(x):\n    return 0 if x is None else x + 2\n"},
            }, ensure_ascii=False),
            json.dumps({"patch": "", "modified_files": ["a.py"], "explanation": "fix a"}),
            json.dumps({
                "tool": "write_file",
                "args": {"path": "b.py", "content": "def fb(y):\n    return 0 if y is None else y * 2\n"},
            }, ensure_ascii=False),
            json.dumps({"patch": "", "modified_files": ["b.py"], "explanation": "fix b"}),
        ],
    })("coder")

    sandbox_calls = Counter()

    def handler(spec):
        sandbox_calls[spec.workspace.name] += 1
        return SandboxResult(status=SandboxStatus.passed, exit_code=0, stdout="passed")

    provider = _DelegatingProvider(
        v2_provider_map(
            pr_brief=PR_BRIEF_MEDIUM_JSON,
            rounds=3,
            overrides={"qa_checker": [QA_PASS_JSON, QA_PASS_JSON]},
        ),
        {
            "review_logic": _ScriptedLLM(logic_handler),
            "coder": coder_llm,
        },
    )
    cfg = load_settings(
        data_dir=tmp_path / "data",
        checkpoint_path=tmp_path / "ckpt.db",
        sandbox_backend="fake",
        merge_mode="auto",
        max_fix_rounds=5,
        max_review_rounds=5,
    )
    github = FakeGitHub()
    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox(handler=handler))
    rt = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ckpt.db"))
    pr = PullRequestContext(
        repo="acme/repo",
        number=105,
        base_ref="main",
        base_sha="a" * 40,
        head_ref="feat/ab",
        head_sha="b" * 40,
        title="feat: ab",
        body="更新 a 与 b",
        author="dev",
        changed_files=[FileChange(path="a.py"), FileChange(path="b.py")],
    )
    snap = rt.start(pr, "job-outer-loop", repo_root=str(head), base_root=str(base))
    if not snap["done"]:
        snap = rt.resume("job-outer-loop", {"approved": True, "approver": "reviewer-b"})
    values = snap["values"]
    assert snap["done"], values.get("error")
    assert values["review_round"] == 3
    assert values["fix_round"] == 2
    # verify_gate 仅对 fix_1 与 fix_2 执行各 1 次；第 3 轮复审通过后直接短路跳过重复 verify
    assert sandbox_calls["fix_1"] == 1 and sandbox_calls["fix_2"] == 1
    assert sum(sandbox_calls.values()) == 2
    # 第 2 轮提示词中必须已注入第 1 轮短期记忆，防止重复提问
    assert len(round_memory_prompts) == 3
    assert "a.py" in round_memory_prompts[1]
    assert set(values["fix_files"]) == {"a.py", "b.py"}
    assert len(github.pushed) == 1
    assert set(github.pushed[0]["files"]) == {"a.py", "b.py"}
    assert values["outcome"] == "MERGED"


def test_five_round_cap_escalates_without_merge(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    five_lines = "\n".join(f"v{i} = {i}" for i in range(1, 7)) + "\n"
    (base / "calc.py").write_text(five_lines, encoding="utf-8")
    (head / "calc.py").write_text(five_lines.replace("v1 = 1", "v1 = 10"), encoding="utf-8")
    init_git(head)

    # 让 review_logic 在第 1~5 轮持续对第 idx+1 行提出新问题，验证精确卡在 5 轮上限收敛升级人工
    logic_llm = _ScriptedLLM(
        lambda idx, _: _blocking_finding(
            path="calc.py",
            line=min(6, idx + 1),
            message=f"第 {idx + 1} 轮发现边界缺陷 {idx + 1}",
            evidence=f"line {idx + 1}",
        )
    )
    coder_llm = _ScriptedLLM(
        lambda idx, _: json.dumps({
            "patch": "",
            "modified_files": ["calc.py"],
            "edits": [{
                "path": "calc.py",
                "search": f"v{idx + 2} = {idx + 2}",
                "replace": f"v{idx + 2} = {idx + 102}",
            }],
            "explanation": f"尝试修复第 {idx + 1} 轮问题",
        }, ensure_ascii=False)
    )

    cfg = load_settings(
        data_dir=tmp_path / "data",
        checkpoint_path=tmp_path / "ckpt.db",
        sandbox_backend="fake",
        merge_mode="auto",
        max_review_rounds=5,
        max_fix_rounds=5,
    )
    provider = _DelegatingProvider(
        v2_provider_map(
            pr_brief=PR_BRIEF_MEDIUM_JSON,
            rounds=6,
            overrides={"qa_checker": [QA_PASS_JSON] * 6},
        ),
        {
            "review_logic": logic_llm,
            "coder": coder_llm,
        },
    )
    github = FakeGitHub()
    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox())
    rt = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ckpt.db"))
    pr = PullRequestContext(
        repo="acme/repo",
        number=106,
        base_ref="main",
        base_sha="a" * 40,
        head_ref="feat/cap",
        head_sha="b" * 40,
        title="feat: cap test",
        body="验证 5 轮上限",
        author="dev",
        changed_files=[FileChange(path="calc.py")],
    )
    snap = rt.start(pr, "job-5-cap", repo_root=str(head), base_root=str(base))
    values = snap["values"]
    assert snap["done"], values.get("error")
    assert values["review_round"] == 5
    assert values["fix_round"] == 4
    assert values["escalation_reason"]
    assert values["outcome"] == "BLOCKED_HUMAN"
    assert not github.merged


def test_submit_mode_new_pr_and_fork_inherits_level_without_merging_original(tmp_path: Path):
    for label, is_fork, submit_mode, brief, expected_level in (
        ("new_pr", False, "new_pr", PR_BRIEF_MEDIUM_JSON, "medium"),
        ("fork", True, "push_commit", PR_BRIEF_CRITICAL_JSON, "critical"),
    ):
        sub = tmp_path / label
        base, head = make_fixture_repo(sub)
        (head / "calc.py").write_text("def divide_safe(a: float, b: float) -> float:\n    return a / b\n", encoding="utf-8")
        init_git(head)
        fixed = (
            "def divide_safe(a: float, b: float) -> float:\n"
            "    if b == 0:\n"
            "        return 0.0\n"
            "    return a / b\n"
        )
        issue = _blocking_finding(path="calc.py", line=2, message="需要补全除零保护", evidence="return a / b")
        cfg = load_settings(
            data_dir=sub / "data",
            checkpoint_path=sub / "ck.db",
            sandbox_backend="fake",
            merge_mode="auto",
            submit_mode=submit_mode,
        )
        coder_llm = FakeProvider({
            "coder": [
                json.dumps({"tool": "write_file", "args": {"path": "calc.py", "content": fixed}}, ensure_ascii=False),
                json.dumps({"patch": "", "modified_files": ["calc.py"], "explanation": "fixed"}),
            ],
        })("coder")
        provider = _DelegatingProvider(
            v2_provider_map(
                pr_brief=brief,
                rounds=2,
                overrides={"review_logic": [issue, REVIEW_PASS_JSON]},
            ),
            {"coder": coder_llm},
        )
        github = FakeGitHub()
        nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox())
        rt = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(sub / "ck.db"))
        pr = PullRequestContext(
            repo="acme/repo",
            number=107,
            base_ref="main",
            base_sha="a" * 40,
            head_ref="feat/fork",
            head_sha="b" * 40,
            title="fix: calc",
            body="fork test",
            author="external-dev",
            is_fork=is_fork,
            changed_files=[FileChange(path="calc.py")],
        )
        snap = rt.start(pr, f"job-{label}", repo_root=str(head), base_root=str(base))
        values = snap["values"]
        assert snap["done"], values.get("error")
        assert len(github.created_prs) == 1
        assert f"importance:{expected_level}" in github.created_prs[0]["labels"]
        assert values["submitted_pr_number"] == github.created_prs[0]["number"]
        assert values["review_summary"]["inherited_level"] == expected_level
        assert values["outcome"] == "BLOCKED_HUMAN"
        assert not github.merged


def test_procedural_memory_persisted_and_retrieved_by_next_pr(tmp_path: Path):
    mem_store = ProceduralMemoryStore(tmp_path / "shared_mem.db", embedder=LocalHashEmbedder())

    # PR #1：修复除零并合并，触发终态写入 ProceduralMemoryRecord
    base1, head1 = make_fixture_repo(tmp_path / "pr1")
    (head1 / "calc.py").write_text("def divide_safe(a: float, b: float) -> float:\n    return a / b\n", encoding="utf-8")
    init_git(head1)
    issue1 = _blocking_finding(
        path="calc.py",
        line=2,
        message="ZeroDivisionError divide 边界未检查除数为零",
        evidence="return a / b",
    )
    coder1 = FakeProvider({
        "coder": [
            json.dumps({
                "tool": "write_file",
                "args": {
                    "path": "calc.py",
                    "content": "def divide_safe(a: float, b: float) -> float:\n    return 0.0 if b == 0 else a / b\n",
                },
            }, ensure_ascii=False),
            json.dumps({
                "patch": "",
                "modified_files": ["calc.py"],
                "explanation": "ZeroDivisionError 修复：增加 b == 0 守卫",
            }, ensure_ascii=False),
        ],
    })("coder")
    cfg1 = load_settings(
        data_dir=tmp_path / "data1",
        checkpoint_path=tmp_path / "ck1.db",
        sandbox_backend="fake",
        merge_mode="auto",
    )
    gh1 = FakeGitHub()
    nodes1 = ReviewNodes(
        cfg1,
        llm_provider=_DelegatingProvider(
            v2_provider_map(rounds=2, overrides={"review_logic": [issue1, REVIEW_PASS_JSON]}),
            {"coder": coder1},
        ),
        github=gh1,
        sandbox=FakeSandbox(),
        memory_store=mem_store,
    )
    rt1 = CodeGuardRuntime(cfg1, nodes=nodes1, checkpointer=make_checkpointer(tmp_path / "ck1.db"))
    pr1 = PullRequestContext(
        repo="acme/repo",
        number=201,
        base_ref="main",
        base_sha="a" * 40,
        head_ref="fix/pr1",
        head_sha="b" * 40,
        title="fix: calc division",
        body="除零防御",
        author="dev",
        changed_files=[FileChange(path="calc.py")],
    )
    snap1 = rt1.start(pr1, "job-mem-1", repo_root=str(head1), base_root=str(base1))
    if not snap1["done"]:
        snap1 = rt1.resume("job-mem-1", {"approved": True, "approver": "reviewer-b"})
    assert snap1["done"]
    assert snap1["values"]["outcome"] == "MERGED"
    assert len(snap1["values"]["memory_records_written"]) == 1
    assert mem_store.count() == 1

    # PR #2：Coder 调用 search_procedural_memory 检索 PR #1 的程序化记忆并完成修复
    base2, head2 = make_fixture_repo(tmp_path / "pr2")
    (base2 / "math_ops.py").write_text("def ratio(x, y):\n    return x / y\n", encoding="utf-8")
    (head2 / "math_ops.py").write_text("def ratio(x, y):\n    return x / y\n", encoding="utf-8")
    init_git(head2)
    issue2 = _blocking_finding(
        path="math_ops.py",
        line=2,
        message="ratio 函数缺少除零检查导致 ZeroDivisionError",
        evidence="return x / y",
    )
    coder2 = FakeProvider({
        "coder": [
            json.dumps({
                "tool": "search_procedural_memory",
                "args": {"query": "ZeroDivisionError divide", "top_k": 3},
            }, ensure_ascii=False),
            json.dumps({
                "tool": "write_file",
                "args": {
                    "path": "math_ops.py",
                    "content": "def ratio(x, y):\n    return 0.0 if y == 0 else x / y\n",
                },
            }, ensure_ascii=False),
            json.dumps({
                "patch": "",
                "modified_files": ["math_ops.py"],
                "explanation": "参考历史程序化记忆增加 y == 0 守卫",
            }, ensure_ascii=False),
        ],
    })("coder")
    cfg2 = load_settings(
        data_dir=tmp_path / "data2",
        checkpoint_path=tmp_path / "ck2.db",
        sandbox_backend="fake",
        merge_mode="auto",
    )
    gh2 = FakeGitHub()
    nodes2 = ReviewNodes(
        cfg2,
        llm_provider=_DelegatingProvider(
            v2_provider_map(rounds=2, overrides={"review_logic": [issue2, REVIEW_PASS_JSON]}),
            {"coder": coder2},
        ),
        github=gh2,
        sandbox=FakeSandbox(),
        memory_store=mem_store,
    )
    rt2 = CodeGuardRuntime(cfg2, nodes=nodes2, checkpointer=make_checkpointer(tmp_path / "ck2.db"))
    pr2 = PullRequestContext(
        repo="acme/repo",
        number=202,
        base_ref="main",
        base_sha="a" * 40,
        head_ref="fix/pr2",
        head_sha="c" * 40,
        title="fix: ratio zero check",
        body="避免除零",
        author="dev",
        changed_files=[FileChange(path="math_ops.py")],
    )
    snap2 = rt2.start(pr2, "job-mem-2", repo_root=str(head2), base_root=str(base2))
    if not snap2["done"]:
        snap2 = rt2.resume("job-mem-2", {"approved": True, "approver": "reviewer-b"})
    v2 = snap2["values"]
    assert snap2["done"], v2.get("error")
    assert v2["outcome"] == "MERGED"
    rag_events = [ev for ev in v2["tool_events"] if ev.get("tool") == "search_procedural_memory"]
    assert rag_events and rag_events[0]["status"] == "ok"
    assert "ZeroDivisionError" in coder2.prompts[1]
    assert mem_store.count() == 2
    mem_store.close()


def test_deterministic_finding_fixed_by_coder_resolves_on_rereview(tmp_path: Path):
    base, head = make_fixture_repo(tmp_path)
    (head / "calc.py").write_text(
        "def divide_safe(a: float) -> float:\n"
        "    return a\n",
        encoding="utf-8",
    )
    init_git(head)

    fixed_safe = (
        "def divide_safe(a: float, b: float) -> float:\n"
        "    return (a / b) if b else 0.0\n"
    )
    coder_llm = FakeProvider({
        "coder": [
            json.dumps({"tool": "write_file", "args": {"path": "calc.py", "content": fixed_safe}}, ensure_ascii=False),
            json.dumps({"patch": "", "modified_files": ["calc.py"], "explanation": "恢复签名并保持除零安全"}),
        ],
    })("coder")

    cfg = load_settings(
        data_dir=tmp_path / "data",
        checkpoint_path=tmp_path / "ckpt.db",
        sandbox_backend="fake",
        merge_mode="auto",
        max_fix_rounds=5,
        max_review_rounds=5,
    )
    provider = _DelegatingProvider(v2_provider_map(rounds=2), {"coder": coder_llm})
    github = FakeGitHub()
    nodes = ReviewNodes(cfg, llm_provider=provider, github=github, sandbox=FakeSandbox())
    rt = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(tmp_path / "ckpt.db"))
    pr = PullRequestContext(
        repo="acme/repo",
        number=108,
        base_ref="main",
        base_sha="a" * 40,
        head_ref="feat/api-restore",
        head_sha="b" * 40,
        title="feat: restore signature",
        body="清理公共函数签名",
        author="dev",
        changed_files=[FileChange(path="calc.py")],
    )
    snap = rt.start(pr, "job-det-resolve", repo_root=str(head), base_root=str(base))
    if not snap["done"]:
        snap = rt.resume("job-det-resolve", {"approved": True, "approver": "reviewer-b"})
    values = snap["values"]
    assert snap["done"], values.get("error")
    assert values["fix_round"] == 1
    assert values["review_round"] == 2
    assert any(
        f.get("rule_id") == "API-001" and f.get("lifecycle") == "resolved"
        for f in (values.get("finding_history") or []) + (values.get("findings") or [])
    )
    assert values["outcome"] == "MERGED"
    assert github.merged
