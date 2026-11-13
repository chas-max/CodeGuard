"""仓库级审查指南解析与注入（S3）。"""
from __future__ import annotations

from pathlib import Path

from codeguard.engine.guidance import (
    load_guidance,
    matches_any_path,
    parse_guidance,
    select_guidance,
)

DOC = """# 本仓库审查约定

通用：只说能给出文件:行的问题。

## path: src/billing/**
金额算错就是失败；改动必须带对账测试。

## path: docs/**
文档里的失败是“它撒谎了”，不是排版问题。
"""


def test_parse_guidance_sections():
    guidance = parse_guidance(DOC)
    assert "通用" in guidance.preamble
    assert [section["glob"] for section in guidance.sections] == ["src/billing/**", "docs/**"]


def test_select_guidance_matches_changed_paths_only():
    guidance = parse_guidance(DOC)
    text = select_guidance(guidance, ["src/billing/invoice.py"])
    assert "金额算错" in text
    assert "它撒谎了" not in text
    assert "通用" in text  # 前言总是注入

    both = select_guidance(guidance, ["src/billing/a.py", "docs/b.md"])
    assert "金额算错" in both and "它撒谎了" in both


def test_select_guidance_truncates():
    guidance = parse_guidance("x" * 20000)
    assert len(select_guidance(guidance, ["src/a.py"])) <= 8000


def test_load_guidance_reads_base_tree_only(tmp_path: Path):
    base = tmp_path / "base"
    head = tmp_path / "head"
    (base / ".codeguard").mkdir(parents=True)
    (head / ".codeguard").mkdir(parents=True)
    (base / ".codeguard" / "review.md").write_text(DOC, encoding="utf-8")
    (head / ".codeguard" / "review.md").write_text("PR 改写的指南，不应生效", encoding="utf-8")

    guidance = load_guidance(base, ".codeguard/review.md")
    assert "金额算错" in select_guidance(guidance, ["src/billing/a.py"])
    assert load_guidance(tmp_path / "missing").sections == []


def test_matches_any_path():
    assert matches_any_path(["src/billing/a.py"], "src/billing/**")
    assert not matches_any_path(["src/calc.py"], "src/billing/**")
