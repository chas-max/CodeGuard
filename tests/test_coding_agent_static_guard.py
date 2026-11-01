"""静态门控 static_guard 与补丁工具 patch_writer 测试（PATH_A Task 3 扩展）。"""
from __future__ import annotations

from pathlib import Path

from codeguard.coding_agent.patch_writer import (
    read_relevant_code,
    synthesize_unified_diff,
)
from codeguard.coding_agent.static_guard import audit_patched_code


BIBLE = {
    "SYM_001": {
        "symbol_id": "SYM_001",
        "file_path": "pkg/calc.py",
        "symbol_name": "add_tax",
        "arg_names": ["amount", "tax_rate"],
        "frozen_signature": True,
    }
}


def test_static_guard_catches_syntax_and_signature_violation():
    # 1. 语法错误拦截
    bad_syntax = "def add_tax(amount, tax_rate):\n    return amount +\n"
    v1 = audit_patched_code("pkg/calc.py", bad_syntax, BIBLE)
    assert v1.passed is False
    assert v1.category == "syntax_error"
    assert "SyntaxError" in v1.issues[0]

    # 2. 破坏冻结公共函数签名拦截（擅自删减参数）
    broken_sig = "def add_tax(amount):\n    return amount * 1.1\n"
    v2 = audit_patched_code("pkg/calc.py", broken_sig, BIBLE)
    assert v2.passed is False
    assert v2.category == "signature_broken"

    # 3. 丢失冻结函数定义同样拦截
    missing_def = "def other():\n    pass\n"
    v2b = audit_patched_code("pkg/calc.py", missing_def, BIBLE)
    assert v2b.passed is False
    assert v2b.category == "signature_broken"
    assert "丢失" in v2b.issues[0]

    # 4. 合规代码放行
    good_code = "def add_tax(amount, tax_rate):\n    return round(amount * (1 + tax_rate), 2)\n"
    v3 = audit_patched_code("pkg/calc.py", good_code, BIBLE)
    assert v3.passed is True
    assert v3.category == "none"


def test_static_guard_blocks_forbidden_calls():
    for snippet in (
        "def add_tax(a, b):\n    return eval('1+1')\n",
        "def add_tax(a, b):\n    exec('x=1')\n    return a\n",
        "def add_tax(a, b):\n    import os\n    return os.system('rm -rf /')\n",
        "def add_tax(a, b):\n    import shutil\n    shutil.rmtree('C:/')\n    return a\n",
    ):
        v = audit_patched_code("pkg/calc.py", snippet, BIBLE)
        assert v.passed is False, snippet
        assert v.category == "security_violation"


def test_static_guard_non_frozen_signature_free():
    bible = {
        "SYM_009": {
            "symbol_id": "SYM_009",
            "file_path": "pkg/calc.py",
            "symbol_name": "_helper",
            "arg_names": ["x"],
            "frozen_signature": False,
        }
    }
    changed = "def _helper(y):\n    return y\n"
    v = audit_patched_code("pkg/calc.py", changed, bible)
    assert v.passed is True  # 非冻结签名允许自由修改


def test_synthesize_unified_diff(tmp_path: Path):
    original = "def add_tax(amount, tax_rate):\n    return amount + tax_rate\n"
    f = tmp_path / "pkg"
    f.mkdir(parents=True)
    (f / "calc.py").write_text(original, encoding="utf-8")

    patched = "def add_tax(amount, tax_rate):\n    return round(amount * (1 + tax_rate), 2)\n"
    diff = synthesize_unified_diff(tmp_path, {"pkg/calc.py": patched})

    assert "--- a/pkg/calc.py" in diff
    assert "+++ b/pkg/calc.py" in diff
    assert "-    return amount + tax_rate" in diff
    assert "+    return round(amount * (1 + tax_rate), 2)" in diff


def test_read_relevant_code_truncates(tmp_path: Path):
    big = tmp_path / "big.py"
    big.write_text("# " + "x" * 5000 + "\n", encoding="utf-8")
    small = tmp_path / "small.py"
    small.write_text("VALUE = 42\n", encoding="utf-8")

    ctx = read_relevant_code(tmp_path, ["big.py", "small.py", "missing.py"], max_chars_per_file=100)
    assert "big.py" in ctx and "small.py" in ctx
    assert len(ctx) < 1500  # 长文件被截断
    assert "VALUE = 42" in ctx
    assert "missing.py" in ctx  # 缺失文件显式标注而非静默丢弃
