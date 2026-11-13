from __future__ import annotations

from pathlib import Path

import pytest

from codeguard.models import SandboxStatus
from codeguard.sandbox.docker_backend import DockerSandboxBackend
from codeguard.sandbox.health import check_docker, check_image
from codeguard.sandbox.models import SandboxSpec

pytestmark = pytest.mark.docker


def test_real_docker_isolation(tmp_path: Path):
    ok, message = check_docker()
    if not ok:
        pytest.skip(f"Docker 不可用: {message}")
    image = "codeguard-sandbox-python:3.12"
    image_ok, image_msg = check_image(image)
    if not image_ok:
        pytest.skip(f"沙箱镜像不存在: {image_msg}")
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    backend = DockerSandboxBackend()
    result = backend.run(SandboxSpec(
        job_id="docker-test", workspace=tmp_path, image=image,
        commands=["python -m pytest -q"], timeout_sec=60, network="none",
    ))
    assert result.status == SandboxStatus.passed
    assert (tmp_path / "test_ok.py").exists()
