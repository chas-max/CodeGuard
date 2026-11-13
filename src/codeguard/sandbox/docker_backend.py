"""Docker 容器隔离沙箱。"""
from __future__ import annotations

import time
from pathlib import Path

from ..errors import SandboxError
from ..models import SandboxStatus
from .backend import SandboxBackend
from .models import SandboxResult, SandboxSpec


class DockerSandboxBackend(SandboxBackend):
    def __init__(self, client=None):
        if client is not None:
            self._client = client
            return
        try:
            import docker  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"未安装 docker Python SDK: {exc}") from exc
        try:
            self._client = docker.from_env()
            self._client.ping()
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"Docker daemon 不可用: {exc}") from exc

    def build_kwargs(self, spec: SandboxSpec, command: str) -> dict:
        """构造容器参数；测试直接断言这些安全不变量。"""
        # Docker 会把相对路径当成 named volume；必须用绝对宿主路径做 bind mount。
        workspace = str(Path(spec.workspace).resolve())
        return {
            "image": spec.image,
            # 镜像自带 ENTRYPOINT ["/bin/sh", "-lc"]，command 只能传脚本本身；
            # 否则会被拼成 `/bin/sh -lc /bin/sh -lc <cmd>`，外层 shell 直接退出 0。
            "entrypoint": ["/bin/sh", "-lc"],
            "command": [command],
            "working_dir": "/workspace",
            "volumes": {workspace: {"bind": "/workspace", "mode": "rw"}},
            "network_mode": "none" if spec.network == "none" else "bridge",
            "read_only": True,
            "tmpfs": {"/tmp": "rw,noexec,nosuid,size=256m"},
            "mem_limit": spec.memory,
            "memswap_limit": spec.memory,
            "nano_cpus": int(spec.cpus * 1_000_000_000),
            "pids_limit": spec.pids_limit,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "user": "1000:1000",
            "environment": {
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                **spec.env,
            },
            "detach": True,
            "auto_remove": False,
            "labels": {"codeguard.job": spec.job_id, "codeguard.stage": "test"},
        }

    def run(self, spec: SandboxSpec) -> SandboxResult:
        started = time.time()
        container = None
        command = " && ".join(spec.commands)
        try:
            container = self._client.containers.run(**self.build_kwargs(spec, command))
            try:
                wait_result = container.wait(timeout=spec.timeout_sec)
                exit_code = int(wait_result.get("StatusCode", 0))
                timed_out = False
            except Exception as exc:  # noqa: BLE001 - 超时/异常统一 kill
                timed_out = "timeout" in str(exc).lower() or "timed out" in str(exc).lower()
                exit_code = -1
                try:
                    container.kill()
                except Exception:  # noqa: BLE001
                    pass
            logs = b""
            try:
                logs = container.logs(stdout=True, stderr=True)
            except Exception:  # noqa: BLE001
                pass
            output = logs.decode("utf-8", errors="replace") if isinstance(logs, bytes) else str(logs)
            if timed_out:
                status = SandboxStatus.infra_error
                infra = f"容器超时（{spec.timeout_sec}s）"
            elif exit_code == 0:
                status = SandboxStatus.passed
                infra = ""
            else:
                status = SandboxStatus.failed
                infra = ""
            return SandboxResult(
                status=status, exit_code=exit_code, timed_out=timed_out,
                stdout=output[:20000], stderr="", duration_sec=round(time.time() - started, 3),
                container_id=getattr(container, "id", ""), infra_error=infra,
            )
        except Exception as exc:  # noqa: BLE001
            return SandboxResult(
                status=SandboxStatus.infra_error, exit_code=-1,
                duration_sec=round(time.time() - started, 3),
                infra_error=f"Docker 执行失败: {exc}",
            )
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:  # noqa: BLE001
                    pass


__all__ = ["DockerSandboxBackend"]
