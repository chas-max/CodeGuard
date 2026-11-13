from __future__ import annotations

from pathlib import Path

import pytest

from codeguard.models import SandboxStatus
from codeguard.sandbox.docker_backend import DockerSandboxBackend
from codeguard.sandbox.fake_backend import FakeSandboxBackend
from codeguard.sandbox.models import SandboxResult, SandboxSpec


def _spec(tmp_path: Path, **override) -> SandboxSpec:
    params = dict(
        job_id="j", workspace=tmp_path, image="img", commands=["python -m pytest -q"],
        timeout_sec=10, network="none",
    )
    params.update(override)
    return SandboxSpec(**params)


def test_spec_validation(tmp_path: Path):
    with pytest.raises(ValueError):
        _spec(tmp_path, commands=[])
    with pytest.raises(ValueError):
        _spec(tmp_path, env={"CG_ALLOW_NETWORK": "1"})


def test_docker_kwargs_security(tmp_path: Path):
    backend = DockerSandboxBackend(client=object())
    kwargs = backend.build_kwargs(_spec(tmp_path), "pytest")
    assert kwargs["network_mode"] == "none"
    assert kwargs["read_only"] is True
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["user"] == "1000:1000"
    assert "docker.sock" not in str(kwargs["volumes"])
    assert kwargs["labels"]["codeguard.job"] == "j"
    # 命令必须作为 entrypoint 的单个脚本参数，不能被 shell 二次包装后丢弃。
    assert kwargs["entrypoint"] == ["/bin/sh", "-lc"]
    assert kwargs["command"] == ["pytest"]


def test_docker_kwargs_resolves_relative_workspace(tmp_path: Path):
    backend = DockerSandboxBackend(client=object())
    kwargs = backend.build_kwargs(_spec(Path("relative-ws")), "pytest")
    volume = next(iter(kwargs["volumes"]))
    assert Path(volume).is_absolute()
    assert volume.endswith("relative-ws")


def test_fake_backend(tmp_path: Path):
    fake = FakeSandboxBackend([SandboxResult(status=SandboxStatus.passed)]) 
    result = fake.run(_spec(tmp_path))
    assert result.status == SandboxStatus.passed
    assert fake.specs
