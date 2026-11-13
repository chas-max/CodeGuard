"""MCP 工具桥接层：JSON Schema -> Pydantic 动态建模 + 能力门禁注册到 ToolBox。"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from ..engine.policy import AgentsMcpSettings, McpServerPolicy, Policy
from ..tools.base import error_result, ok_result, truncate
from ..tools.spec import ToolResult, ToolSpec, capability_allows, min_capability
from .client import McpClientManager

_JSON_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _canonical_role(role: str) -> str:
    if role.startswith("finding_verifier"):
        return "finding_verifier"
    return role


def _sanitize_model_name(name: str) -> str:
    cleaned = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    return f"McpArgs_{cleaned}"


def schema_to_args_model(tool_name: str, input_schema: dict[str, Any] | None) -> type[BaseModel]:
    """将 MCP Tool 的 JSON Schema 转为 Pydantic 参数模型。"""
    schema = input_schema or {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])

    fields: dict[str, tuple[Any, Any]] = {}
    for prop_name, prop_def in props.items():
        if not isinstance(prop_def, dict):
            fields[prop_name] = (Any, None)
            continue
        json_type = prop_def.get("type")
        if isinstance(json_type, list):
            non_null = [t for t in json_type if t != "null"]
            py_type = _JSON_TYPE_MAP.get(non_null[0], Any) if non_null else Any
        else:
            py_type = _JSON_TYPE_MAP.get(str(json_type), Any)

        description = str(prop_def.get("description") or "")
        if prop_name in required:
            fields[prop_name] = (py_type, Field(..., description=description))
        else:
            default = prop_def.get("default", None)
            fields[prop_name] = (py_type | None, Field(default=default, description=description))

    return create_model(
        _sanitize_model_name(tool_name),
        __config__=ConfigDict(extra="allow"),
        **fields,  # type: ignore[call-overload]
    )


def _effective_mcp_capability(
    *,
    mcp_policy: AgentsMcpSettings,
    mode: str,
    role_cap: str,
    env_max_cap: str,
) -> str:
    caps = [mcp_policy.max_capability, role_cap]
    if mode == "external":
        caps.append(mcp_policy.external_max_capability)
    if env_max_cap:
        caps.append(env_max_cap)
    return min_capability(*caps)


def _format_mcp_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, indent=2)
    except Exception:
        return str(content)


_MANIFEST_EXACT_NAMES = frozenset({
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
    "pom.xml",
})


def _has_manifest_change(changed_files: list[str] | None) -> bool:
    """当未提供 PR 变更列表时保持放行；有变更列表时仅在涉及依赖清单文件才激活 osv_security。"""
    if not changed_files:
        return True
    for raw in changed_files:
        base = str(raw).replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base in _MANIFEST_EXACT_NAMES:
            return True
        if base.startswith("requirements") and base.endswith((".txt", ".in")):
            return True
        if base.endswith(".gradle"):
            return True
    return False


def bind_mcp_tools(
    toolbox: Any,
    *,
    role: str,
    policy: Policy,
    mcp_manager: McpClientManager,
    role_cap: str,
    mcp_disabled: bool = False,
    mcp_max_capability: str = "",
    context_env: dict[str, str] | None = None,
    force_stdio: bool = False,
    changed_files: list[str] | None = None,
) -> list[str]:
    """按策略与能力门禁，将可用 MCP 工具注册进指定 `toolbox`。返回已绑定工具名列表。"""
    if mcp_disabled:
        return []
    mcp_policy = policy.agents.mcp
    if not mcp_policy.enabled:
        return []

    effective_cap = _effective_mcp_capability(
        mcp_policy=mcp_policy,
        mode=policy.mode,
        role_cap=role_cap,
        env_max_cap=mcp_max_capability,
    )
    if effective_cap == "none":
        return []

    canon_role = _canonical_role(role)
    manifest_changed = _has_manifest_change(changed_files)
    bound_names: list[str] = []

    for server in mcp_policy.servers:
        if not server.enabled:
            continue
        if server.name == "osv_security" and not manifest_changed:
            continue
        if server.allowed_roles and canon_role not in server.allowed_roles and role not in server.allowed_roles:
            continue
        if not capability_allows(effective_cap, server.capability):
            continue

        try:
            tool_infos = mcp_manager.list_server_tools(server, force_stdio=force_stdio)
        except Exception:
            continue

        allowed_set = set(server.allowed_tools) if server.allowed_tools else None
        blocked_set = set(server.blocked_tools)

        for info in tool_infos:
            if not info.name or info.name in blocked_set:
                continue
            if allowed_set is not None and info.name not in allowed_set:
                continue

            exposed_name = (
                f"mcp__{server.name}__{info.name}"
                if mcp_policy.tool_name_prefix
                else info.name
            )
            args_model = schema_to_args_model(exposed_name, info.input_schema)
            spec = ToolSpec(
                name=exposed_name,
                description=f"[MCP:{server.name}] {info.description}".strip(),
                args_model=args_model,
                capability=server.capability,
                timeout_sec=server.timeout_sec,
                source="mcp",
                server=server.name,
                raw_schema=dict(info.input_schema or {}),
            )

            def _make_handler(
                srv: McpServerPolicy,
                remote_name: str,
                max_chars: int,
            ):
                def _handler(args: BaseModel) -> ToolResult:
                    payload = args.model_dump(exclude_none=True)
                    res = mcp_manager.call_tool(
                        srv,
                        remote_name,
                        payload,
                        context_env=context_env,
                    )
                    if not res.ok:
                        err_res = error_result(
                            "failed",
                            f"MCP[{srv.name}.{remote_name}] 失败: {res.error}",
                            meta={
                                "source": "mcp",
                                "server": srv.name,
                                "remote_tool": remote_name,
                            },
                        )
                        err_res.duration_ms = res.duration_ms
                        return err_res
                    text, _ = truncate(_format_mcp_content(res.content), max_chars)
                    ok_res = ok_result(
                        text,
                        meta={
                            "source": "mcp",
                            "server": srv.name,
                            "remote_tool": remote_name,
                        },
                    )
                    ok_res.duration_ms = res.duration_ms
                    return ok_res

                return _handler

            toolbox.register_custom_spec(
                spec,
                _make_handler(server, info.name, min(server.max_output_chars, toolbox.limits.max_output_chars)),
            )
            bound_names.append(exposed_name)

    return bound_names
