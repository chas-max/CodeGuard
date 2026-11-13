"""沙箱后端协议。"""
from __future__ import annotations

from typing import Protocol

from .models import SandboxResult, SandboxSpec


class SandboxBackend(Protocol):
    def run(self, spec: SandboxSpec) -> SandboxResult: ...


__all__ = ["SandboxBackend"]
