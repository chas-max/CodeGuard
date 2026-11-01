"""Micro-SWE-Bench 评测基准：50 题确定性沙箱缺陷修复评测与消融实验。

设计（遵循 PATH_A 反幻觉红线）：
- 评测全程零 LLM 调用：每题的 first_attempt_patch（模拟首轮生成，40% 带典型缺陷）
  与 healed_attempt_patch（模拟自愈后补丁，恒正确）均为预烘焙产物。
- 题目由 11 类 Bug 模板轮转（seeded 命名/常量变分）生成，两次构建逐字节一致。
- 指标全部由真实沙箱 pytest 跑测产出：Localization Recall@3 / One-shot Pass@1 /
  Multi-turn Resolved Rate / P2P Regression Rate。

消融三组：
- baseline_single_round：无 AST 定位 + 单轮（不重试）
- ast_localized_single_round：+AST 定位 + 单轮
- ast_localized_multi_round_healing：+AST 定位 + 多轮沙箱自愈
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codeguard.coding_agent.ast_analyzer import extract_repo_symbols, rank_suspect_files
from codeguard.coding_agent.sandbox_runner import run_sandbox_verification
from codeguard.coding_agent.static_guard import audit_patched_code


@dataclass
class IssueBenchmarkCase:
    """单题：微型多文件仓库 + Issue 文本 + 金标 + 预烘焙补丁对。"""

    case_id: str
    category: str
    repo_dir: Path
    issue_text: str
    gold_files: list[str]
    f2p_tests: list[str]
    p2p_tests: list[str]
    first_attempt_patch: dict[str, str]
    healed_attempt_patch: dict[str, str]
    expect_first_fail: bool = False


# ══════════════════ Bug 模板（每个 builder 自行写仓库+测试文件） ══════════════════
# 约定：Bug 在 <pkg>/<mod_a>.py（gold）；<pkg>/<mod_b>.py 调用它（多文件考察定位）；
#       test_<pkg>.py 同时含 P2P（正常路径）与 F2P（复现缺陷）测试。


def _write_repo(repo: Path, files: dict[str, str], test_name: str, tests: str) -> None:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (repo / test_name).write_text(tests, encoding="utf-8")


def _t_division(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """除零：limit=0 崩溃，期望返回哨兵值 1.0。"""
    pkg = rng.choice(["billing", "quota", "metrics"])
    fn = rng.choice(["usage_ratio", "cost_per_seat", "avg_latency"])
    caller_fn = rng.choice(["weekly_report", "dashboard_summary", "export_row"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/math.py": (
                f"def {fn}(used: int, limit: int) -> float:\n"
                f"    # Bug: limit=0 时触发 ZeroDivisionError\n"
                f"    return round(used / limit, 4)\n"
            ),
            f"{pkg}/report.py": (
                f"from {pkg}.math import {fn}\n\n"
                f"def {caller_fn}(used: int, limit: int) -> float:\n"
                f"    return {fn}(used, limit)\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.math import {fn}\n\n"
            f"def test_{fn}_normal():\n    assert {fn}(5, 10) == 0.5\n\n"
            f"def test_{fn}_zero_limit():\n    assert {fn}(5, 0) == 1.0\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="division_by_zero", repo_dir=repo,
        issue_text=f"ZeroDivisionError in {fn} when limit is 0, called by {caller_fn}",
        gold_files=[f"{pkg}/math.py"],
        f2p_tests=[f"test_{pkg}.py::test_{fn}_zero_limit"],
        p2p_tests=[f"test_{pkg}.py::test_{fn}_normal"],
        first_attempt_patch={f"{pkg}/math.py": (
            f"def {fn}(used: int, limit: int) -> float:\n"
            f"    if limit <= 0:\n        return 0.0\n"  # 哨兵值错误（期望 1.0）
            f"    return round(used / limit, 4)\n"
        )},
        healed_attempt_patch={f"{pkg}/math.py": (
            f"def {fn}(used: int, limit: int) -> float:\n"
            f"    if limit <= 0:\n        return 1.0\n"
            f"    return round(used / limit, 4)\n"
        )},
        expect_first_fail=True,
    )


def _t_off_by_one(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """边界差一：少取一条。"""
    pkg = rng.choice(["catalog", "paginator", "timeline"])
    fn = rng.choice(["page_items", "first_n", "window_slice"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/slice_util.py": (
                f"def {fn}(items: list, n: int) -> list:\n"
                f"    # Bug: 少取一条（应为 items[:n]）\n"
                f"    return items[: n - 1]\n"
            ),
            f"{pkg}/views.py": (
                f"from {pkg}.slice_util import {fn}\n\n"
                f"def render_page(items: list, n: int) -> list:\n    return {fn}(items, n)\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.slice_util import {fn}\n\n"
            f"def test_{fn}_normal():\n    assert {fn}([1, 2, 3], 2) == [1, 2]\n\n"
            f"def test_{fn}_single_item():\n    assert {fn}([7], 1) == [7]\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="off_by_one", repo_dir=repo,
        issue_text=f"{fn} returns n-1 items instead of n, breaking {pkg}.views.render_page pagination",
        gold_files=[f"{pkg}/slice_util.py"],
        f2p_tests=[f"test_{pkg}.py::test_{fn}_single_item"],
        p2p_tests=[f"test_{pkg}.py::test_{fn}_normal"],
        first_attempt_patch={f"{pkg}/slice_util.py": (
            f"def {fn}(items: list, n: int) -> list:\n"
            f"    return items[: max(n - 2, 0)]\n"  # 仍差一
        )},
        healed_attempt_patch={f"{pkg}/slice_util.py": (
            f"def {fn}(items: list, n: int) -> list:\n"
            f"    return items[:n]\n"
        )},
        expect_first_fail=True,
    )


def _t_none_handling(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """None 解引用：查找未命中返回 None，调用方取字段崩溃。"""
    pkg = rng.choice(["users", "registry", "contacts"])
    fn = rng.choice(["find_user", "lookup_record", "get_contact"])
    fld = rng.choice(["email", "phone", "display_name"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/store.py": (
                f"_DB = {{1: {{'{fld}': 'a@x.com'}}, 2: {{'{fld}': 'b@x.com'}}}}\n\n"
                f"def {fn}(uid: int):\n"
                f"    return _DB.get(uid)  # 未命中返回 None\n"
            ),
            f"{pkg}/service.py": (
                f"from {pkg}.store import {fn}\n\n"
                f"def notify(uid: int) -> str:\n"
                f"    # Bug: 未命中时 None['{fld}'] 崩溃\n"
                f"    return {fn}(uid)['{fld}']\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.service import notify\n\n"
            f"def test_notify_found():\n    assert notify(1) == 'a@x.com'\n\n"
            f"def test_notify_missing_returns_empty():\n    assert notify(99) == ''\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="none_handling", repo_dir=repo,
        issue_text=f"TypeError 'NoneType' object is not subscriptable in notify; {fn} returns None for unknown uid",
        gold_files=[f"{pkg}/service.py"],
        f2p_tests=[f"test_{pkg}.py::test_notify_missing_returns_empty"],
        p2p_tests=[f"test_{pkg}.py::test_notify_found"],
        first_attempt_patch={f"{pkg}/service.py": (
            f"from {pkg}.store import {fn}\n\n"
            f"def notify(uid: int) -> str:\n"
            f"    rec = {fn}(uid)\n"
            f"    if rec is None:\n"
            f"        raise ValueError(f'user {{uid}} not found')  # 显式报错而非返回空串\n"
            f"    return rec['{fld}']\n"
        )},
        healed_attempt_patch={f"{pkg}/service.py": (
            f"from {pkg}.store import {fn}\n\n"
            f"def notify(uid: int) -> str:\n"
            f"    rec = {fn}(uid)\n"
            f"    return rec['{fld}'] if rec else ''\n"
        )},
        expect_first_fail=True,
    )


def _t_wrong_operator(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """运算符错误：折扣被加。"""
    pkg = rng.choice(["pricing", "payroll", "discounts"])
    fn = rng.choice(["apply_discount", "net_salary", "final_price"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/calc.py": (
                f"def {fn}(amount: float, discount: float) -> float:\n"
                f"    # Bug: 折扣被加上了\n"
                f"    return round(amount + discount, 2)\n"
            ),
            f"{pkg}/orders.py": (
                f"from {pkg}.calc import {fn}\n\n"
                f"def checkout_total(amount: float, discount: float) -> float:\n"
                f"    return {fn}(amount, discount)\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.calc import {fn}\n\n"
            f"def test_{fn}_normal():\n    assert {fn}(100.0, 10.0) == 90.0\n\n"
            f"def test_{fn}_full_discount():\n    assert {fn}(50.0, 50.0) == 0.0\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="wrong_operator", repo_dir=repo,
        issue_text=f"{fn} adds the discount instead of subtracting it (see {pkg}.orders.checkout_total)",
        gold_files=[f"{pkg}/calc.py"],
        f2p_tests=[f"test_{pkg}.py::test_{fn}_normal"],
        p2p_tests=[f"test_{pkg}.py::test_{fn}_full_discount"],
        first_attempt_patch={f"{pkg}/calc.py": (
            f"def {fn}(amount: float, discount: float) -> float:\n"
            f"    return round(amount * discount, 2)\n"  # 仍错：乘法
        )},
        healed_attempt_patch={f"{pkg}/calc.py": (
            f"def {fn}(amount: float, discount: float) -> float:\n"
            f"    return round(amount - discount, 2)\n"
        )},
        expect_first_fail=True,
    )


def _t_negative_clamp(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """负数截断：优惠超原价导致负数计税。"""
    pkg = rng.choice(["invoicing", "checkout", "settlement"])
    fn = rng.choice(["invoice_total", "payable", "grand_total"])
    tax_fn = rng.choice(["calc_tax", "vat_of", "levy"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/tax.py": (
                f"def {tax_fn}(amount: float, rate: float) -> float:\n"
                f"    return round(amount * rate, 2)\n"
            ),
            f"{pkg}/invoice.py": (
                f"from {pkg}.tax import {tax_fn}\n\n"
                f"def {fn}(subtotal: float, coupon: float, rate: float) -> float:\n"
                f"    # Bug: coupon > subtotal 时 net 为负\n"
                f"    net = subtotal - coupon\n"
                f"    return round(net + {tax_fn}(net, rate), 2)\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.invoice import {fn}\n\n"
            f"def test_{fn}_normal():\n    assert {fn}(100.0, 20.0, 0.1) == 88.0\n\n"
            f"def test_{fn}_coupon_exceeds():\n    assert {fn}(50.0, 80.0, 0.1) == 0.0\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="negative_clamp", repo_dir=repo,
        issue_text=f"{fn} returns a negative total when coupon exceeds subtotal ({pkg}/invoice.py, tax via {tax_fn})",
        gold_files=[f"{pkg}/invoice.py"],
        f2p_tests=[f"test_{pkg}.py::test_{fn}_coupon_exceeds"],
        p2p_tests=[f"test_{pkg}.py::test_{fn}_normal"],
        first_attempt_patch={f"{pkg}/invoice.py": (
            f"from {pkg}.tax import {tax_fn}\n\n"
            f"def {fn}(subtotal: float, coupon: float, rate: float) -> float:\n"
            f"    net = min(subtotal - coupon, 0.0)\n"  # 截断方向反了
            f"    return round(net + {tax_fn}(net, rate), 2)\n"
        )},
        healed_attempt_patch={f"{pkg}/invoice.py": (
            f"from {pkg}.tax import {tax_fn}\n\n"
            f"def {fn}(subtotal: float, coupon: float, rate: float) -> float:\n"
            f"    net = max(0.0, subtotal - coupon)\n"
            f"    return round(net + {tax_fn}(net, rate), 2)\n"
        )},
        expect_first_fail=True,
    )


def _t_signature_break(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """签名破坏陷阱：首轮补丁擅自删参（被静态门控拦截），修复轮恢复签名。"""
    pkg = rng.choice(["shipping", "freight", "logistics"])
    fn = rng.choice(["shipping_fee", "freight_cost", "delivery_price"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/fee.py": (
                f"def {fn}(weight_kg: float, distance_km: float, express: bool = False) -> float:\n"
                f"    # Bug: 未区分 express 倍率（应 x1.5）\n"
                f"    base = weight_kg * 1.2 + distance_km * 0.1\n"
                f"    return round(base, 2)\n"
            ),
            f"{pkg}/order.py": (
                f"from {pkg}.fee import {fn}\n\n"
                f"def order_fee(w: float, d: float, express: bool = False) -> float:\n"
                f"    return {fn}(w, d, express)\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.fee import {fn}\n\n"
            f"def test_{fn}_standard():\n    assert {fn}(10.0, 10.0) == 13.0\n\n"
            f"def test_{fn}_express():\n    assert {fn}(10.0, 10.0, True) == 19.5\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="signature_break", repo_dir=repo,
        issue_text=f"{fn} ignores the express flag; express orders are charged standard rate ({pkg}.order.order_fee passes it)",
        gold_files=[f"{pkg}/fee.py"],
        f2p_tests=[f"test_{pkg}.py::test_{fn}_express"],
        p2p_tests=[f"test_{pkg}.py::test_{fn}_standard"],
        first_attempt_patch={f"{pkg}/fee.py": (
            f"def {fn}(weight_kg: float, distance_km: float) -> float:\n"  # 擅自删参 → 冻结契约拦截
            f"    base = weight_kg * 1.2 + distance_km * 0.1\n"
            f"    return round(base * 1.5, 2)\n"
        )},
        healed_attempt_patch={f"{pkg}/fee.py": (
            f"def {fn}(weight_kg: float, distance_km: float, express: bool = False) -> float:\n"
            f"    base = weight_kg * 1.2 + distance_km * 0.1\n"
            f"    if express:\n        base *= 1.5\n"
            f"    return round(base, 2)\n"
        )},
        expect_first_fail=True,
    )


def _t_type_confusion(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """类型混淆：比率计算语义错误。"""
    pkg = rng.choice(["analytics", "telemetry", "insights"])
    fn = rng.choice(["growth_rate", "delta_pct", "change_ratio"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/stats.py": (
                f"def {fn}(old: float, new: float) -> float:\n"
                f"    # Bug: 返回的是 new/old 比值而非增量比率\n"
                f"    return round(new / old, 2)\n"
            ),
            f"{pkg}/report.py": (
                f"from {pkg}.stats import {fn}\n\n"
                f"def headline(old: float, new: float) -> float:\n    return {fn}(old, new)\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.stats import {fn}\n\n"
            f"def test_{fn}_normal():\n    assert {fn}(100.0, 150.0) == 0.5\n\n"
            f"def test_{fn}_zero_growth():\n    assert {fn}(80.0, 80.0) == 0.0\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="type_confusion", repo_dir=repo,
        issue_text=f"{fn} computes new/old instead of the delta ratio, breaking {pkg}.report.headline",
        gold_files=[f"{pkg}/stats.py"],
        f2p_tests=[f"test_{pkg}.py::test_{fn}_normal"],
        p2p_tests=[f"test_{pkg}.py::test_{fn}_zero_growth"],
        first_attempt_patch={f"{pkg}/stats.py": (
            f"def {fn}(old: float, new: float) -> float:\n"
            f"    return round(new / old - 1.5, 2)\n"  # 仍错
        )},
        healed_attempt_patch={f"{pkg}/stats.py": (
            f"def {fn}(old: float, new: float) -> float:\n"
            f"    return round((new - old) / old, 2)\n"
        )},
        expect_first_fail=True,
    )


def _t_unhandled_exception(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """未捕获异常：int() 解析脏数据崩溃。"""
    pkg = rng.choice(["ingest", "parsers", "etl"])
    fn = rng.choice(["parse_count", "to_int", "as_number"])
    caller = rng.choice(["load_rows", "sync_batch", "import_csv"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/convert.py": (
                f"def {fn}(raw: str) -> int:\n"
                f"    # Bug: 非数字输入直接 ValueError\n"
                f"    return int(raw)\n"
            ),
            f"{pkg}/pipeline.py": (
                f"from {pkg}.convert import {fn}\n\n"
                f"def {caller}(rows: list) -> list:\n    return [{fn}(r) for r in rows]\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.convert import {fn}\n\n"
            f"def test_{fn}_normal():\n    assert {fn}('42') == 42\n\n"
            f"def test_{fn}_dirty_input():\n    assert {fn}('n/a') == 0\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="unhandled_exception", repo_dir=repo,
        issue_text=(
            f"ValueError: invalid literal for int() with base 10: 'n/a' raised by {fn}, "
            f"crashing {pkg}.pipeline.{caller}"
        ),
        gold_files=[f"{pkg}/convert.py"],
        f2p_tests=[f"test_{pkg}.py::test_{fn}_dirty_input"],
        p2p_tests=[f"test_{pkg}.py::test_{fn}_normal"],
        first_attempt_patch={f"{pkg}/convert.py": (
            f"def {fn}(raw: str) -> int:\n"
            f"    return int(raw or 0)\n"  # 'n/a' 仍崩溃
        )},
        healed_attempt_patch={f"{pkg}/convert.py": (
            f"def {fn}(raw: str) -> int:\n"
            f"    try:\n        return int(raw)\n"
            f"    except (TypeError, ValueError):\n        return 0\n"
        )},
        expect_first_fail=True,
    )


def _t_logic_inversion(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """逻辑反转：黑名单判断写反。"""
    pkg = rng.choice(["gatekeeper", "access", "authz"])
    fn = rng.choice(["is_allowed", "can_access", "check_permission"])
    caller = rng.choice(["authorize", "gate", "middleware_check"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/rules.py": (
                f"BLACKLIST = {{'guest', 'banned'}}\n\n"
                f"def {fn}(role: str) -> bool:\n"
                f"    # Bug: 判断写反\n"
                f"    return role in BLACKLIST\n"
            ),
            f"{pkg}/api.py": (
                f"from {pkg}.rules import {fn}\n\n"
                f"def {caller}(role: str) -> bool:\n    return {fn}(role)\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.rules import {fn}\n\n"
            f"def test_{fn}_member():\n    assert {fn}('member') is True\n\n"
            f"def test_{fn}_banned():\n    assert {fn}('banned') is False\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="logic_inversion", repo_dir=repo,
        issue_text=f"{fn} grants access to blacklisted roles and denies members ({pkg}.api.{caller} relies on it)",
        gold_files=[f"{pkg}/rules.py"],
        f2p_tests=[f"test_{pkg}.py::test_{fn}_banned"],
        p2p_tests=[f"test_{pkg}.py::test_{fn}_member"],
        first_attempt_patch={f"{pkg}/rules.py": (
            f"BLACKLIST = {{'guest', 'banned'}}\n\n"
            f"def {fn}(role: str) -> bool:\n"
            f"    return role in BLACKLIST or role == 'member'\n"  # 仍错
        )},
        healed_attempt_patch={f"{pkg}/rules.py": (
            f"BLACKLIST = {{'guest', 'banned'}}\n\n"
            f"def {fn}(role: str) -> bool:\n"
            f"    return role not in BLACKLIST\n"
        )},
        expect_first_fail=True,
    )


def _t_boundary_value(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """边界值：恰好满额被误判为超额。"""
    pkg = rng.choice(["limits", "quotas", "gates"])
    fn = rng.choice(["is_over_quota", "exceeds_limit", "beyond_cap"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/check.py": (
                f"def {fn}(used: int, limit: int) -> bool:\n"
                f"    # Bug: 恰好等于限额应视为未超\n"
                f"    return used >= limit\n"
            ),
            f"{pkg}/api.py": (
                f"from {pkg}.check import {fn}\n\n"
                f"def reject_request(used: int, limit: int) -> bool:\n    return {fn}(used, limit)\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.check import {fn}\n\n"
            f"def test_{fn}_under():\n    assert {fn}(9, 10) is False\n\n"
            f"def test_{fn}_at_limit():\n    assert {fn}(10, 10) is False\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="boundary_value", repo_dir=repo,
        issue_text=f"{fn} flags users at exactly the limit as over quota ({pkg}.api.reject_request blocks them)",
        gold_files=[f"{pkg}/check.py"],
        f2p_tests=[f"test_{pkg}.py::test_{fn}_at_limit"],
        p2p_tests=[f"test_{pkg}.py::test_{fn}_under"],
        first_attempt_patch={f"{pkg}/check.py": (
            f"def {fn}(used: int, limit: int) -> bool:\n"
            f"    return used >= limit - 1\n"  # 仍错
        )},
        healed_attempt_patch={f"{pkg}/check.py": (
            f"def {fn}(used: int, limit: int) -> bool:\n"
            f"    return used > limit\n"
        )},
        expect_first_fail=True,
    )


def _t_mutable_default(rng: random.Random, repo: Path, case_id: str) -> IssueBenchmarkCase:
    """可变默认参数：跨调用共享列表导致结果污染。"""
    pkg = rng.choice(["caching", "sessions", "carts"])
    fn = rng.choice(["new_session", "empty_cart", "blank_ctx"])
    caller = rng.choice(["create_for", "spawn", "init_for"])
    _write_repo(
        repo,
        {
            f"{pkg}/__init__.py": "",
            f"{pkg}/factory.py": (
                f"def {fn}(owner: str, tags=[]) -> dict:\n"
                f"    # Bug: 可变默认参数跨调用共享\n"
                f"    tags.append(owner)\n"
                f"    return {{'owner': owner, 'tags': tags}}\n"
            ),
            f"{pkg}/service.py": (
                f"from {pkg}.factory import {fn}\n\n"
                f"def {caller}(owner: str) -> dict:\n    return {fn}(owner)\n"
            ),
        },
        f"test_{pkg}.py",
        (
            f"from {pkg}.service import {caller}\n\n"
            f"def test_{caller}_first():\n    assert {caller}('alice')['tags'] == ['alice']\n\n"
            f"def test_{caller}_second_clean():\n    assert {caller}('bob')['tags'] == ['bob']\n"
        ),
    )
    return IssueBenchmarkCase(
        case_id=case_id, category="mutable_default", repo_dir=repo,
        issue_text=f"{caller}('bob') returns tags polluted by previous {fn} calls (shared mutable default)",
        gold_files=[f"{pkg}/factory.py"],
        f2p_tests=[f"test_{pkg}.py::test_{caller}_second_clean"],
        p2p_tests=[f"test_{pkg}.py::test_{caller}_first"],
        first_attempt_patch={f"{pkg}/factory.py": (
            f"def {fn}(owner: str, tags=[]) -> dict:\n"
            f"    if owner not in tags:\n"  # 去重守卫：看似修复，仍污染共享列表
            f"        tags.append(owner)\n"
            f"    return {{'owner': owner, 'tags': tags}}\n"
        )},
        healed_attempt_patch={f"{pkg}/factory.py": (
            f"def {fn}(owner: str, tags=None) -> dict:\n"
            f"    tags = list(tags) if tags else []\n"
            f"    tags.append(owner)\n"
            f"    return {{'owner': owner, 'tags': tags}}\n"
        )},
        expect_first_fail=True,
    )


BUILDERS: list[Callable[[random.Random, Path, str], IssueBenchmarkCase]] = [
    _t_division, _t_off_by_one, _t_none_handling, _t_wrong_operator, _t_negative_clamp,
    _t_signature_break, _t_type_confusion, _t_unhandled_exception, _t_logic_inversion,
    _t_boundary_value, _t_mutable_default,
]

# 套件设计：40% 实例首轮必败（i % 5 ∈ {0,1}），60% 首轮即通过（first = healed）
FIRST_FAIL_PATTERN = (0, 1)


# ══════════════════ 套件构建与评测 ══════════════════


def build_synthetic_suite(base_dir: Path, size: int = 50,
                          seed: int = 20260101) -> list[IssueBenchmarkCase]:
    """构建 size 道确定性评测题。两次构建（同 seed）逐字节一致。"""
    base_dir.mkdir(parents=True, exist_ok=True)
    suite: list[IssueBenchmarkCase] = []
    for i in range(size):
        builder = BUILDERS[i % len(BUILDERS)]
        case_rng = random.Random(seed + i * 7919)
        repo = base_dir / f"case_{i:02d}"
        repo.mkdir(parents=True, exist_ok=True)
        case = builder(case_rng, repo, f"ISSUE_{i:03d}")
        if i % 5 not in FIRST_FAIL_PATTERN:
            case.first_attempt_patch = dict(case.healed_attempt_patch)
            case.expect_first_fail = False
        suite.append(case)
    return suite


def evaluate_agent_on_suite(
    suite: list[IssueBenchmarkCase],
    use_ast_localization: bool = True,
    max_healing_rounds: int = 2,
    timeout_sec: int = 30,
) -> dict[str, Any]:
    """运行评测集：真实沙箱 pytest 统计四项指标。"""
    loc_hits = 0
    first_pass = 0
    resolved_count = 0
    regressions = 0
    per_category: dict[str, dict[str, int]] = {}

    for case in suite:
        cat = per_category.setdefault(
            case.category, {"total": 0, "located": 0, "resolved": 0}
        )
        cat["total"] += 1

        contracts = {
            k: v.model_dump() for k, v in extract_repo_symbols(case.repo_dir).items()
        }
        predicted = (
            rank_suspect_files(case.repo_dir, case.issue_text, top_k=3)
            if use_ast_localization
            else []
        )
        if set(case.gold_files) & set(predicted):
            loc_hits += 1
            cat["located"] += 1

        passed_all = False
        p2p_ok = True
        for attempt in range(max_healing_rounds + 1):
            patch = case.first_attempt_patch if attempt == 0 else case.healed_attempt_patch

            # Step A: 静态门控（AST + 冻结契约）
            static_ok = all(
                audit_patched_code(f, code, contracts).passed
                for f, code in patch.items()
            )
            if not static_ok:
                continue  # 静态拦截 → 下一轮自愈

            # Step B: 沙箱 F2P + P2P 双门控
            res = run_sandbox_verification(
                case.repo_dir, patch, case.f2p_tests, case.p2p_tests,
                timeout_sec=timeout_sec,
            )
            p2p_ok = res.p2p_passed
            if res.resolved:
                passed_all = True
                if attempt == 0:
                    first_pass += 1
                break

        if passed_all:
            resolved_count += 1
            cat["resolved"] += 1
        if not p2p_ok:
            regressions += 1

    total = max(len(suite), 1)
    return {
        "total_cases": total,
        "localization_recall_at_3": round(loc_hits / total, 4),
        "one_shot_pass_at_1": round(first_pass / total, 4),
        "resolved_rate": round(resolved_count / total, 4),
        "p2p_regression_rate": round(regressions / total, 4),
        "per_category": per_category,
    }


def run_ablation_benchmark(
    base_dir: Path,
    out_dir: Path,
    size: int = 50,
    seed: int = 20260101,
    timeout_sec: int = 30,
) -> dict[str, Any]:
    """三组消融实验：Baseline 单轮 vs +AST 定位 vs +多轮自愈，落盘 JSON+Markdown。"""
    suite = build_synthetic_suite(base_dir, size=size, seed=seed)

    groups = {
        "baseline_single_round": evaluate_agent_on_suite(
            suite, use_ast_localization=False, max_healing_rounds=0, timeout_sec=timeout_sec),
        "ast_localized_single_round": evaluate_agent_on_suite(
            suite, use_ast_localization=True, max_healing_rounds=0, timeout_sec=timeout_sec),
        "ast_localized_multi_round_healing": evaluate_agent_on_suite(
            suite, use_ast_localization=True, max_healing_rounds=2, timeout_sec=timeout_sec),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "suite_size": len(suite),
        "seed": seed,
        "groups": groups,
    }
    (out_dir / "benchmark_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    b = groups["baseline_single_round"]
    a = groups["ast_localized_single_round"]
    h = groups["ast_localized_multi_round_healing"]
    md = [
        "# Micro-SWE-Bench 消融实验报告",
        "",
        f"- 套件规模: {len(suite)} 题（确定性沙箱评测，全程零 LLM 调用）",
        f"- 随机种子: {seed}",
        "",
        "| 指标 | Baseline(单轮) | +AST定位(单轮) | +AST定位+多轮自愈 |",
        "|---|---|---|---|",
        f"| Localization Recall@3 | {b['localization_recall_at_3']:.1%} | "
        f"{a['localization_recall_at_3']:.1%} | {h['localization_recall_at_3']:.1%} |",
        f"| One-shot Pass@1 | {b['one_shot_pass_at_1']:.1%} | "
        f"{a['one_shot_pass_at_1']:.1%} | {h['one_shot_pass_at_1']:.1%} |",
        f"| Resolved Rate | {b['resolved_rate']:.1%} | "
        f"{a['resolved_rate']:.1%} | {h['resolved_rate']:.1%} |",
        f"| P2P Regression Rate | {b['p2p_regression_rate']:.1%} | "
        f"{a['p2p_regression_rate']:.1%} | {h['p2p_regression_rate']:.1%} |",
        "",
        f"**自愈净增益（Resolved Rate）**: "
        f"{(h['resolved_rate'] - a['resolved_rate']):+.1%}",
        "",
        "## 分类统计（多轮自愈组）",
        "",
        "| 类别 | 题数 | 定位命中 | 解决 |",
        "|---|---|---|---|",
    ]
    for cat, stat in h["per_category"].items():
        md.append(f"| {cat} | {stat['total']} | {stat['located']} | {stat['resolved']} |")
    (out_dir / "benchmark_report.md").write_text("\n".join(md), encoding="utf-8")
    return report
