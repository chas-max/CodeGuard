"""CodeGuardState 字段、reducer 接线与 prompts 纯函数冒烟测试。"""
from __future__ import annotations

from codeguard.prompts.heal import f2p_test_prompt, react_think_prompt
from codeguard.prompts.plan import plan_prompt
from codeguard.prompts.resolve import resolve_prompt
from codeguard.prompts.supervise import supervise_prompt, supervise_verify_prompt
from codeguard.prompts.understand import (
    parse_issue_prompt,
    revision_classify_prompt,
    summarize_prompt,
)
from codeguard.prompts.write import outline_prompt, patch_code_prompt, reflect_prompt
from codeguard.state import CodeGuardState, initial_state


def test_initial_state_fields():
    st = initial_state("issue text", "job-1", repo_dir="D:/repo")
    assert st["job_id"] == "job-1"
    assert st["user_input"] == "issue text"
    assert st["repo_dir"] == "D:/repo"
    assert st["status"] == "parsing"
    assert st["open_issues"] == []
    assert st["agent_issues"] == []
    assert st["patch_units"] == []
    assert st["symbol_bible"] == {}
    assert st["clarification_round"] == 0
    assert st["director_resolve_count"] == 0
    # 六个防死循环计数器全部就位
    for key in (
        "clarification_round",
        "machine_revision_count",
        "human_revision_count",
        "anchor_round",
        "production_attempt_count",
        "director_resolve_count",
    ):
        assert key in st


def test_state_has_open_issues_field():
    """open_issues 是普通字段（无 reducer），供 director_resolve 覆盖式清空。"""
    st = initial_state("x", "j")
    st["open_issues"] = [{"agent": "sandbox"}]
    st["open_issues"] = []  # 覆盖式清空（LangGraph 无 reducer 字段语义）
    assert st["open_issues"] == []
    # agent_issues 是 append-only 审计日志，语义不同
    assert "agent_issues" in CodeGuardState.__annotations__


def test_all_prompts_return_str_with_json_contract():
    ctx = {"title": "t", "symptom": "s", "completeness": "clear"}
    prompts = [
        parse_issue_prompt("bug report", ["a.py", "b.py"]),
        revision_classify_prompt("user msg"),
        summarize_prompt("history"),
        plan_prompt(ctx, ["a.py"], "code", "contracts", ["test_a.py"]),
        outline_prompt(ctx, [{"target_file": "a.py"}]),
        patch_code_prompt("def f():\n", "hyp", "sketch", "contracts", ctx),
        reflect_prompt({"a.py": "code"}, "contracts", ctx),
        supervise_prompt("diff text", ctx),
        supervise_verify_prompt("diff text", ctx, {"decision": "approve"}),
        resolve_prompt([{"agent": "x"}], "executing", 1, 3),
        f2p_test_prompt(ctx, "code", ""),
        react_think_prompt({"unit_id": "U001"}, "traceback", "code"),
    ]
    for p in prompts:
        assert isinstance(p, str) and len(p) > 50
        assert "JSON" in p or "json" in p  # 均含 JSON 输出契约
