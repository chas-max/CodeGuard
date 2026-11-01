"""隔离沙箱执行引擎测试：F2P/P2P 双门控、Red 态验证、覆盖率、宿主隔离、超时防护。"""
from __future__ import annotations

from pathlib import Path

from codeguard.coding_agent.sandbox_runner import (
    run_sandbox_verification,
    snapshot_tree_hash,
    verify_f2p_red,
)


def _make_buggy_repo(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text(
        "def divide_safe(a: float, b: float) -> float:\n"
        "    return a / b\n",  # Bug: b=0 时抛 ZeroDivisionError
        encoding="utf-8",
    )
    (tmp_path / "test_calc.py").write_text(
        "from calc import divide_safe\n\n"
        "def test_normal_divide():\n"
        "    assert divide_safe(10, 2) == 5.0\n\n"
        "def test_zero_divide_returns_zero():\n"
        "    assert divide_safe(10, 0) == 0.0\n",
        encoding="utf-8",
    )


FIXED_CODE = (
    "def divide_safe(a: float, b: float) -> float:\n"
    "    if b == 0:\n"
    "        return 0.0\n"
    "    return a / b\n"
)


def test_sandbox_runner_f2p_and_p2p_dual_gate(tmp_path: Path):
    _make_buggy_repo(tmp_path)
    f2p = ["test_calc.py::test_zero_divide_returns_zero"]
    p2p = ["test_calc.py::test_normal_divide"]

    # 打补丁前：F2P 必失败（Red），P2P 正常通过
    pre_res = run_sandbox_verification(tmp_path, {}, f2p, p2p)
    assert pre_res.f2p_passed is False
    assert pre_res.p2p_passed is True
    assert pre_res.resolved is False
    # 失败时输出包含精简报错信息
    assert "assert" in pre_res.error_summary or "Error" in pre_res.error_summary

    # 应用修复补丁后：双门控全过；宿主原文件未被污染（100% 沙箱隔离）
    post_res = run_sandbox_verification(tmp_path, {"calc.py": FIXED_CODE}, f2p, p2p)
    assert post_res.f2p_passed is True
    assert post_res.p2p_passed is True
    assert post_res.resolved is True
    assert "if b == 0" not in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_verify_f2p_red_state(tmp_path: Path):
    _make_buggy_repo(tmp_path)
    f2p = ["test_calc.py::test_zero_divide_returns_zero"]

    # 未修复：Red（复现测试稳定失败）→ True
    assert verify_f2p_red(tmp_path, f2p) is True
    # 已修复：不再是 Red → False
    assert verify_f2p_red(tmp_path, f2p, patches={"calc.py": FIXED_CODE}) is False
    # 非法测试文件：视为无效锚点（不可靠 Red）→ False
    assert verify_f2p_red(tmp_path, ["test_noexist.py::test_x"]) is False


def test_sandbox_host_isolation_by_hash(tmp_path: Path):
    _make_buggy_repo(tmp_path)
    before = snapshot_tree_hash(tmp_path)
    run_sandbox_verification(
        tmp_path,
        {"calc.py": FIXED_CODE, "new_mod.py": "NEW = 1\n"},
        ["test_calc.py::test_zero_divide_returns_zero"],
        ["test_calc.py::test_normal_divide"],
    )
    after = snapshot_tree_hash(tmp_path)
    assert before == after  # 沙箱内的一切写入不得泄漏回宿主仓库


def test_sandbox_timeout_kills_infinite_loop(tmp_path: Path):
    (tmp_path / "loop.py").write_text(
        "def spin():\n    x = 1\n    while True:\n        x = x + 1\n",
        encoding="utf-8",
    )
    (tmp_path / "test_loop.py").write_text(
        "from loop import spin\n\n"
        "def test_spin_terminates():\n"
        "    assert spin() is not None\n",
        encoding="utf-8",
    )
    res = run_sandbox_verification(
        tmp_path, {}, ["test_loop.py::test_spin_terminates"], [], timeout_sec=5
    )
    assert res.f2p_passed is False
    assert "timeout" in res.error_summary.lower() or "疑似死循环" in res.error_summary


def test_sandbox_branch_coverage(tmp_path: Path):
    _make_buggy_repo(tmp_path)
    res = run_sandbox_verification(
        tmp_path,
        {"calc.py": FIXED_CODE},
        ["test_calc.py::test_zero_divide_returns_zero"],
        ["test_calc.py::test_normal_divide"],
        cov_target=".",
    )
    assert res.resolved is True
    assert 0.0 < res.branch_coverage_pct <= 100.0
