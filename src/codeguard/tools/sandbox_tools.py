"""沙箱执行工具：run_tests / run_check / run_command。

所有执行都构造 SandboxSpec 走 ctx.sandbox.run()，测试阶段强制 network=none，
并继承 DockerSandboxBackend.build_kwargs 的只读根、非 root、cap_drop 等不变量。
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from ..engine.repo_map import is_test_path
from ..models import SandboxStatus
from ..sandbox.models import SandboxResult, SandboxSpec
from .base import error_result, ok_result, safe_resolve, truncate
from .spec import ToolLimits, ToolResult

FORBIDDEN_EXECUTABLES = {
    "curl", "wget", "ssh", "scp", "nc", "ncat", "telnet", "docker", "podman",
    "sudo", "su", "bash", "sh", "zsh", "powershell", "pwsh", "cmd",
    "apt-get", "apt", "yum", "dnf", "apk", "npm", "pnpm", "yarn", "pip", "pip3",
}
FORBIDDEN_PHRASES = (
    "git push", "git remote", "pip install", "pip3 install", "npm install",
    "docker run", "docker exec", "chmod 777", "rm -rf /",
)


class RunTestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list, description="测试文件相对路径；空表示按策略跑全量")
    timeout_sec: int = Field(default=0, ge=0, le=600)


class RunCheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class RunCommandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str] = Field(min_length=1, max_length=32)
    timeout_sec: int = Field(default=0, ge=0, le=600)


@dataclass
class SandboxEnv:
    ctx: Any
    policy: Any
    job_id: str
    root: Path
    limits: ToolLimits
    slots: Any = None
    allow_commands: list[str] = field(default_factory=list)
    remaining_sec: Callable[[], float] = lambda: float("inf")


def _test_command(policy: Any, tests: list[str]) -> str:
    joined = " ".join(shlex.quote(t) for t in tests)
    template = policy.sandbox.test_command_template or ""
    if "{tests}" in template:
        return template.replace("{tests}", joined).strip()
    commands = list(policy.sandbox.test_commands or ["python -m pytest -q"])
    first = commands[0] + ((" " + joined) if joined else "")
    return " && ".join([first] + commands[1:])


def _timeout(env: SandboxEnv, requested: int) -> int:
    cfg = env.ctx.cfg
    budget = min(
        int(env.policy.sandbox.timeout_sec or 300),
        int(getattr(cfg, "sandbox_timeout_sec", 300) or 300),
        int(env.limits.sandbox_timeout_sec or 120),
    )
    if requested > 0:
        budget = min(budget, requested)
    remaining = env.remaining_sec()
    if remaining != float("inf"):
        budget = min(budget, max(1, int(remaining)))
    return max(1, budget)


def _run(env: SandboxEnv, command: str, *, timeout_sec: int = 0) -> ToolResult:
    budget = _timeout(env, timeout_sec)
    if env.remaining_sec() <= 0:
        return error_result("infra_error", "工具循环时间预算已耗尽")
    sandbox = getattr(env.ctx, "sandbox", None)
    if sandbox is None:
        return error_result("infra_error", "沙箱不可用（Docker 未启动或后端未注入）")
    cfg = env.ctx.cfg
    spec = SandboxSpec(
        job_id=env.job_id,
        workspace=env.root,
        image=env.policy.sandbox.image,
        commands=[command],
        timeout_sec=budget,
        network="none",
        memory=cfg.sandbox_memory,
        cpus=cfg.sandbox_cpus,
        pids_limit=cfg.sandbox_pids,
    )
    acquired = True
    if env.slots is not None:
        acquired = env.slots.acquire(timeout=min(5, budget))
        if not acquired:
            return error_result("infra_error", "沙箱并发槽位等待超时")
    try:
        result = sandbox.run(spec)
    except Exception as exc:  # noqa: BLE001 - 统一转基础设施错误
        return error_result("infra_error", f"沙箱调用失败: {exc}")
    finally:
        if acquired and env.slots is not None:
            env.slots.release()
    return _from_result(result, env.limits.max_output_chars)


def _from_result(result: SandboxResult, max_chars: int) -> ToolResult:
    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    body = f"exit_code={result.exit_code}\n{output}".strip()
    body, truncated = truncate(body, max_chars)
    meta = {
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_sec": result.duration_sec,
        "container_id": result.container_id[:12],
        "truncated": truncated,
    }
    if result.status == SandboxStatus.infra_error:
        return error_result("infra_error", result.infra_error or "沙箱基础设施错误", meta=meta)
    status = "ok" if result.status == SandboxStatus.passed and result.exit_code == 0 else "failed"
    return ok_result(body, status=status, meta=meta)


def run_tests(env: SandboxEnv, args: RunTestsArgs) -> ToolResult:
    tests: list[str] = []
    for raw in args.paths:
        try:
            rel, path = safe_resolve(env.root, raw)
        except ValueError as exc:
            return error_result("denied", str(exc))
        if not path.is_file():
            return error_result("invalid_args", f"测试文件不存在: {rel}")
        if not is_test_path(rel):
            return error_result(
                "denied",
                f"只允许运行测试文件，拒绝 {rel}；若需执行命令请用 run_command（受白名单限制）",
            )
        if rel not in tests:
            tests.append(rel)
    return _run(env, _test_command(env.policy, tests), timeout_sec=args.timeout_sec)


def run_check(env: SandboxEnv, args: RunCheckArgs) -> ToolResult:
    checks = list(env.policy.sandbox.checks or [])
    for check in checks:
        if check.name == args.name:
            return _run(env, check.command)
    available = ", ".join(c.name for c in checks) or "（策略未配置 checks）"
    return error_result("denied", f"未知检查项 {args.name!r}；可用: {available}")


def _allowed_prefixes(env: SandboxEnv) -> list[str]:
    raw: list[str] = []
    raw.extend(env.policy.sandbox.test_commands or [])
    raw.extend(check.command for check in (env.policy.sandbox.checks or []))
    raw.extend(env.allow_commands or [])
    result: list[str] = []
    for item in raw:
        text = (item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _forbidden(command: str, argv: list[str]) -> bool:
    first = Path(argv[0]).name.lower()
    if first in FORBIDDEN_EXECUTABLES:
        return True
    lowered = command.lower()
    return any(phrase in lowered for phrase in FORBIDDEN_PHRASES)


def run_command(env: SandboxEnv, args: RunCommandArgs) -> ToolResult:
    argv = [str(item) for item in args.argv]
    if any(not item.strip() for item in argv):
        return error_result("invalid_args", "argv 不能包含空字符串")
    if any(len(item) > 200 for item in argv):
        return error_result("invalid_args", "argv 单个元素超过 200 字符")
    command = " ".join(shlex.quote(item) for item in argv)
    if _forbidden(command, argv):
        return error_result("denied", "命令命中禁止列表（网络/安装/推送/提权/宿主 shell）")
    allowed = _allowed_prefixes(env)
    if not any(command == item or command.startswith(item + " ") for item in allowed):
        return error_result(
            "denied",
            "命令不在白名单内；允许的命令前缀: " + (", ".join(allowed) or "（空）"),
        )
    return _run(env, command, timeout_sec=args.timeout_sec)


__all__ = [
    "RunTestsArgs", "RunCheckArgs", "RunCommandArgs", "SandboxEnv",
    "run_tests", "run_check", "run_command",
]
