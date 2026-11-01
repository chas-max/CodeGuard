"""CodeGuard 强类型 Pydantic 模型与状态合并 Reducers。

同构映射（源自 PATH_A_CODING_AGENT_PLAN.md）：
- SymbolContract  <- 漫剧 CharacterCard（冻结公共函数签名，防止"改 A 文件搞崩 B 文件调用方"）
- PatchUnit       <- 漫剧 Shot（按 unit_id 幂等更新，支持单文件局部重试与断点恢复）
- StaticAuditVerdict <- 漫剧 ReviewResult（确定性静态门控结论）
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def merge_patch_units(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 unit_id 字段幂等合并补丁槽位：支持单文件局部重试与断点恢复。"""
    by_id: dict[str, dict[str, Any]] = {
        u["unit_id"]: dict(u) for u in (left or []) if u.get("unit_id")
    }
    for u in right or []:
        uid = u.get("unit_id")
        if not uid:
            continue
        by_id[uid] = {**by_id.get(uid, {}), **u}
    return list(by_id.values())


def merge_symbol_contracts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """按 symbol_id 合并 AST 符号契约圣经（同构于 merge_character_bible）。"""
    merged = dict(left or {})
    for k, v in (right or {}).items():
        merged[k] = {**merged.get(k, {}), **v}
    return merged


class SymbolContract(BaseModel):
    """AST 符号契约卡片（对应漫剧中的 CharacterCard，跨节点冻结函数签名）。"""

    symbol_id: str = Field(..., description="唯一符号ID，如 SYM_001")
    file_path: str = Field(..., description="所属文件相对路径")
    symbol_name: str = Field(..., description="函数或类名称")
    kind: str = Field(default="function", description="function | class | method")
    arg_names: list[str] = Field(default_factory=list, description="位置与关键字参数名列表")
    return_annotation: str = Field(default="", description="返回值类型注解字符串")
    callers: list[str] = Field(default_factory=list, description="跨文件调用方列表")
    frozen_signature: bool = Field(default=True, description="是否冻结公共参数签名禁止破坏")


class PatchUnit(BaseModel):
    """单文件代码修复与单测槽位（对应漫剧中的 Shot）。"""

    unit_id: str = Field(..., description="槽位ID，如 U001")
    target_file: str = Field(..., description="待修改源码相对路径")
    test_file: str = Field(default="", description="关联或新增的测试文件相对路径")
    issue_hypothesis: str = Field(default="", description="该文件的故障根因假设")
    patched_code: str = Field(default="", description="修复后的完整文件代码")
    git_diff: str = Field(default="", description="标准 Unified Diff 文本")
    status: str = Field(default="pending", description="pending | testing | passed | failed")
    attempts: int = Field(default=0, description="沙箱修复尝试次数")
    f2p_passed: bool = Field(default=False, description="Fail-to-Pass 复现测试是否通过")
    p2p_passed: bool = Field(default=False, description="Pass-to-Pass 回归测试是否通过")
    last_traceback: str = Field(default="", description="最近一次沙箱报错精简堆栈")


class StaticAuditVerdict(BaseModel):
    """静态 AST 与契约门控结论（对应漫剧中的 ReviewResult）。"""

    passed: bool
    issues: list[str] = Field(default_factory=list)
    severity: str = Field(default="none", description="none | minor | major | critical")
    category: str = Field(
        default="none",
        description="syntax_error | signature_broken | security_violation | lint_error | none",
    )
    feedback: str = Field(default="")


class SandboxTestResult(BaseModel):
    """隔离沙箱 pytest 执行结果。"""

    f2p_passed: bool
    p2p_passed: bool
    resolved: bool
    branch_coverage_pct: float = Field(default=0.0)
    error_summary: str = Field(default="")
    raw_stdout: str = Field(default="")
