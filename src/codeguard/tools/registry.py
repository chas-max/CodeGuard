"""工具注册表：按 Agent 职责分配最小权限工具集。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from . import edit_tools, fs_tools, memory_tools, review_tools, sandbox_tools
from .base import error_result, summarize_args
from .spec import (
    Capability,
    ToolCapability,
    ToolLimits,
    ToolResult,
    ToolSpec,
    capability_allows,
    min_capability,
)

# 每个 Agent 的最小权限工具集合；只有 coder/fixer 拥有写工具与沙箱执行权。
ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "pr_reader": (
        "read_file", "diff_base_head", "get_pr_metadata",
    ),
    "review_logic": (
        "read_file", "grep", "ast_symbols", "find_symbol_usages",
        "diff_base_head", "related_files",
    ),
    "review_security": (
        "read_file", "grep", "ast_symbols", "find_symbol_usages",
        "diff_base_head", "query_findings",
    ),
    "review_architecture": (
        "read_file", "list_dir", "grep", "ast_symbols",
        "find_symbol_usages", "related_files",
    ),
    "review_tests": (
        "read_file", "grep", "inspect_tests", "diff_base_head",
        "related_files", "query_findings",
    ),
    "coder": (
        "read_file", "list_dir", "grep", "related_files",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "check_patch_guard",
        "search_procedural_memory",
        "run_tests", "run_check", "run_command",
        "edit_file", "apply_patch", "write_file",
    ),
    "qa_checker": (
        "read_file", "diff_base_head", "ast_symbols",
    ),
    # 兼容保留旧角色映射
    "semantic_detector": (
        "read_file", "list_dir", "grep", "related_files",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "inspect_tests",
    ),
    "finding_verifier": (
        "read_file", "grep",
        "ast_symbols", "find_symbol_usages", "diff_base_head",
    ),
    "issue_verifier": (
        "read_file", "list_dir", "grep",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "inspect_tests",
    ),
    "reviewer": (
        "read_file", "list_dir", "grep",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "inspect_tests",
        "run_tests", "run_check",
    ),
    "fixer": (
        "read_file", "list_dir", "grep", "related_files",
        "ast_symbols", "find_symbol_usages", "diff_base_head", "check_patch_guard",
        "search_procedural_memory",
        "run_tests", "run_check", "run_command",
        "edit_file", "apply_patch", "write_file",
    ),
}

TOOL_CAPABILITY: dict[str, ToolCapability] = {
    "read_file": "read",
    "list_dir": "read",
    "grep": "read",
    "related_files": "read",
    "ast_symbols": "read",
    "find_symbol_usages": "read",
    "diff_base_head": "read",
    "get_pr_metadata": "read",
    "inspect_tests": "read",
    "query_findings": "read",
    "check_patch_guard": "read",
    "search_procedural_memory": "read",
    "run_tests": "exec",
    "run_check": "exec",
    "run_command": "exec",
    "edit_file": "write",
    "apply_patch": "write",
    "write_file": "write",
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "read_file": "读取工作区文件的一段内容（带行号范围），用于核对代码事实。",
    "list_dir": "列出工作区目录内容（最多两层），用于定位文件。",
    "grep": "在工作区文本文件中按正则搜索，返回 path:line: 内容（最多 50 条）。",
    "related_files": "根据变更文件找一跳导入方、同目录测试与同名测试。",
    "ast_symbols": "提取文件内类、函数、方法定义大纲与精确行号范围，减少大文件盲读。",
    "find_symbol_usages": "跨工作区查找符号的定义（def）、导入（import）与调用引用（ref）。",
    "diff_base_head": "查看单文件 base/head 精确 unified diff 及 Python 公共 API 签名变更。",
    "get_pr_metadata": "获取当前 PR 脱敏元数据（仓库、PR 号、分支、标题、正文与变更文件列表）。",
    "inspect_tests": "静态扫描关联测试文件与测试用例名（不执行代码），确认测试覆盖情况。",
    "query_findings": "按文件/严重度/来源查询前序静态分析产出的告警记录。",
    "check_patch_guard": "只读预检修复补丁是否符合 allowed_files 与测试保护红线。",
    "search_procedural_memory": "检索本地程序化向量记忆库中的历史问题修复策略与反面教训（RAG）。",
    "run_tests": "在 Docker 沙箱中运行指定测试文件（network=none）。",
    "run_check": "在沙箱中运行仓库策略里声明的检查项（如 lint/typecheck）。",
    "run_command": "在沙箱中运行白名单内的命令（argv 形式，不接受 shell 拼接）。",
    "edit_file": "精确局部替换单个源文件中的唯一匹配文本片段（Search-and-Replace）。",
    "apply_patch": "把 unified diff 应用到当前修复副本；路径必须在允许列表内。",
    "write_file": "覆盖或新建单个文件；路径必须在允许列表内。",
}

ARGS_MODELS: dict[str, type[BaseModel]] = {
    "read_file": fs_tools.ReadFileArgs,
    "list_dir": fs_tools.ListDirArgs,
    "grep": fs_tools.GrepArgs,
    "related_files": fs_tools.RelatedFilesArgs,
    "ast_symbols": review_tools.AstSymbolsArgs,
    "find_symbol_usages": review_tools.FindSymbolUsagesArgs,
    "diff_base_head": review_tools.DiffBaseHeadArgs,
    "get_pr_metadata": review_tools.GetPrMetadataArgs,
    "inspect_tests": review_tools.InspectTestsArgs,
    "query_findings": review_tools.QueryFindingsArgs,
    "check_patch_guard": review_tools.CheckPatchGuardArgs,
    "search_procedural_memory": memory_tools.SearchProceduralMemoryArgs,
    "run_tests": sandbox_tools.RunTestsArgs,
    "run_check": sandbox_tools.RunCheckArgs,
    "run_command": sandbox_tools.RunCommandArgs,
    "edit_file": edit_tools.EditFileArgs,
    "apply_patch": edit_tools.ApplyPatchArgs,
    "write_file": edit_tools.WriteFileArgs,
}

TOOL_TIMEOUTS: dict[str, int] = {
    "read_file": 15,
    "list_dir": 15,
    "grep": 30,
    "related_files": 30,
    "ast_symbols": 15,
    "find_symbol_usages": 25,
    "diff_base_head": 20,
    "get_pr_metadata": 10,
    "inspect_tests": 15,
    "query_findings": 10,
    "check_patch_guard": 10,
    "search_procedural_memory": 15,
    "run_tests": 300,
    "run_check": 300,
    "run_command": 300,
    "edit_file": 30,
    "apply_patch": 60,
    "write_file": 30,
}

ROLE_GROUPS = (
    "pr_reader",
    "review_logic",
    "review_security",
    "review_architecture",
    "review_tests",
    "coder",
    "qa_checker",
    "semantic_detector",
    "finding_verifier",
    "issue_verifier",
    "reviewer",
    "fixer",
)
# 默认低风险阶段对普通审查角色收敛至 read；coder 仅在沙箱隔离副本中写白名单文件，
# 其他只读角色仍受自身 role_cap = "read" 严格限制。
TIER_CAPABILITY: dict[str, Capability] = {"T0": "read", "T1": "write", "T2": "write"}


def role_group(role: str) -> str:
    for group in ROLE_GROUPS:
        if role == group or role.startswith(group + "_"):
            return group
    return ""


def role_tools(role: str) -> tuple[str, ...]:
    if role in ROLE_TOOLS:
        return ROLE_TOOLS[role]
    return ROLE_TOOLS.get(role_group(role), ())


def effective_capability(
    *,
    role: str,
    policy: Any,
    tier: str = "T1",
    cfg: Any = None,
) -> Capability:
    """role 档位、tier、部署模式、环境变量四层上限取最小值；关闭即 none。"""
    group = role_group(role)
    if not group:
        return "none"
    settings = policy.agents.tools
    if not settings.enabled:
        return "none"
    if cfg is not None and getattr(cfg, "agent_tools_disabled", False):
        return "none"
    role_cap = getattr(settings.roles, group, "read")
    tier_cap = "write" if group == "coder" else TIER_CAPABILITY.get(str(tier or "T1"), "write")
    mode_cap = settings.external_max_capability if policy.mode == "external" else "write"
    env_cap = (getattr(cfg, "agent_tools_max_capability", "") or "write") if cfg else "write"
    return min_capability(role_cap, tier_cap, mode_cap, env_cap)


def limits_from_policy(policy: Any) -> ToolLimits:
    settings = policy.agents.tools
    return ToolLimits(
        max_steps=settings.max_steps,
        max_tool_calls=settings.max_tool_calls,
        max_wall_sec=settings.max_wall_sec,
        max_output_chars=settings.max_output_chars,
        sandbox_timeout_sec=settings.sandbox_timeout_sec,
        protocol=str(getattr(settings, "protocol", "auto") or "auto"),
    )


def _allowed_commands(policy: Any) -> list[str]:
    settings = policy.agents.tools
    commands: list[str] = []
    for item in list(settings.allow_commands or []):
        text = (item or "").strip()
        if text and text not in commands:
            commands.append(text)
    return commands


class AgentToolBox:
    """按能力裁剪后的工具集合；dispatch 负责参数校验、审计与错误归一化。"""

    def __init__(
        self,
        *,
        role: str,
        capability: Capability,
        root: Path,
        job_id: str,
        ctx: Any,
        policy: Any,
        limits: ToolLimits,
        repo_map: dict | None = None,
        allowed_files: list[str] | None = None,
        allowed_test_addition_files: list[str] | None = None,
        slots: Any = None,
        names: list[str] | None = None,
        base_root: Path | None = None,
        pr_context: dict[str, Any] | None = None,
        findings: list[dict[str, Any]] | None = None,
        memory_store: Any = None,
    ):
        self.role = role
        self.capability = capability
        self.root = root
        self.base_root = base_root
        self.pr_context = dict(pr_context or {})
        self.findings = list(findings or [])
        self.memory_store = memory_store
        self.job_id = job_id
        self.ctx = ctx
        self.policy = policy
        self.limits = limits
        self.repo_map = repo_map or {}
        self.allowed_files = list(allowed_files or [])
        self.allowed_test_addition_files = list(allowed_test_addition_files or [])
        self._events: list[dict] = []
        self._used_memory_ids: list[str] = []
        self._deadline: float | None = None
        self._enabled_names = set(names) if names is not None else None
        self._specs: list[ToolSpec] = []
        self._handlers: dict[str, Callable[[Any], ToolResult]] = {}
        self._edit_env = edit_tools.EditEnv(
            root,
            self.allowed_files,
            allowed_test_addition_files=self.allowed_test_addition_files,
        )
        self._sandbox_env = sandbox_tools.SandboxEnv(
            ctx=ctx, policy=policy, job_id=job_id, root=root, limits=limits,
            slots=slots, allow_commands=_allowed_commands(policy),
        )
        self._sandbox_env.remaining_sec = self.remaining_sec

    # ── 构造 ──
    def _register(self, name: str) -> None:
        args_model = ARGS_MODELS[name]
        self._specs.append(
            ToolSpec(
                name=name,
                description=TOOL_DESCRIPTIONS[name],
                args_model=args_model,
                capability=TOOL_CAPABILITY[name],
                timeout_sec=TOOL_TIMEOUTS[name],
            )
        )

    def add_handler(self, name: str, handler: Callable[[Any], ToolResult]) -> None:
        if self._enabled_names is not None and name not in self._enabled_names:
            return
        self._register(name)
        self._handlers[name] = handler

    def register_custom_spec(
        self,
        spec: ToolSpec,
        handler: Callable[[Any], ToolResult],
    ) -> None:
        """动态注册外部扩展工具（如 MCP Server 发现的远端/本地工具）。"""
        if not capability_allows(self.capability, spec.capability):
            return
        self._specs = [item for item in self._specs if item.name != spec.name]
        self._specs.append(spec)
        self._handlers[spec.name] = handler

    def bind_default_handlers(self) -> None:
        current_repo = str(self.pr_context.get("repo") or "")
        fs_map: dict[str, Callable[[Any], ToolResult]] = {
            "read_file": lambda a: fs_tools.read_file(self.root, a, max_chars=self.limits.max_output_chars),
            "list_dir": lambda a: fs_tools.list_dir(self.root, a, max_chars=self.limits.max_output_chars),
            "grep": lambda a: fs_tools.grep(
                self.root, a, repo_map=self.repo_map, max_chars=self.limits.max_output_chars),
            "related_files": lambda a: fs_tools.related_files(
                self.root, a, repo_map=self.repo_map, max_chars=self.limits.max_output_chars),
            "ast_symbols": lambda a: review_tools.ast_symbols(
                self.root, a, max_chars=self.limits.max_output_chars),
            "find_symbol_usages": lambda a: review_tools.find_symbol_usages(
                self.root, a, max_chars=self.limits.max_output_chars),
            "diff_base_head": lambda a: review_tools.diff_base_head(
                self.root, self.base_root, a, max_chars=self.limits.max_output_chars),
            "get_pr_metadata": lambda a: review_tools.get_pr_metadata(
                self.pr_context, a, max_chars=self.limits.max_output_chars),
            "inspect_tests": lambda a: review_tools.inspect_tests(
                self.root, a, repo_map=self.repo_map, max_chars=self.limits.max_output_chars),
            "query_findings": lambda a: review_tools.query_findings(
                self.findings, a, max_chars=self.limits.max_output_chars),
            "check_patch_guard": lambda a: review_tools.check_patch_guard(
                self.root,
                self._edit_env.all_allowed_paths(),
                a,
                max_chars=self.limits.max_output_chars,
            ),
            "search_procedural_memory": lambda a: memory_tools.search_procedural_memory(
                self.memory_store,
                a,
                current_repo=current_repo,
                max_chars=self.limits.max_output_chars,
            ),
            "run_tests": lambda a: sandbox_tools.run_tests(self._sandbox_env, a),
            "run_check": lambda a: sandbox_tools.run_check(self._sandbox_env, a),
            "run_command": lambda a: sandbox_tools.run_command(self._sandbox_env, a),
            "edit_file": lambda a: edit_tools.edit_file(self._edit_env, a),
            "apply_patch": lambda a: edit_tools.apply_patch(self._edit_env, a),
            "write_file": lambda a: edit_tools.write_file(self._edit_env, a),
        }
        for name in fs_map:
            self.add_handler(name, fs_map[name])

    # ── 协议 ──
    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def used_memory_ids(self) -> list[str]:
        return list(self._used_memory_ids)

    def set_deadline(self, deadline: float | None) -> None:
        self._deadline = deadline

    def remaining_sec(self) -> float:
        if self._deadline is None:
            return float("inf")
        return max(0.0, self._deadline - time.monotonic())

    def dispatch(self, name: str, args: dict[str, Any]) -> ToolResult:
        spec = next((item for item in self._specs if item.name == name), None)
        if spec is None:
            return error_result("denied", f"工具不可用: {name}；可用: {[s.name for s in self._specs]}")
        if not capability_allows(self.capability, spec.capability):
            return error_result("denied", f"当前 Agent 无 {spec.capability} 权限: {name}")
        try:
            parsed = spec.args_model.model_validate(args or {})
        except ValidationError as exc:
            return error_result("invalid_args", f"{name} 参数非法: {exc.errors()[:3]}")
        started = time.monotonic()
        try:
            result = self._handlers[name](parsed)
        except Exception as exc:  # noqa: BLE001 - 单个工具失败不拖垮整个 Agent
            result = error_result("failed", f"{name} 执行异常: {type(exc).__name__}: {exc}")
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if not result.output_chars:
            result.output_chars = len(result.content)
        if name == "search_procedural_memory" and result.ok:
            for mid in list((result.meta or {}).get("memory_ids") or []):
                if mid and mid not in self._used_memory_ids:
                    self._used_memory_ids.append(str(mid))
        self._events.append({
            "role": self.role,
            "tool": name,
            "source": spec.source,
            "server": spec.server,
            "args": summarize_args(args),
            "status": result.status,
            "duration_ms": result.duration_ms,
            "output_chars": result.output_chars,
            "error": (result.error or "")[:300],
        })
        return result


def build_toolbox(
    *,
    role: str,
    root: Path,
    job_id: str,
    ctx: Any,
    policy: Any,
    tier: str = "T1",
    repo_map: dict | None = None,
    allowed_files: list[str] | None = None,
    allowed_test_addition_files: list[str] | None = None,
    limits: ToolLimits | None = None,
    slots: Any = None,
    base_root: Path | None = None,
    pr_context: dict[str, Any] | None = None,
    findings: list[dict[str, Any]] | None = None,
    mcp_manager: Any = None,
    memory_store: Any = None,
) -> AgentToolBox | None:
    """构造受限工具箱；能力为 none 或没有可用工具时返回 None（调用方回退单轮）。"""
    cfg = getattr(ctx, "cfg", None)
    capability = effective_capability(role=role, policy=policy, tier=tier, cfg=cfg)
    if capability == "none":
        return None
    names = [
        name for name in role_tools(role)
        if name in TOOL_CAPABILITY and capability_allows(capability, TOOL_CAPABILITY[name])
    ]
    if not (allowed_files or allowed_test_addition_files):
        names = [name for name in names if TOOL_CAPABILITY[name] != "write"]
    if not names:
        return None
    box = AgentToolBox(
        role=role,
        capability=capability,
        root=root,
        job_id=job_id,
        ctx=ctx,
        policy=policy,
        limits=limits or limits_from_policy(policy),
        repo_map=repo_map,
        allowed_files=allowed_files,
        allowed_test_addition_files=allowed_test_addition_files,
        slots=slots,
        names=names,
        base_root=base_root,
        pr_context=pr_context,
        findings=findings,
        memory_store=memory_store if memory_store is not None else getattr(ctx, "memory_store", None),
    )
    box.bind_default_handlers()

    mcp_policy = getattr(getattr(policy, "agents", None), "mcp", None)
    if policy is not None and mcp_policy is not None:
        from ..mcp import McpClientManager, bind_mcp_tools

        mgr = mcp_manager if mcp_manager is not None else getattr(ctx, "mcp_manager", None)
        if mgr is None:
            mgr = McpClientManager()
        context_env: dict[str, str] = {"CODEGUARD_REPO_ROOT": str(root)}
        if isinstance(pr_context, dict) and pr_context.get("repo"):
            context_env["CODEGUARD_PR_REPO"] = str(pr_context["repo"])
        reports_dir = Path("reports").resolve()
        if reports_dir.is_dir():
            context_env["CODEGUARD_REPORTS_DIR"] = str(reports_dir)

        bind_mcp_tools(
            box,
            role=role,
            policy=policy,
            mcp_manager=mgr,
            role_cap=capability,
            mcp_disabled=bool(getattr(cfg, "mcp_disabled", False)),
            mcp_max_capability=str(getattr(cfg, "mcp_max_capability", "") or ""),
            context_env=context_env,
            changed_files=list((pr_context or {}).get("changed_files") or []),
        )

    return box


__all__ = [
    "ROLE_TOOLS", "TOOL_CAPABILITY", "TOOL_DESCRIPTIONS", "ARGS_MODELS",
    "ROLE_GROUPS", "TIER_CAPABILITY", "role_group", "role_tools",
    "effective_capability", "limits_from_policy", "AgentToolBox", "build_toolbox",
]
