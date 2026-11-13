"""MCP Client 管理器：支持 stdio JSON-RPC 2.0、HTTP/SSE 与内置 Server 快速探测。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from ..engine.policy import AgentsMcpSettings, McpServerPolicy
from .models import McpCallResult, McpServerStatus, McpToolInfo

_PROJECT_SRC = Path(__file__).resolve().parents[2]

_SAFE_SYS_ENV_KEYS = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "VIRTUAL_ENV",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "PYTHONPATH",
)

_BUILTIN_SERVER_MODULES = frozenset({
    "codeguard.mcp.servers.osv_server",
    "codeguard.mcp.servers.git_server",
    "codeguard.mcp.servers.knowledge_server",
    "codeguard.mcp.servers.github_server",
})


def build_stdio_env(
    server: McpServerPolicy,
    context_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """构造隔离的 MCP 子进程环境变量。

    默认不传递主进程 LLM/管理员密钥，仅保留必要系统变量与策略允许的 `env_vars`。
    """
    env: dict[str, str] = {}
    for key in _SAFE_SYS_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value

    # 强制 Windows / 子进程以 UTF-8 进行 stdio JSON-RPC 通信
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    src_str = str(_PROJECT_SRC)
    existing_pp = env.get("PYTHONPATH", "")
    if src_str not in existing_pp.split(os.pathsep):
        env["PYTHONPATH"] = f"{src_str}{os.pathsep}{existing_pp}" if existing_pp else src_str

    for var_name in server.env_vars:
        if var_name and var_name in os.environ:
            env[var_name] = os.environ[var_name]

    if context_env:
        for key, value in context_env.items():
            if value is not None:
                env[str(key)] = str(value)
    return env


def _resolve_stdio_command(server: McpServerPolicy) -> tuple[str, list[str]]:
    cmd = (server.command or "").strip()
    if cmd in ("python", "python3"):
        cmd = sys.executable
    return cmd, list(server.args or [])


def _builtin_module_name(server: McpServerPolicy) -> str:
    if server.transport != "stdio":
        return ""
    args = server.args or []
    if len(args) >= 2 and args[0] == "-m" and args[1] in _BUILTIN_SERVER_MODULES:
        return args[1]
    return ""


def _run_coro_sync(coro: Any) -> Any:
    """在同步函数或已有事件循环（如 FastAPI）中安全运行协程。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _extract_mcp_text(result_obj: dict[str, Any]) -> Any:
    """从 MCP `tools/call` 响应中提取文本或结构化结果。"""
    structured = result_obj.get("structuredContent")
    if structured is not None:
        return structured
    content_list = result_obj.get("content") or []
    texts: list[str] = []
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(str(item.get("text", "")))
    if not texts:
        return content_list
    joined = "\n".join(texts)
    if len(texts) == 1:
        try:
            return json.loads(joined)
        except Exception:
            return joined
    return joined


def _stdio_rpc_exchange(
    server: McpServerPolicy,
    method: str,
    params: dict[str, Any],
    *,
    context_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """通过 stdio 换行 JSON-RPC 2.0 与外部或本地 MCP Server 交互。"""
    cmd, args = _resolve_stdio_command(server)
    if not cmd:
        raise ValueError(f"MCP Server {server.name} 缺少 command 配置")

    env = build_stdio_env(server, context_env=context_env)
    proc = subprocess.Popen(
        [cmd, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        try:
            stderr_chunks.append(proc.stderr.read())
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    messages: dict[int, dict[str, Any]] = {}
    reader_done = threading.Event()

    def _read_stdout() -> None:
        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and "id" in msg and msg["id"] is not None:
                    messages[int(msg["id"])] = msg
        finally:
            reader_done.set()

    reader_thread = threading.Thread(target=_read_stdout, daemon=True)
    reader_thread.start()

    try:
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "codeguard", "version": "1.0.0"},
            },
        }
        proc.stdin.write(json.dumps(init_req, ensure_ascii=False) + "\n")
        proc.stdin.flush()

        deadline = time.monotonic() + float(server.timeout_sec)
        while 1 not in messages:
            if time.monotonic() >= deadline or reader_done.is_set():
                break
            time.sleep(0.01)

        if 1 not in messages:
            stderr_text = "".join(stderr_chunks).strip()
            raise TimeoutError(
                f"MCP {server.name} initialize 超时或提前退出: {stderr_text[:300]}"
            )

        notif = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        req = {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}
        proc.stdin.write(json.dumps(notif, ensure_ascii=False) + "\n")
        proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        proc.stdin.close()

        while 2 not in messages:
            if time.monotonic() >= deadline or reader_done.is_set():
                break
            time.sleep(0.01)

        if 2 not in messages:
            stderr_text = "".join(stderr_chunks).strip()
            raise TimeoutError(
                f"MCP {server.name} {method} 超时或无响应: {stderr_text[:300]}"
            )

        resp = messages[2]
        if "error" in resp and resp["error"]:
            err = resp["error"]
            msg_text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"MCP {server.name} 返回错误: {msg_text}")
        return resp.get("result") or {}
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=1.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _http_rpc_exchange(
    server: McpServerPolicy,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """通过 Streamable HTTP / SSE JSON-RPC POST 与远程 MCP Server 交互。"""
    if not server.url:
        raise ValueError(f"MCP Server {server.name} 缺少 url 配置")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **dict(server.headers or {}),
    }
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    with httpx.Client(timeout=float(server.timeout_sec)) as client:
        resp = client.post(server.url, headers=headers, json=payload)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            data_obj: dict[str, Any] = {}
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if raw and raw != "[DONE]":
                        data_obj = json.loads(raw)
        else:
            data_obj = resp.json()
    if isinstance(data_obj, dict) and data_obj.get("error"):
        raise RuntimeError(f"MCP {server.name} HTTP 错误: {data_obj['error']}")
    if isinstance(data_obj, dict) and "result" in data_obj:
        return data_obj["result"] or {}
    return data_obj if isinstance(data_obj, dict) else {"content": data_obj}


class McpClientManager:
    """统一管理 CodeGuard 的 MCP Server 探测、工具发现与执行。"""

    def __init__(self) -> None:
        self._tool_cache: dict[tuple[Any, ...], list[McpToolInfo]] = {}

    @staticmethod
    def _cache_key(server: McpServerPolicy) -> tuple[Any, ...]:
        return (
            server.name,
            server.transport,
            server.command,
            tuple(server.args or []),
            server.url,
        )

    def _inspect_builtin_tools(self, server: McpServerPolicy, module_name: str) -> list[McpToolInfo]:
        mod = importlib.import_module(module_name)
        mcp_obj = getattr(mod, "mcp", None)
        if mcp_obj is None:
            raise RuntimeError(f"内置 MCP 模块 {module_name} 未导出 mcp 实例")
        raw_tools = _run_coro_sync(mcp_obj.list_tools())
        infos: list[McpToolInfo] = []
        for item in raw_tools:
            schema = getattr(item, "inputSchema", None) or {}
            infos.append(
                McpToolInfo(
                    server=server.name,
                    name=str(getattr(item, "name", "")),
                    description=str(getattr(item, "description", "") or ""),
                    input_schema=dict(schema) if isinstance(schema, dict) else {},
                )
            )
        return infos

    def list_server_tools(
        self,
        server: McpServerPolicy,
        *,
        force_stdio: bool = False,
    ) -> list[McpToolInfo]:
        """获取指定 MCP Server 提供的工具清单。

        `force_stdio=False` 时对内置 FastMCP Server 走内存元信息反射并缓存，
        避免每次构箱拉起子进程；`force_stdio=True` 时走真实 stdio JSON-RPC 握手。
        """
        if not server.enabled:
            return []
        key = self._cache_key(server)
        if not force_stdio and key in self._tool_cache:
            return list(self._tool_cache[key])

        builtin_mod = _builtin_module_name(server)
        if builtin_mod and not force_stdio:
            tools = self._inspect_builtin_tools(server, builtin_mod)
            self._tool_cache[key] = list(tools)
            return tools

        if server.transport == "stdio":
            result = _stdio_rpc_exchange(server, "tools/list", {})
        else:
            result = _http_rpc_exchange(server, "tools/list", {})

        raw_tools = result.get("tools") or []
        tools = [
            McpToolInfo(
                server=server.name,
                name=str(item.get("name", "")),
                description=str(item.get("description", "") or ""),
                input_schema=dict(item.get("inputSchema") or {}),
            )
            for item in raw_tools
            if isinstance(item, dict) and item.get("name")
        ]
        self._tool_cache[key] = list(tools)
        return tools

    def call_tool(
        self,
        server: McpServerPolicy,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        context_env: dict[str, str] | None = None,
    ) -> McpCallResult:
        """通过真实传输通道执行 MCP 工具调用。"""
        t0 = time.monotonic()
        try:
            params = {"name": tool_name, "arguments": dict(arguments or {})}
            if server.transport == "stdio":
                result = _stdio_rpc_exchange(
                    server, "tools/call", params, context_env=context_env
                )
            else:
                result = _http_rpc_exchange(server, "tools/call", params)

            duration_ms = int((time.monotonic() - t0) * 1000)
            is_error = bool(result.get("isError", False))
            extracted = _extract_mcp_text(result)
            if is_error:
                return McpCallResult(
                    ok=False,
                    server=server.name,
                    tool=tool_name,
                    content=extracted,
                    error=str(extracted),
                    duration_ms=duration_ms,
                )
            return McpCallResult(
                ok=True,
                server=server.name,
                tool=tool_name,
                content=extracted,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            return McpCallResult(
                ok=False,
                server=server.name,
                tool=tool_name,
                error=str(exc),
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

    def probe_server(
        self,
        server: McpServerPolicy,
        *,
        force_stdio: bool = True,
    ) -> McpServerStatus:
        """探测单个 MCP Server 的连通性与工具清单。"""
        if not server.enabled:
            return McpServerStatus(
                name=server.name,
                enabled=False,
                transport=server.transport,
                ok=False,
                error="disabled",
                capability=server.capability,
                allowed_roles=list(server.allowed_roles),
            )
        t0 = time.monotonic()
        try:
            tools = self.list_server_tools(server, force_stdio=force_stdio)
            return McpServerStatus(
                name=server.name,
                enabled=True,
                transport=server.transport,
                ok=True,
                tools_count=len(tools),
                tools=[t.name for t in tools],
                capability=server.capability,
                allowed_roles=list(server.allowed_roles),
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as exc:
            return McpServerStatus(
                name=server.name,
                enabled=True,
                transport=server.transport,
                ok=False,
                error=str(exc),
                capability=server.capability,
                allowed_roles=list(server.allowed_roles),
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

    def probe_all(
        self,
        mcp_policy: AgentsMcpSettings,
        *,
        force_stdio: bool = True,
    ) -> list[McpServerStatus]:
        """探测策略中配置的所有 MCP Server。"""
        return [
            self.probe_server(server, force_stdio=force_stdio)
            for server in (mcp_policy.servers or [])
        ]
