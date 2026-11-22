"""Agent 工具能力矩阵与收敛规则。"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeguard.config import Settings
from codeguard.engine.policy import Policy, load_policy_text
from codeguard.errors import PolicyError
from codeguard.tools.registry import build_toolbox, effective_capability
from tests.fakes import FakeSandbox


class Ctx:
    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or Settings()
        self.sandbox = FakeSandbox()


def _box(role: str, *, policy: Policy | None = None, tier: str = "T1",
         cfg: Settings | None = None, allowed_files: list[str] | None = None):
    return build_toolbox(
        role=role, root=Path("."), job_id="j", ctx=Ctx(cfg), policy=policy or Policy(),
        tier=tier, allowed_files=allowed_files,
    )


def _names(box) -> set[str]:
    return {spec.name for spec in box.specs()} if box else set()


def test_default_role_matrix():
    assert _names(_box("semantic_detector")) == {
        "read_file", "list_dir", "grep", "related_files",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "inspect_tests",
        "mcp__osv_security__query_osv_vulnerability",
        "mcp__osv_security__scan_manifest_dependencies",
        "mcp__git_history__git_blame",
        "mcp__git_history__git_file_log",
        "mcp__git_history__git_show_commit",
        "mcp__knowledge__inspect_package_api",
        "mcp__knowledge__search_incidents",
    }
    assert _names(_box("finding_verifier_a")) == {
        "read_file", "grep",
        "ast_symbols", "find_symbol_usages", "diff_base_head",
        "mcp__git_history__git_blame",
        "mcp__git_history__git_file_log",
        "mcp__git_history__git_show_commit",
        "mcp__knowledge__inspect_package_api",
        "mcp__knowledge__search_incidents",
    }
    assert _names(_box("issue_verifier")) == {
        "read_file", "list_dir", "grep",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "inspect_tests",
        "mcp__github_context__get_issue",
        "mcp__github_context__list_pr_comments",
        "mcp__github_context__get_commit_status",
    }
    assert _names(_box("reviewer")) == {
        "read_file", "list_dir", "grep",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "inspect_tests",
        "run_tests", "run_check",
        "mcp__osv_security__query_osv_vulnerability",
        "mcp__osv_security__scan_manifest_dependencies",
        "mcp__git_history__git_blame",
        "mcp__git_history__git_file_log",
        "mcp__git_history__git_show_commit",
        "mcp__knowledge__inspect_package_api",
        "mcp__knowledge__search_incidents",
        "mcp__github_context__get_issue",
        "mcp__github_context__list_pr_comments",
        "mcp__github_context__get_commit_status",
    }
    assert _names(_box("pr_reader")) == {
        "read_file", "diff_base_head", "get_pr_metadata",
        "mcp__git_history__git_blame",
        "mcp__git_history__git_file_log",
        "mcp__git_history__git_show_commit",
        "mcp__github_context__get_issue",
        "mcp__github_context__list_pr_comments",
        "mcp__github_context__get_commit_status",
    }
    assert _names(_box("review_logic")) == {
        "read_file", "grep", "ast_symbols", "find_symbol_usages",
        "diff_base_head", "related_files",
        "mcp__git_history__git_blame",
        "mcp__git_history__git_file_log",
        "mcp__git_history__git_show_commit",
    }
    assert _names(_box("qa_checker")) == {
        "read_file", "diff_base_head", "ast_symbols",
    }
    assert _names(_box("coder", allowed_files=["src/a.py"])) == {
        "read_file", "list_dir", "grep", "related_files",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "check_patch_guard",
        "search_procedural_memory",
        "run_tests", "run_check", "run_command", "edit_file", "apply_patch", "write_file",
        "mcp__git_history__git_blame",
        "mcp__git_history__git_file_log",
        "mcp__git_history__git_show_commit",
        "mcp__knowledge__inspect_package_api",
        "mcp__knowledge__search_incidents",
    }
    assert _names(_box("fixer", allowed_files=["src/a.py"])) == {
        "read_file", "list_dir", "grep", "related_files",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "check_patch_guard",
        "search_procedural_memory",
        "run_tests", "run_check", "run_command", "edit_file", "apply_patch", "write_file",
        "mcp__git_history__git_blame",
        "mcp__git_history__git_file_log",
        "mcp__git_history__git_show_commit",
        "mcp__knowledge__inspect_package_api",
        "mcp__knowledge__search_incidents",
    }


def test_write_tools_require_allowed_files():
    names = _names(_box("fixer"))
    assert "apply_patch" not in names and "write_file" not in names and "edit_file" not in names
    assert "run_tests" in names
    assert "search_procedural_memory" in _names(_box("coder"))


def test_default_enabled():
    policy = Policy()
    assert policy.agents.tools.enabled is True
    assert effective_capability(role="reviewer", policy=policy) == "exec"
    assert effective_capability(role="fixer", policy=policy) == "write"
    assert effective_capability(role="coder", policy=policy) == "write"
    for ro_role in ("pr_reader", "review_logic", "review_security",
                    "review_architecture", "review_tests", "qa_checker"):
        assert effective_capability(role=ro_role, policy=policy) == "read"


def test_non_coder_roles_stay_read_across_all_tiers():
    for tier in ("T0", "T1", "T2"):
        for ro_role in ("pr_reader", "review_logic", "review_security",
                        "review_architecture", "review_tests", "qa_checker"):
            assert effective_capability(role=ro_role, policy=Policy(), tier=tier) == "read"
        assert effective_capability(role="coder", policy=Policy(), tier=tier) == "write"


def test_external_mode_caps_at_external_max():
    policy = Policy.model_validate({"mode": "external"})
    assert effective_capability(role="reviewer", policy=policy) == "read"
    assert effective_capability(role="fixer", policy=policy) == "read"
    policy_low = Policy.model_validate({
        "mode": "external", "agents": {"tools": {"external_max_capability": "none"}},
    })
    assert effective_capability(role="reviewer", policy=policy_low) == "none"


def test_env_can_only_tighten():
    policy = Policy()
    cfg = Settings(agent_tools_max_capability="read")
    assert effective_capability(role="fixer", policy=policy, cfg=cfg) == "read"
    disabled = Settings(agent_tools_disabled=True)
    assert effective_capability(role="fixer", policy=policy, cfg=disabled) == "none"
    # 环境变量不能把策略按住的档位放宽
    policy_low = Policy.model_validate({"agents": {"tools": {"roles": {"reviewer": "read"}}}})
    cfg_high = Settings(agent_tools_max_capability="write")
    assert effective_capability(role="reviewer", policy=policy_low, cfg=cfg_high) == "read"


def test_policy_disable_returns_empty_toolbox():
    policy = Policy.model_validate({"agents": {"tools": {"enabled": False}}})
    assert _box("fixer", policy=policy, allowed_files=["src/a.py"]) is None


def test_role_downgrade_is_respected():
    policy = Policy.model_validate({
        "agents": {
            "tools": {"roles": {"reviewer": "read"}},
            "mcp": {"enabled": False},
        }
    })
    assert _names(_box("reviewer", policy=policy)) == {
        "read_file", "list_dir", "grep",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "inspect_tests",
    }


def test_osv_mcp_gated_by_manifest_changes():
    box_code_only = build_toolbox(
        role="reviewer",
        root=Path("."),
        job_id="j-code",
        ctx=Ctx(),
        policy=Policy(),
        tier="T1",
        pr_context={"changed_files": ["src/app.py", "tests/test_app.py"]},
    )
    assert not any(n.startswith("mcp__osv_security__") for n in _names(box_code_only))

    box_with_req = build_toolbox(
        role="reviewer",
        root=Path("."),
        job_id="j-deps",
        ctx=Ctx(),
        policy=Policy(),
        tier="T1",
        pr_context={"changed_files": ["src/app.py", "requirements.txt"]},
    )
    assert "mcp__osv_security__query_osv_vulnerability" in _names(box_with_req)


def test_mcp_disabled_keeps_builtin_tools():
    cfg_disabled = Settings(mcp_disabled=True)
    names = _names(_box("reviewer", cfg=cfg_disabled))
    assert "read_file" in names
    assert "ast_symbols" in names
    assert not any(n.startswith("mcp__") for n in names)

    cap_none = Settings(mcp_max_capability="none")
    names_none = _names(_box("reviewer", cfg=cap_none))
    assert not any(n.startswith("mcp__") for n in names_none)


def test_unknown_role_gets_no_tools():
    assert _box("unknown_agent") is None


def test_invalid_capability_literal_rejected():
    with pytest.raises(PolicyError):
        load_policy_text("agents: {tools: {roles: {reviewer: root}}}\n")
    with pytest.raises(PolicyError):
        load_policy_text("agents: {tools: {external_max_capability: all}}\n")


def test_empty_allow_command_rejected():
    with pytest.raises(PolicyError):
        load_policy_text("agents: {tools: {allow_commands: ['  ']}}\n")


def test_agents_section_is_strict():
    with pytest.raises(PolicyError):
        load_policy_text("agents: {tools: {unknown: 1}}\n")
