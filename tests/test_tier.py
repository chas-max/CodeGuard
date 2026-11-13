"""确定性风险分级表驱动测试（S0）。"""
from __future__ import annotations

import pytest

from codeguard.engine.policy import Policy
from codeguard.engine.tier import classify_tier, detect_agent_authorship, is_trusted_author


def _policy(**overrides) -> Policy:
    data = {"trusted_authors": ["alice", "bob"]}
    data.update(overrides)
    return Policy.model_validate(data)


@pytest.mark.parametrize(
    "paths,lines,author,expected",
    [
        # docs-only、可信作者、改动小 → T0
        (["docs/guide.md"], 40, "alice", "T0"),
        # docs-only 但作者不可信 → 至少 T1
        (["docs/guide.md"], 40, "mallory", "T1"),
        # 普通源码 → T1
        (["src/calc.py"], 30, "alice", "T1"),
        # 高风险路径 → T2
        (["src/auth/login.py"], 10, "alice", "T2"),
        # 大改动 → T2
        (["src/calc.py"], 1200, "alice", "T2"),
        # 受保护路径（CI 配置）→ 默认策略里 CI 属 T2
        ([".github/workflows/ci.yml"], 5, "alice", "T2"),
        # 依赖文件 → 默认策略里依赖属 T2
        (["requirements.txt"], 2, "alice", "T2"),
        # 策略文件本身 → force_human → T1
        ([".codeguard/policy.yml"], 2, "alice", "T1"),
    ],
)
def test_classify_tier(paths, lines, author, expected):
    result = classify_tier(
        _policy(), changed_paths=paths, changed_lines=lines, author=author,
    )
    assert result.tier.value == expected, (paths, lines, author, result.reasons)


def test_agent_authored_is_at_least_t1():
    result = classify_tier(
        _policy(), changed_paths=["docs/guide.md"], changed_lines=10, author="alice",
        pr_body="Generated with Claude Code",
    )
    assert result.tier.value == "T1"
    assert result.agent_authored is True


def test_t2_requires_non_author_approval():
    result = classify_tier(
        _policy(), changed_paths=["src/auth/login.py"], changed_lines=10, author="alice",
    )
    assert result.tier.value == "T2"
    assert result.require_non_author is True
    assert result.min_human_approvals == 2
    assert result.depth == "deep"


def test_untrusted_author_cannot_use_light_path():
    result = classify_tier(
        _policy(), changed_paths=["src/calc.py"], changed_lines=10, author="mallory",
    )
    assert result.tier.value == "T1"  # 不能降级
    assert result.trusted_author is False


def test_api_break_is_at_least_t1():
    result = classify_tier(
        _policy(), changed_paths=["docs/api.md"], changed_lines=10, author="alice",
        api_break=True,
    )
    assert result.tier.value in ("T1", "T2")


def test_trusted_and_agent_detection():
    assert is_trusted_author("alice", _policy())
    assert is_trusted_author("@alice", _policy())
    assert not is_trusted_author("", _policy())
    assert not is_trusted_author("team:org/platform", Policy.model_validate(
        {"trusted_authors": ["team:org/platform"]}))
    assert detect_agent_authorship("Co-authored-by: Claude <noreply@anthropic.com>")
    assert detect_agent_authorship("", labels=["agent"])
    assert not detect_agent_authorship("普通描述")
