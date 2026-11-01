"""确定性静态代码安全门控：AST 语法检查、高危调用拦截与冻结符号契约校验。

设计动机（同构于漫剧敏感词机审的"左移测试"）：在不启动耗时沙箱前，毫秒级拦截
语法幻觉、越权系统调用与公共接口签名破坏，节省无效沙箱开销。
"""
from __future__ import annotations

import ast
from typing import Any

from .models import StaticAuditVerdict

# 严禁在生成补丁中出现的高危调用（含 os.system / shutil.rmtree 等破坏性系统操作）
FORBIDDEN_CALLS = {"eval", "exec", "os.system", "shutil.rmtree", "subprocess.call", "subprocess.run"}

# 可选 Ruff 增强：未安装或调用失败时静默降级（不阻塞主门控）


def audit_patched_code(
    file_path: str,
    new_code: str,
    symbol_bible: dict[str, dict[str, Any]],
) -> StaticAuditVerdict:
    """对生成的补丁代码执行确定性 AST 静态审计。

    检查顺序（严重度递减）：语法错误 → 高危系统调用 → 冻结签名契约。
    """
    try:
        tree = ast.parse(new_code, filename=file_path)
    except SyntaxError as err:
        return StaticAuditVerdict(
            passed=False,
            issues=[f"SyntaxError at line {err.lineno}: {err.msg}"],
            severity="critical",
            category="syntax_error",
            feedback=f"代码存在语法错误（第 {err.lineno} 行）: {err.msg}",
        )

    # 检查高危系统调用
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            try:
                func_str = ast.unparse(node.func)
            except Exception:  # noqa: BLE001
                continue
            if func_str in FORBIDDEN_CALLS:
                return StaticAuditVerdict(
                    passed=False,
                    issues=[f"检测到禁用高危调用: {func_str}"],
                    severity="critical",
                    category="security_violation",
                    feedback=f"安全门控拦截：严禁在补丁中使用高危函数 {func_str}",
                )

    # 校验是否破坏 symbol_bible 中冻结的公共函数签名
    defined_funcs: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_funcs[node.name] = [a.arg for a in node.args.args if a.arg != "self"]

    broken_issues: list[str] = []
    for sym in (symbol_bible or {}).values():
        if sym.get("file_path") != file_path or not sym.get("frozen_signature", True):
            continue
        fname = sym.get("symbol_name", "")
        expected_args = sym.get("arg_names", [])
        if fname not in defined_funcs:
            broken_issues.append(
                f"丢失冻结公共函数定义: {fname}({', '.join(expected_args)})"
            )
        elif defined_funcs[fname] != expected_args:
            broken_issues.append(
                f"函数 {fname} 签名被破坏: 期望参数 {expected_args}，实际参数 {defined_funcs[fname]}"
            )

    if broken_issues:
        return StaticAuditVerdict(
            passed=False,
            issues=broken_issues,
            severity="major",
            category="signature_broken",
            feedback="；".join(broken_issues) + "。请保持公共接口参数签名兼容！",
        )

    return StaticAuditVerdict(
        passed=True,
        issues=[],
        severity="none",
        category="none",
        feedback="AST 语法与接口契约静态审计通过",
    )
