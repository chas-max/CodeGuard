"""AST 符号契约提取与跨文件故障定位测试（PATH_A Task 2）。"""
from __future__ import annotations

from pathlib import Path

from codeguard.coding_agent.ast_analyzer import (
    extract_repo_symbols,
    find_repo_test_files,
    rank_suspect_files,
)


def test_extract_repo_symbols_and_rank_files(tmp_path: Path):
    src_dir = tmp_path / "pkg"
    src_dir.mkdir()
    (src_dir / "pricing.py").write_text(
        "def compute_discount(price: float, rate: float) -> float:\n"
        "    return price * (1.0 - rate)\n",
        encoding="utf-8",
    )
    (src_dir / "checkout.py").write_text(
        "from pkg.pricing import compute_discount\n\n"
        "def final_total(amount: float) -> float:\n"
        "    return compute_discount(amount, 0.2)\n",
        encoding="utf-8",
    )

    symbols = extract_repo_symbols(tmp_path)
    names = {s.symbol_name: s for s in symbols.values()}
    assert "compute_discount" in names
    assert names["compute_discount"].arg_names == ["price", "rate"]
    assert names["compute_discount"].return_annotation == "float"
    assert any("checkout.py:final_total" in c for c in names["compute_discount"].callers)
    # 私有函数不冻结签名
    assert names["compute_discount"].frozen_signature is True

    # 基于 Issue 报错文本 + 调用图传播的 Top-K 故障文件定位
    issue = "TypeError in final_total when calling compute_discount with negative rate"
    ranked = rank_suspect_files(tmp_path, issue, top_k=2)
    assert "pkg/pricing.py" in ranked
    assert "pkg/checkout.py" in ranked


def test_rank_files_propagates_to_caller_file(tmp_path: Path):
    """报错文本只提到被调函数时，调用方所在文件也应通过 1-hop 传播进入候选。"""
    (tmp_path / "math_ops.py").write_text(
        "def safe_divide(a: float, b: float) -> float:\n"
        "    return a / b\n",
        encoding="utf-8",
    )
    (tmp_path / "report.py").write_text(
        "from math_ops import safe_divide\n\n"
        "def build_report(used: int, limit: int) -> float:\n"
        "    return safe_divide(used, limit)\n",
        encoding="utf-8",
    )
    ranked = rank_suspect_files(tmp_path, "ZeroDivisionError inside safe_divide", top_k=3)
    assert "math_ops.py" in ranked[:2]
    assert "report.py" in ranked


def test_extract_skips_unparsable_and_ignored_dirs(tmp_path: Path):
    (tmp_path / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    junk = tmp_path / ".venv" / "lib"
    junk.mkdir(parents=True)
    (junk / "junk_mod.py").write_text("def junk():\n    pass\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("def visible():\n    pass\n", encoding="utf-8")

    symbols = extract_repo_symbols(tmp_path)
    assert "visible" in {s.symbol_name for s in symbols.values()}
    assert "junk" not in {s.symbol_name for s in symbols.values()}


def test_find_repo_test_files(tmp_path: Path):
    (tmp_path / "test_a.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    sub = tmp_path / "tests"
    sub.mkdir()
    (sub / "test_b.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    tests = find_repo_test_files(tmp_path)
    assert "test_a.py" in tests
    assert "tests/test_b.py" in tests
    assert "app.py" not in tests
