"""沙箱运行器：构造 spec、执行命令、生成 ValidationReport。"""
from __future__ import annotations

from pathlib import Path

from ..models import SandboxStatus, ValidationReport
from .backend import SandboxBackend
from .models import SandboxResult, SandboxSpec


def default_test_commands(has_tests: bool) -> list[str]:
    if not has_tests:
        return ["python -m pytest -q"]
    return ["python -m pytest -q"]


def run_spec(
    backend: SandboxBackend,
    *,
    job_id: str,
    workspace: Path,
    commands: list[str],
    image: str,
    timeout_sec: int,
    memory: str = "1g",
    cpus: float = 1.0,
    pids_limit: int = 256,
    network: str = "none",
    env: dict[str, str] | None = None,
) -> SandboxResult:
    spec = SandboxSpec(
        job_id=job_id,
        workspace=workspace,
        image=image,
        commands=commands,
        timeout_sec=timeout_sec,
        network=network,  # type: ignore[arg-type]
        env=env or {},
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
    )
    return backend.run(spec)


def validation_from_results(
    *,
    repro: SandboxResult,
    regression: SandboxResult,
    tree_hash: str,
) -> ValidationReport:
    if repro.status == SandboxStatus.infra_error or regression.status == SandboxStatus.infra_error:
        return ValidationReport(
            status=SandboxStatus.infra_error,
            commands=[],
            repro_passed=False,
            regression_passed=False,
            logs=(repro.stdout + "\n" + regression.stdout)[-8000:],
            error=repro.infra_error or regression.infra_error or "沙箱环境异常",
            tree_hash=tree_hash,
        )
    repro_ok = repro.status == SandboxStatus.passed
    regression_ok = regression.status == SandboxStatus.passed
    status = SandboxStatus.passed if (repro_ok and regression_ok) else SandboxStatus.failed
    return ValidationReport(
        status=status,
        commands=[],
        repro_passed=repro_ok,
        regression_passed=regression_ok,
        logs=(repro.stdout + "\n" + regression.stdout)[-8000:],
        tree_hash=tree_hash,
    )


__all__ = ["default_test_commands", "run_spec", "validation_from_results"]
