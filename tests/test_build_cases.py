"""用例生成器契约：覆盖、稳定性、可加载、正负例成对。"""
from __future__ import annotations

from eval.build_cases import RULE_IDS, build, summary
from eval.loader import LAYERS, load_suite, write_suite


def test_generator_covers_all_rules_and_layers():
    cases = build()
    stats = summary(cases)
    assert stats["total"] >= 150
    covered = set(stats["rule_coverage"])
    assert set(RULE_IDS) <= covered, f"缺少规则: {sorted(set(RULE_IDS) - covered)}"
    assert set(stats["by_layer"]) == set(LAYERS) - {"external"}
    assert stats["by_layer"]["det"] >= 100
    assert stats["by_layer"]["semantic"] >= 20
    assert stats["by_layer"]["gate"] >= 25
    assert stats["by_layer"]["security"] >= 20
    assert stats["by_layer"]["e2e"] >= 8


def test_ids_are_unique_and_paths_are_safe():
    cases = build()
    ids = [case.id for case in cases]
    assert len(ids) == len(set(ids))
    for case in cases:
        for path in list(case.base) + list(case.head):
            assert not path.startswith("/")
            assert ".." not in path.split("/")
        assert case.id == case.id.lower()


def test_generator_is_deterministic():
    first = [case.to_dict() for case in build()]
    second = [case.to_dict() for case in build()]
    assert first == second


def test_positive_and_negative_pairs():
    cases = build()
    positive = [case for case in cases if case.layer == "det" and case.expect_block]
    negative = [case for case in cases if case.layer == "det" and not case.expect_block]
    assert positive and negative
    assert len(negative) >= 10
    for rule in RULE_IDS:
        assert any(rule in case.expect_rules for case in positive), f"{rule} 没有正例"


def test_suite_roundtrip(tmp_path):
    cases = build()
    target = write_suite(cases, tmp_path / "cases.core.jsonl")
    loaded = load_suite(target)
    assert len(loaded) == len(cases)
    assert {case.id for case in loaded} == {case.id for case in cases}
    assert loaded[0].base == next(c for c in cases if c.id == loaded[0].id).base
