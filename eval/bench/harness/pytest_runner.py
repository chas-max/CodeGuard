"""在沙箱里按 nodeid 跑 pytest，并解析每个用例的 PASSED/FAILED/ERROR。

不依赖 pytest-json-report 等插件；`-rA` 的短摘要足够稳定。
prepare 阶段允许 `network=prepare` 做一次性的环境初始化（例如生成版本文件），
测试阶段仍然强制 `network=none`。
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from codeguard.models import SandboxStatus
from codeguard.sandbox.models import SandboxSpec

from ..metrics import parse_pytest_rA

_LOG_LIMIT = 4000


@dataclass
class TestRun:
    statuses: dict[str, str] = field(default_factory=dict)
    infra_error: str = ""
    exit_code: int = 0
    logs: str = ""
    invalid: tuple[str, ...] = ()

    def passed(self, node: str) -> bool:
        return self.statuses.get(node) == "passed"


def is_well_formed_node(node: str) -> bool:
    """数据集里的参数化 nodeid 可能被日志截断（如 `[assert` 丢了 ` x]`）。

    方括号不配对的 nodeid 直接标 invalid，不传给 pytest；否则 pytest 会以
    exit 4 中止整批，连带把同批有效用例也跳过。
    """
    return node.count("[") == node.count("]")


def build_command(test_command: str, nodes: list[str]) -> str:
    base = (test_command or "python -m pytest -q").strip()
    if "-rA" not in base:
        base += " -rA"
    return base + " " + " ".join(shlex.quote(node) for node in nodes)


def prepare_environment(
    sandbox,
    *,
    workspace: Path | str,
    image: str,
    commands: list[str],
    timeout_sec: int = 600,
    job_id: str = "bench",
) -> TestRun:
    items = [str(item) for item in commands if str(item).strip()]
    if not items:
        return TestRun()
    spec = SandboxSpec(
        job_id=f"{job_id}-prepare", workspace=Path(workspace),
        image=image, commands=items, timeout_sec=timeout_sec, network="prepare",
    )
    result = sandbox.run(spec)
    logs = ((result.stdout or "") + "\n" + (result.stderr or ""))[-_LOG_LIMIT:]
    if result.status == SandboxStatus.infra_error:
        return TestRun(
            infra_error=result.infra_error or "prepare 沙箱异常",
            exit_code=int(result.exit_code or -1), logs=logs,
        )
    return TestRun(infra_error="", exit_code=int(result.exit_code or 0), logs=logs)


def run_tests(
    sandbox,
    *,
    workspace: Path | str,
    image: str,
    nodes: list[str],
    test_command: str = "python -m pytest -q",
    timeout_sec: int = 300,
    job_id: str = "bench",
    chunk_size: int = 50,
) -> TestRun:
    unique = list(dict.fromkeys(str(node) for node in nodes if str(node).strip()))
    if not unique:
        return TestRun()
    invalid = tuple(node for node in unique if not is_well_formed_node(node))
    runnable = [node for node in unique if is_well_formed_node(node)]
    statuses: dict[str, str] = {node: "invalid" for node in invalid}
    if not runnable:
        return TestRun(
            statuses=statuses, invalid=invalid,
            logs=f"全部 nodeid 均不完整（{len(invalid)} 条），未执行 pytest",
        )
    exit_code = 0
    logs: list[str] = []
    if invalid:
        logs.append(f"跳过 {len(invalid)} 条被数据集截断的 nodeid")
    for start in range(0, len(runnable), max(1, chunk_size)):
        chunk = runnable[start:start + chunk_size]
        command = build_command(test_command, chunk)
        spec = SandboxSpec(
            job_id=f"{job_id}-chunk{start}", workspace=Path(workspace),
            image=image, commands=[command],
            timeout_sec=timeout_sec, network="none",
        )
        result = sandbox.run(spec)
        exit_code = max(exit_code, int(result.exit_code or 0))
        logs.append(f"$ {command}\n{result.stdout or ''}\n{result.stderr or ''}")
        if result.status == SandboxStatus.infra_error:
            return TestRun(
                statuses=statuses,
                infra_error=result.infra_error or "sandbox 环境异常",
                exit_code=exit_code,
                logs="\n".join(logs)[-_LOG_LIMIT:],
                invalid=invalid,
            )
        parsed = parse_pytest_rA((result.stdout or "") + "\n" + (result.stderr or ""))
        if int(result.exit_code or 0) == 4 and not parsed:
            # 数据集里的 nodeid 可能已失效（改名/参数变化）；退回按测试文件跑，
            # 仍然只解析目标 nodeid，避免整批被一个坏 nodeid 拖死。
            files = sorted({node.split("::", 1)[0] for node in chunk if "::" in node})
            if files:
                fallback_command = build_command(test_command, files)
                fallback_spec = SandboxSpec(
                    job_id=f"{job_id}-chunk{start}-fallback",
                    workspace=Path(workspace), image=image,
                    commands=[fallback_command], timeout_sec=timeout_sec,
                    network="none",
                )
                fallback = sandbox.run(fallback_spec)
                exit_code = max(exit_code, int(fallback.exit_code or 0))
                logs.append(
                    f"$ {fallback_command}\n{fallback.stdout or ''}\n{fallback.stderr or ''}"
                )
                parsed = parse_pytest_rA(
                    (fallback.stdout or "") + "\n" + (fallback.stderr or "")
                )
        for node in chunk:
            statuses[node] = parsed.get(node, "missing")
    return TestRun(
        statuses=statuses, infra_error="", exit_code=exit_code,
        logs="\n".join(logs)[-_LOG_LIMIT:], invalid=invalid,
    )


__all__ = [
    "TestRun", "build_command", "is_well_formed_node", "prepare_environment", "run_tests",
]
