"""Docker 健康检查（延迟导入 SDK，保证无 docker 依赖时测试可导入模块）。"""
from __future__ import annotations


def check_docker() -> tuple[bool, str]:
    try:
        import docker  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return False, f"未安装 docker Python SDK: {exc}"
    try:
        client = docker.from_env()
        client.ping()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def check_image(image: str) -> tuple[bool, str]:
    try:
        import docker  # type: ignore
        client = docker.from_env()
        client.images.get(image)
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


__all__ = ["check_docker", "check_image"]
