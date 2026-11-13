"""评测夹具加载与校验。

夹具是数据而不是代码：每个 case 描述 base/head 的覆盖文件与期望的拦截结果，
这样一次漏检/误拦可以直接沉淀成一条 case，而不用改评测脚本。

两层契约：
- `cases.json`（旧）：只做确定性四件套，字段 `expected.block/rules`；
- `cases.core.jsonl`（新）：分层用例，额外带 `layer/dataset/expect.semantic/tier/probe`。
  新契约的 `expect.rules` 表示"应当出现的 rule_id"，不要求这些 rule 都是 blocking。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CASES = Path(__file__).resolve().parent / "fixtures" / "cases.json"
DEFAULT_SUITE = Path(__file__).resolve().parent / "datasets" / "core" / "cases.core.jsonl"
# synthetic：手写场景；incident：合并后归因沉淀；benchmark：外部数据集抽样。
VALID_SOURCES = ("synthetic", "incident", "benchmark")
# 分层：确定性规则、语义召回、门控/审批、安全不变量、全链路、外部切片。
LAYERS = ("det", "semantic", "gate", "security", "e2e", "external")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def validate_fixture(case: dict) -> dict:
    if not isinstance(case, dict):
        raise ValueError("fixture 必须是对象")
    name = str(case.get("name", "")).strip()
    if not name:
        raise ValueError("fixture 缺少 name")
    expected = case.get("expected")
    if not isinstance(expected, dict) or "block" not in expected:
        raise ValueError(f"{name}: expected 必须包含 block")
    source = str(case.get("source", "synthetic"))
    if source not in VALID_SOURCES:
        raise ValueError(f"{name}: 未知 source {source}")
    for key in ("base", "head"):
        value = case.get(key, {})
        if not isinstance(value, dict):
            raise ValueError(f"{name}: {key} 必须是 path -> content 的对象")
        for path in value:
            if not isinstance(path, str) or path.startswith("/") or ".." in path.split("/"):
                raise ValueError(f"{name}: 非法路径 {path!r}")
    rules = expected.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError(f"{name}: expected.rules 必须是列表")
    return {
        "name": name,
        "source": source,
        "profile": str(case.get("profile", "change")),
        "base": dict(case.get("base") or {}),
        "head": dict(case.get("head") or {}),
        "expected": {"block": bool(expected["block"]), "rules": [str(r) for r in rules]},
    }


def load_cases(path: Path | str | None = None) -> list[dict]:
    target = Path(path) if path else DEFAULT_CASES
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("cases 文件必须是数组")
    return [validate_fixture(case) for case in data]


@dataclass(frozen=True)
class CaseSpec:
    """分层用例契约：执行层只认这个对象，评分卡只认 `CaseResult`。"""

    id: str
    layer: str
    source: str = "synthetic"
    dataset: str = ""
    profile: str = "change"
    base: dict[str, str] = field(default_factory=dict)
    head: dict[str, str | None] = field(default_factory=dict)
    expect_block: bool = False
    expect_rules: tuple[str, ...] = ()
    expect_semantic: bool = False
    expect_tier: str = ""
    expect_approvals: int | None = None
    weight: float = 1.0
    tags: tuple[str, ...] = ()
    notes: str = ""
    probe: str = ""
    probe_args: dict = field(default_factory=dict)

    @property
    def expected_outcome(self) -> str:
        return "block" if self.expect_block else "merge"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "layer": self.layer,
            "source": self.source,
            "dataset": self.dataset,
            "profile": self.profile,
            "base": dict(self.base),
            "head": dict(self.head),
            "expect": {
                "block": self.expect_block,
                "rules": list(self.expect_rules),
                "semantic": self.expect_semantic,
                "tier": self.expect_tier,
                "approvals": self.expect_approvals,
            },
            "weight": self.weight,
            "tags": list(self.tags),
            "notes": self.notes,
            "probe": self.probe,
            "probe_args": dict(self.probe_args),
        }


def _check_tree(raw: object, *, name: str, key: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{name}: {key} 必须是 path -> content 的对象")
    result: dict[str, str | None] = {}
    for path, content in raw.items():
        if not isinstance(path, str) or path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"{name}: 非法路径 {path!r}")
        if content is not None and not isinstance(content, str):
            raise ValueError(f"{name}: {path} 内容必须是字符串或 null")
        result[path] = content
    return result


def validate_case(case: dict) -> CaseSpec:
    """校验并归一化一条分层用例；失败即 raise（夹具错误不应静默跳过）。"""
    if not isinstance(case, dict):
        raise ValueError("case 必须是对象")
    case_id = str(case.get("id", "")).strip()
    if not case_id:
        raise ValueError("case 缺少 id")
    if not _ID_RE.match(case_id):
        raise ValueError(f"非法 id: {case_id!r}（只允许小写字母/数字/._-）")
    layer = str(case.get("layer", "")).strip()
    if layer not in LAYERS:
        raise ValueError(f"{case_id}: 未知 layer {layer!r}")
    source = str(case.get("source", "synthetic"))
    if source not in VALID_SOURCES:
        raise ValueError(f"{case_id}: 未知 source {source!r}")
    expect = case.get("expect")
    if not isinstance(expect, dict) or "block" not in expect:
        raise ValueError(f"{case_id}: expect 必须包含 block")
    rules = expect.get("rules") or []
    if not isinstance(rules, list) or any(not isinstance(item, str) for item in rules):
        raise ValueError(f"{case_id}: expect.rules 必须是字符串列表")
    tier = str(expect.get("tier", "") or "")
    if tier and tier not in ("T0", "T1", "T2"):
        raise ValueError(f"{case_id}: 非法 tier {tier!r}")
    approvals = expect.get("approvals")
    if approvals is not None and (not isinstance(approvals, int) or approvals < 0):
        raise ValueError(f"{case_id}: expect.approvals 必须是非负整数")
    weight = float(case.get("weight", 1.0))
    if weight <= 0:
        raise ValueError(f"{case_id}: weight 必须为正")
    probe_args = case.get("probe_args") or {}
    if not isinstance(probe_args, dict):
        raise ValueError(f"{case_id}: probe_args 必须是对象")
    base = _check_tree(case.get("base"), name=case_id, key="base")
    head = _check_tree(case.get("head"), name=case_id, key="head")
    return CaseSpec(
        id=case_id,
        layer=layer,
        source=source,
        dataset=str(case.get("dataset", "")),
        profile=str(case.get("profile", "change")),
        base={k: str(v) for k, v in base.items()},
        head=dict(head),
        expect_block=bool(expect["block"]),
        expect_rules=tuple(str(item) for item in rules),
        expect_semantic=bool(expect.get("semantic", False)),
        expect_tier=tier,
        expect_approvals=approvals,
        weight=weight,
        tags=tuple(str(item) for item in (case.get("tags") or [])),
        notes=str(case.get("notes", "")),
        probe=str(case.get("probe", "")),
        probe_args=probe_args,
    )


def load_suite(path: Path | str | None = None) -> list[CaseSpec]:
    """加载 .jsonl（每行一个）或 .json（数组）格式的分层用例集。"""
    target = Path(path) if path else DEFAULT_SUITE
    text = target.read_text(encoding="utf-8")
    if target.suffix == ".jsonl":
        raw = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("用例集必须是数组或 JSONL")
    cases = [validate_case(item) for item in raw]
    ids = [item.id for item in cases]
    duplicated = sorted({item for item in ids if ids.count(item) > 1})
    if duplicated:
        raise ValueError(f"重复 case id: {duplicated[:5]}")
    return cases


def write_suite(cases: list[CaseSpec], path: Path | str) -> Path:
    """按 id 排序写出 JSONL；生成物可 diff、可复现。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(cases, key=lambda item: (item.layer, item.id))
    target.write_text(
        "\n".join(json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=False) for item in ordered)
        + "\n",
        encoding="utf-8",
    )
    return target


__all__ = [
    "DEFAULT_CASES", "DEFAULT_SUITE", "VALID_SOURCES", "LAYERS",
    "validate_fixture", "load_cases", "CaseSpec", "validate_case", "load_suite", "write_suite",
]
