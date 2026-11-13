"""适配器注册表：新增数据集只实现 `load_instances / to_tasks / aggregate`。"""
from __future__ import annotations

from .bugsinpy import BugsInPyAdapter
from .core import CoreAdapter
from .reviewbench import ReviewBenchAdapter
from .security import SecurityAdapter
from .swebench import SwebenchAdapter

ADAPTERS = {
    "core": CoreAdapter(),
    "swebench": SwebenchAdapter(),
    "bugsinpy": BugsInPyAdapter(),
    "reviewbench": ReviewBenchAdapter(),
    "security": SecurityAdapter(),
}


def get_adapter(name: str):
    try:
        return ADAPTERS[name]
    except KeyError as exc:
        raise ValueError(
            f"未知 adapter {name!r}（可用: {', '.join(sorted(ADAPTERS))}）"
        ) from exc


__all__ = [
    "ADAPTERS", "get_adapter",
    "CoreAdapter", "SwebenchAdapter", "BugsInPyAdapter",
    "ReviewBenchAdapter", "SecurityAdapter",
]
