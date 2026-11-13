"""数据集注册表：加载、校验、许可证与状态闸门。

注册表是数据而不是代码：新增数据集只改 `registry.json` 并实现对应 adapter，
评分与执行主干不随数据集变化。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .models import DatasetEntry, license_is_clear
from .paths import BENCH_ROOT, DEFAULT_REGISTRY, DEFAULT_THRESHOLDS

SCHEMA_VERSION = 1


class RegistryError(ValueError):
    """注册表非法：宁可 fail closed，也不静默跳过数据集。"""


@dataclass(frozen=True)
class Registry:
    entries: tuple[DatasetEntry, ...]
    sha256: str = ""
    path: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)

    def get(self, name: str) -> DatasetEntry:
        for entry in self.entries:
            if entry.name == name:
                return entry
        raise RegistryError(f"未登记的数据集: {name!r}（已登记: {', '.join(self.names)}）")

    def maybe_get(self, name: str) -> DatasetEntry | None:
        try:
            return self.get(name)
        except RegistryError:
            return None

    @property
    def approved(self) -> tuple[DatasetEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_scorable)

    @property
    def primary(self) -> tuple[DatasetEntry, ...]:
        """可进入外部主分的条目：role=primary + approved + 许可证清晰。"""
        return tuple(entry for entry in self.entries if entry.is_primary)

    @property
    def supplementary(self) -> tuple[DatasetEntry, ...]:
        """自建补充分轨条目。"""
        return tuple(entry for entry in self.entries if entry.is_supplementary)

    @property
    def diagnostic(self) -> tuple[DatasetEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_diagnostic)

    def names_for_role(self, role: str | None = None) -> tuple[str, ...]:
        if role is None:
            return self.names
        return tuple(entry.name for entry in self.entries if entry.role == role)


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"无法读取注册表 {path}: {exc}") from exc


def load_registry(path: Path | str | None = None) -> Registry:
    target = Path(path) if path else DEFAULT_REGISTRY
    raw_bytes = _read_bytes(target)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"注册表不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegistryError("注册表必须是 JSON 对象")
    if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        raise RegistryError(f"不支持的 schema_version: {payload.get('schema_version')!r}")
    rows = payload.get("datasets")
    if not isinstance(rows, list) or not rows:
        raise RegistryError("注册表 datasets 必须是非空数组")

    # 延迟导入，避免 registry <-> adapters 循环。
    from .adapters import ADAPTERS

    entries: list[DatasetEntry] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RegistryError("datasets 的每一项必须是对象")
        try:
            entry = DatasetEntry(**row)
        except TypeError as exc:
            raise RegistryError(f"数据集字段非法: {exc}") from exc
        except ValueError as exc:
            raise RegistryError(str(exc)) from exc
        if entry.name in seen:
            raise RegistryError(f"重复数据集名: {entry.name}")
        seen.add(entry.name)
        if entry.adapter not in ADAPTERS:
            raise RegistryError(
                f"{entry.name}: 未知 adapter {entry.adapter!r}（可用: {', '.join(sorted(ADAPTERS))}）"
            )
        if entry.status == "approved" and not license_is_clear(entry.license):
            raise RegistryError(
                f"{entry.name}: 许可证不明确的数据集不能是 approved，请先核验或降为 candidate"
            )
        if entry.kind == "core" and entry.status != "approved":
            raise RegistryError(f"{entry.name}: core 数据集必须是 approved")
        if entry.kind == "core" and entry.role != "supplementary":
            raise RegistryError(
                f"{entry.name}: 自建 core 数据集只能标 supplementary，不得进入主分"
            )
        if entry.role == "primary" and entry.status == "approved" and not license_is_clear(
            entry.license
        ):
            raise RegistryError(f"{entry.name}: 主分数据集必须有清晰许可证")
        entries.append(entry)
    return Registry(entries=tuple(entries), sha256=digest, path=target.as_posix())


def registry_entries(path: Path | str | None = None) -> list[DatasetEntry]:
    return list(load_registry(path).entries)


__all__ = [
    "BENCH_ROOT", "DEFAULT_REGISTRY", "DEFAULT_THRESHOLDS", "SCHEMA_VERSION",
    "RegistryError", "Registry", "load_registry", "registry_entries",
]
