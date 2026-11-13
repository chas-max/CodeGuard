"""Docker 隔离沙箱：后端协议、Docker 实现、fake 实现、工作区与运行器。"""

from .backend import SandboxBackend
from .fake_backend import FakeSandboxBackend
from .models import SandboxResult, SandboxSpec

__all__ = ["SandboxBackend", "FakeSandboxBackend", "SandboxResult", "SandboxSpec"]
