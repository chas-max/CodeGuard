"""策略 schema 与风险分级单测（S0）。"""
from __future__ import annotations

import pytest

from codeguard.engine.policy import (
    Policy,
    load_policy_text,
    matches_any,
    path_matches,
)
from codeguard.errors import PolicyError


def test_default_policy_is_internal_change_profile():
    policy = Policy()
    assert policy.mode == "internal"
    assert policy.profiles.default == "change"
    assert policy.require_reproduction is False
    assert policy.min_approvals("T1") == 1
    assert policy.min_approvals("T2") == 2
    # allow_zero_human 默认关闭：即便是 T0 也至少 1 人
    assert policy.min_approvals("T0") == 1


def test_allow_zero_human_enables_t0():
    policy = Policy.model_validate({"merge": {"allow_zero_human": True}})
    assert policy.min_approvals("T0") == 0
    assert policy.min_approvals("T1") == 1


def test_external_mode_requires_reproduction():
    policy = load_policy_text("mode: external\n")
    assert policy.require_reproduction is True
    policy_explicit = load_policy_text("mode: external\ngates: {require_reproduction: false}\n")
    assert policy_explicit.require_reproduction is False


def test_unknown_field_is_rejected():
    with pytest.raises(PolicyError):
        load_policy_text("mode: internal\nunknown_field: 1\n")


def test_bad_tier_name_is_rejected():
    with pytest.raises(PolicyError):
        load_policy_text("tiers: {default: T9}\n")
    with pytest.raises(PolicyError):
        load_policy_text("tiers: {min_human_approvals: {T5: 1}}\n")


def test_approval_counts_must_be_monotonic():
    with pytest.raises(PolicyError):
        load_policy_text("tiers: {min_human_approvals: {T0: 2, T1: 1, T2: 2}}\n")


def test_glob_matching_variants():
    assert path_matches("docs/guide.md", "docs/**")
    assert path_matches("README.md", "**/*.md")
    assert path_matches("src/pkg/README.md", "**/*.md")
    assert path_matches("src/auth/login.py", "**/auth/**")
    assert path_matches("pyproject.toml", "pyproject.toml")
    assert not path_matches("src/calc.py", "**/*.md")
    assert matches_any("src/auth/login.py", ["**/auth/**", "docs/**"])


def test_internal_defaults_have_tiers_and_review_settings():
    policy = Policy()
    assert policy.tiers.default == "T1"
    assert policy.review.max_nits == 5
    assert policy.review.max_findings_verified == 5
    assert policy.depth_for("T2") == "deep"
    assert policy.is_force_human(".codeguard/policy.yml")
    assert policy.is_force_human("src/auth/session.py")
    assert not policy.is_force_human("src/calc.py")


def test_agent_tools_defaults_are_enabled():
    policy = Policy()
    tools = policy.agents.tools
    assert tools.enabled is True
    assert tools.external_max_capability == "read"
    assert tools.roles.semantic_detector == "read"
    assert tools.roles.reviewer == "exec"
    assert tools.roles.fixer == "write"
    assert policy.mode == "internal"


def test_agent_tools_section_is_strict_and_bounded():
    with pytest.raises(PolicyError):
        load_policy_text("agents: {tools: {enabled: true, unknown: 1}}\n")
    with pytest.raises(PolicyError):
        load_policy_text("agents: {tools: {roles: {fixer: admin}}}\n")
    with pytest.raises(PolicyError):
        load_policy_text("agents: {tools: {max_steps: 0}}\n")
    with pytest.raises(PolicyError):
        load_policy_text("agents: {tools: {allow_commands: ['']}}\n")
