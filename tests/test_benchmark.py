"""Micro-SWE-Bench 评测基准测试：确定性、套件结构、小子集真实跑测。"""
from __future__ import annotations

from pathlib import Path

from eval.micro_swe_bench import (
    BUILDERS,
    build_synthetic_suite,
    evaluate_agent_on_suite,
)


def _tree_bytes(repo: Path) -> dict[str, bytes]:
    return {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in sorted(repo.rglob("*")) if p.is_file()
    }


def test_suite_determinism(tmp_path: Path):
    """同 seed 两次构建：仓库文件与补丁对逐字节一致（确定性红线）。"""
    s1 = build_synthetic_suite(tmp_path / "a", size=11)
    s2 = build_synthetic_suite(tmp_path / "b", size=11)
    assert len(s1) == len(s2) == 11
    for c1, c2 in zip(s1, s2):
        assert _tree_bytes(c1.repo_dir) == _tree_bytes(c2.repo_dir)
        assert c1.first_attempt_patch == c2.first_attempt_patch
        assert c1.healed_attempt_patch == c2.healed_attempt_patch
        assert c1.issue_text == c2.issue_text


def test_suite_shape(tmp_path: Path):
    suite = build_synthetic_suite(tmp_path, size=50)
    assert len(suite) == 50
    categories = {c.category for c in suite}
    assert categories == {b.__name__ for b in BUILDERS} or len(categories) >= 10
    first_fail = [c for c in suite if c.expect_first_fail]
    assert len(first_fail) == 20  # 40% 首轮必败（自愈增益来源）
    for c in suite:
        assert (c.repo_dir / f"test_{c.gold_files[0].split('/')[0]}.py").is_file()
        assert c.f2p_tests and c.p2p_tests


def test_evaluate_healing_gain_on_first_fail_cases(tmp_path: Path):
    """首轮必败题：单轮 0% → 多轮自愈 100%，P2P 零回归，定位全命中。"""
    suite = build_synthetic_suite(tmp_path, size=11)
    subset = [c for c in suite if c.expect_first_fail][:3]

    one_shot = evaluate_agent_on_suite(subset, use_ast_localization=True, max_healing_rounds=0)
    healed = evaluate_agent_on_suite(subset, use_ast_localization=True, max_healing_rounds=2)

    assert one_shot["resolved_rate"] == 0.0  # 首轮补丁必败
    assert healed["resolved_rate"] == 1.0    # 自愈后全解决
    assert healed["resolved_rate"] > one_shot["resolved_rate"]
    assert healed["p2p_regression_rate"] == 0.0
    assert healed["localization_recall_at_3"] == 1.0


def test_evaluate_first_pass_cases(tmp_path: Path):
    """首轮即过的题：单轮 Resolved=100%、Pass@1=100%。"""
    suite = build_synthetic_suite(tmp_path, size=11)
    subset = [c for c in suite if not c.expect_first_fail][:2]
    m = evaluate_agent_on_suite(subset, use_ast_localization=True, max_healing_rounds=0)
    assert m["resolved_rate"] == 1.0
    assert m["one_shot_pass_at_1"] == 1.0
    assert m["p2p_regression_rate"] == 0.0
