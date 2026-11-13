"""评测平台契约：TaskSpec / Observation / ScoreRecord / DatasetEntry / RunMeta。

三层解耦：
- 适配层只产出 `TaskSpec`；
- 执行层只产出 `Observation`；
- 评分层只消费 `ScoreRecord`（以及 registry 的权重）。
任何一层都不需要知道另外两层的实现细节。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator, model_validator

from .metrics import normalize_severity

TASK_SCHEMA_VERSION = 2
CORE_LAYERS = ("probe", "reviewer", "det", "semantic", "gate", "core_security", "e2e", "external")
EXTERNAL_LAYERS = ("fix", "review", "security")
SOURCE_LAYERS = CORE_LAYERS + EXTERNAL_LAYERS
TASK_PROFILES = ("change", "bugfix", "")
NORMALIZED_SEVERITIES = ("high", "medium", "low")

CAPABILITIES = ("F1", "F2", "F3", "F4", "F5", "F6")
CAPABILITY_NAMES = {
    "F1": "PR 理解与变更分析",
    "F2": "缺陷检测与定位",
    "F3": "代码审查质量",
    "F4": "自动代码修复",
    "F5": "自动测试与回归",
    "F6": "Agent 工作流与工具",
}
# 功能层权重：F1 是前置诊断维，不占综合分权重，不达标时不输出主分。
CAPABILITY_WEIGHTS = {"F2": 0.25, "F3": 0.25, "F4": 0.25, "F5": 0.15, "F6": 0.10}
DIAGNOSTIC_CAPABILITIES = ("F1",)
# 三轨口径：主分只接受外部已核准数据集；自建集只做补充分；诊断独立报告。
ROLES = ("primary", "supplementary", "diagnostic")
DEFAULT_ROLE = "supplementary"
# 主分要求维度：F1 是门禁维（不占权重），F6 单独作为 S_integration。
PRIMARY_REQUIRED = ("F1", "F2", "F3", "F4", "F5")
PRIMARY_WEIGHTED = ("F2", "F3", "F4", "F5")
INTEGRATION = ("F6",)
SUPPLEMENTARY_WEIGHTED = ("F2", "F3", "F5")
# 外部主分要能输出时的目标样本量（不足即 small_sample）。
COVERAGE_TARGETS = {"F1": 10, "F2": 20, "F3": 20, "F4": 20, "F5": 20, "F6": 10}
OBSERVATION_STATUSES = ("ok", "skipped", "error")
DATASET_STATUSES = ("candidate", "approved", "quarantined", "rejected")
QUALITY_GRADES = ("A", "B", "C")
GRADE_WEIGHTS = {"A": 1.0, "B": 0.7, "C": 0.0}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_UNKNOWN_LICENSES = {"", "unknown", "n/a", "none", "unlicensed"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def license_is_clear(license_text: str) -> bool:
    return str(license_text or "").strip().lower() not in _UNKNOWN_LICENSES


@dataclass(frozen=True)
class DatasetEntry:
    """数据集登记项。capabilities 的值是该数据集对某能力的覆盖权重（0..1）。

    `role` 决定该数据集进入哪条轨道：`primary`（外部主分）、`supplementary`
    （自建补充分）、`diagnostic`（诊断）。缺省 `supplementary`，避免旧 registry
    因为新增字段突然进入主分。
    """

    name: str
    adapter: str
    kind: str
    license: str
    status: str
    quality_grade: str
    capabilities: dict[str, float] = field(default_factory=dict)
    primary_metrics: dict[str, str] = field(default_factory=dict)
    snapshot_required: bool = False
    snapshot_root: str = "snapshots"
    image: str = ""
    test_command: str = "python -m pytest -q"
    instances_file: str = ""
    manifest_file: str = ""
    fixtures: str = ""
    repo_url_template: str = "https://github.com/{repo}.git"
    size: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    verification: str = "pending"
    role: str = DEFAULT_ROLE

    def __post_init__(self) -> None:
        _require(bool(_NAME_RE.match(self.name or "")), f"非法数据集名: {self.name!r}")
        _require(bool(self.adapter), f"{self.name}: 缺少 adapter")
        _require(self.kind in ("core", "external"), f"{self.name}: kind 必须是 core/external")
        _require(self.role in ROLES, f"{self.name}: 未知 role {self.role!r}")
        _require(self.status in DATASET_STATUSES, f"{self.name}: 未知 status {self.status!r}")
        _require(self.quality_grade in QUALITY_GRADES,
                 f"{self.name}: 未知 quality_grade {self.quality_grade!r}")
        _require(isinstance(self.capabilities, dict), f"{self.name}: capabilities 必须是对象")
        for cap, weight in self.capabilities.items():
            _require(cap in CAPABILITIES, f"{self.name}: 未知能力 {cap!r}")
            _require(0.0 <= float(weight) <= 1.0,
                     f"{self.name}: {cap} 覆盖权重必须在 0..1")
        for cap, metric in self.primary_metrics.items():
            _require(cap in CAPABILITIES, f"{self.name}: 主指标引用了未知能力 {cap!r}")
            _require(bool(str(metric).strip()), f"{self.name}: {cap} 主指标为空")

    @property
    def grade_weight(self) -> float:
        return GRADE_WEIGHTS[self.quality_grade]

    @property
    def is_scorable(self) -> bool:
        """只有已核准、非 C 级、许可证清晰的数据集进入功能层分母。"""
        return (
            self.status == "approved"
            and self.grade_weight > 0
            and (license_is_clear(self.license) or self.kind == "core")
        )

    @property
    def is_primary(self) -> bool:
        """外部主分：role=primary + approved + 许可证清晰 + 非 C 级。"""
        return (
            self.role == "primary"
            and self.status == "approved"
            and self.grade_weight > 0
            and license_is_clear(self.license)
        )

    @property
    def is_supplementary(self) -> bool:
        """自建补充分：role=supplementary 且可评分。"""
        return self.role == "supplementary" and self.is_scorable

    @property
    def is_diagnostic(self) -> bool:
        """诊断轨：F1 探针与 F6 工作流，不进入主分。"""
        return self.role == "diagnostic"

    def coverage(self, capability: str) -> float:
        return float(self.capabilities.get(capability, 0.0)) if self.is_scorable else 0.0

    def primary_metric(self, capability: str) -> str:
        return str(self.primary_metrics.get(capability, ""))

    def to_dict(self) -> dict:
        return asdict(self)


def _empty_if_none(value: Any) -> Any:
    return "" if value is None else value


def _zero_if_none(value: Any) -> Any:
    return 0 if value is None else value


def _tuple_if_none(value: Any) -> Any:
    return () if value is None else value


_OptStr = Annotated[str, BeforeValidator(_empty_if_none)]
_OptInt = Annotated[int, BeforeValidator(_zero_if_none)]
_OptStrTuple = Annotated[tuple[str, ...], BeforeValidator(_tuple_if_none)]


class _StrictModel(BaseModel):
    """TaskSpec 子模型基类：不可变且禁止未知字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskIdentity(_StrictModel):
    """任务仓库与提交标识。"""

    repo: _OptStr = ""
    base_commit: _OptStr = ""
    head_commit: _OptStr = ""
    pr_or_issue: _OptStr = ""
    instance_id: _OptStr = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        data = dict(raw)
        legacy_commit = data.pop("commit", None)
        if not str(data.get("base_commit") or "").strip() and legacy_commit not in (None, ""):
            data["base_commit"] = legacy_commit
        for key in ("repo", "base_commit", "head_commit", "pr_or_issue", "instance_id"):
            if key in data and isinstance(data[key], str):
                data[key] = data[key].strip()
        instance_id = "" if data.get("instance_id") is None else str(data.get("instance_id")).strip()
        if not (data.get("pr_or_issue") or "").strip() and instance_id:
            data["pr_or_issue"] = f"instance:{instance_id}"
        return data


class TaskEnv(_StrictModel):
    """任务运行环境元数据。"""

    image: _OptStr = ""
    language: _OptStr = ""
    version: _OptStr = ""
    python_version: _OptStr = ""
    environment_setup_commit: _OptStr = ""
    prepare_commands: _OptStrTuple = ()
    test_command: str = "python -m pytest -q"

    @field_validator("test_command", mode="before")
    @classmethod
    def _default_test_command(cls, value: Any) -> str:
        if value is None or (isinstance(value, str) and not value.strip()):
            return "python -m pytest -q"
        return value


class GoldFinding(_StrictModel):
    """代码审查标准标注条目。"""

    path: _OptStr
    line: _OptInt = 0
    severity: Literal["high", "medium", "low"] = "medium"
    category: _OptStr = ""

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("GoldFinding.path 不能为空")
        return cleaned

    @field_validator("line")
    @classmethod
    def _check_line(cls, value: int) -> int:
        if value < 0:
            raise ValueError("GoldFinding.line 不能为负数")
        return value

    @field_validator("severity", mode="before")
    @classmethod
    def _align_severity(cls, value: Any) -> str:
        if value is None or str(value).strip() == "":
            return "medium"
        return normalize_severity(str(value))


class FixInput(_StrictModel):
    """修复类外部任务输入（SWE-bench / BugsInPy / Multi-SWE-bench）。"""

    problem_statement: _OptStr = ""
    patch: _OptStr = ""
    test_patch: _OptStr = ""
    title: _OptStr = ""
    diff: _OptStr = ""
    profile: Literal["change", "bugfix", ""] = "bugfix"


class FixExpected(_StrictModel):
    """修复类任务预期测试通过集。"""

    fail_to_pass: _OptStrTuple = ()
    pass_to_pass: _OptStrTuple = ()


class ReviewInput(_StrictModel):
    """PR 审查任务输入（ReviewBench / SWE-CARE / CodeFuse-CR-Bench）。"""

    diff: _OptStr
    title: _OptStr = ""
    problem_statement: _OptStr = ""
    profile: Literal["change", "bugfix", ""] = "change"


class ReviewExpected(_StrictModel):
    """PR 审查任务金标 findings。"""

    gold_findings: tuple[GoldFinding, ...] = ()


class SecurityInput(_StrictModel):
    """外部漏洞检测输入（Devign / PrimeVul 等）。"""

    code: _OptStr
    fixed_code: _OptStr = ""
    diff: _OptStr = ""
    path: _OptStr = "sample.py"
    language: _OptStr = "python"

    @field_validator("path")
    @classmethod
    def _default_path(cls, value: str) -> str:
        return value.strip() or "sample.py"


class SecurityExpected(_StrictModel):
    """外部漏洞检测预期标签与严重度。"""

    vulnerable: bool = False
    severity: Literal["high", "medium", "low"] = "high"
    cwe: _OptStr = ""


class ProbeInput(_StrictModel):
    """F1 diff 解析探针输入。"""

    probe: _OptStr
    diff: _OptStr = ""

    @field_validator("probe")
    @classmethod
    def _non_empty_probe(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("ProbeInput.probe 不能为空")
        return cleaned


class ProbeExpected(_StrictModel):
    """F1 diff 解析探针预期路径集合。"""

    paths: _OptStrTuple = ()


class ReviewerReplayInput(_StrictModel):
    """F3 reviewer 离线回放输入。"""

    case_name: _OptStr
    response: dict[str, Any]

    @field_validator("case_name")
    @classmethod
    def _non_empty_case(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("ReviewerReplayInput.case_name 不能为空")
        return cleaned


class ReviewerReplayExpected(_StrictModel):
    """F3 reviewer 离线回放预期阻断项。"""

    blocking: _OptStrTuple = ()


class CoreCaseInput(_StrictModel):
    """内部核心回归用例输入。"""

    case: dict[str, Any]

    @field_validator("case")
    @classmethod
    def _check_case_id(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not str(value.get("id") or "").strip():
            raise ValueError("CoreCaseInput.case 必须含非空 id")
        return value


class CoreCaseExpected(_StrictModel):
    """内部核心回归用例预期结果。"""

    block: bool = False
    rules: _OptStrTuple = ()
    tier: _OptStr = ""
    approvals: int | None = None
    semantic: bool = False


TaskInput = (
    FixInput | ReviewInput | SecurityInput | ProbeInput | ReviewerReplayInput | CoreCaseInput
)
TaskExpected = (
    FixExpected | ReviewExpected | SecurityExpected | ProbeExpected | ReviewerReplayExpected | CoreCaseExpected
)

LAYER_SCHEMAS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "probe": (ProbeInput, ProbeExpected),
    "reviewer": (ReviewerReplayInput, ReviewerReplayExpected),
    "det": (CoreCaseInput, CoreCaseExpected),
    "semantic": (CoreCaseInput, CoreCaseExpected),
    "gate": (CoreCaseInput, CoreCaseExpected),
    "core_security": (CoreCaseInput, CoreCaseExpected),
    "e2e": (CoreCaseInput, CoreCaseExpected),
    "external": (CoreCaseInput, CoreCaseExpected),
    "fix": (FixInput, FixExpected),
    "review": (ReviewInput, ReviewExpected),
    "security": (SecurityInput, SecurityExpected),
}


def _coerce_model(value: Any, model_cls: type[BaseModel], task_id: str, bucket: str) -> BaseModel:
    if isinstance(value, model_cls):
        return value
    if isinstance(value, BaseModel):
        raise ValueError(
            f"{task_id}: {bucket} 类型 {type(value).__name__} 与预期 {model_cls.__name__} 不匹配"
        )
    if not isinstance(value, dict):
        raise ValueError(f"{task_id}: {bucket} 必须是对象或 {model_cls.__name__}")
    return model_cls.model_validate(value)


def _fill_missing(target: dict[str, Any], source: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        cur = target.get(key)
        if (key not in target or cur in (None, "", [])) and source.get(key) not in (None, ""):
            target[key] = source[key]


def _migrate_v1_task(raw: dict) -> dict:
    """将 v1 格式 TaskSpec 字典迁移为 v2 契约字典。"""
    data = dict(raw)
    layer = str(data.get("source_layer") or "").strip()
    if data.get("dataset") == "core" and layer == "security":
        layer = "core_security"
    data["source_layer"] = layer

    identity = dict(data.get("identity") or {})
    inp = dict(data.get("input") or {})
    exp = dict(data.get("expected") or {})
    env = dict(data.get("env") or {})
    env.pop("window", None)
    env.pop("snapshot_root", None)

    instance = inp.pop("instance", None)
    inst = instance if isinstance(instance, dict) else {}

    if layer == "fix":
        _fill_missing(inp, inst, ("problem_statement", "title", "patch", "test_patch", "diff"))
        _fill_missing(
            env, inst,
            (
                "version", "python_version", "environment_setup_commit",
                "image", "test_command", "prepare_commands", "language",
            ),
        )
        _fill_missing(identity, inst, ("instance_id", "repo", "base_commit", "head_commit"))
        _fill_missing(exp, inst, ("fail_to_pass", "pass_to_pass"))
    elif layer == "review":
        _fill_missing(inp, inst, ("title", "problem_statement", "diff"))
        inp.setdefault("diff", "")
        _fill_missing(identity, inst, ("head_commit", "instance_id", "repo", "base_commit"))
        _fill_missing(exp, inst, ("gold_findings",))
    elif layer == "security":
        _fill_missing(inp, inst, ("code", "fixed_code", "diff", "path", "language"))
        inp.setdefault("code", "")
        if not identity.get("base_commit") and inst.get("commit"):
            identity["base_commit"] = inst["commit"]
        _fill_missing(identity, inst, ("repo", "instance_id", "base_commit"))
        if "vulnerable" not in exp and "vulnerable" in inst:
            exp["vulnerable"] = bool(inst["vulnerable"])
        _fill_missing(exp, inst, ("cwe", "severity"))
        if "severity" in exp:
            exp["severity"] = normalize_severity(str(exp["severity"] or "high"))
    elif layer == "reviewer":
        rev = inp.pop("reviewer", None)
        if isinstance(rev, dict):
            if not inp.get("case_name"):
                inp["case_name"] = str(
                    rev.get("case_name") or str(data.get("task_id") or "").rsplit(".", 1)[-1]
                )
            if "response" not in inp:
                inp["response"] = dict(rev.get("response") or {})
            if "blocking" not in exp and "expected_blocking" in rev:
                exp["blocking"] = list(rev.get("expected_blocking") or [])

    data["identity"] = identity
    data["input"] = inp
    data["expected"] = exp
    data["env"] = env
    data["schema_version"] = TASK_SCHEMA_VERSION
    return data


@dataclass(frozen=True)
class TaskSpec:
    """统一任务。四桶载荷经 Pydantic v2 强校验，执行层按 `source_layer` 分派。"""

    task_id: str
    dataset: str
    capabilities: tuple[str, ...]
    identity: TaskIdentity
    input: TaskInput
    expected: TaskExpected
    env: TaskEnv
    license: str
    quality_grade: str
    source_layer: str = ""
    sample_uid: str = ""
    role: str = DEFAULT_ROLE
    schema_version: int = TASK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require(bool(str(self.task_id).strip()), "TaskSpec.task_id 不能为空")
        _require(bool(str(self.dataset).strip()), "TaskSpec.dataset 不能为空")
        caps = tuple(self.capabilities)
        _require(bool(caps), f"{self.task_id}: capabilities 不能为空")
        _require(len(set(caps)) == len(caps), f"{self.task_id}: capabilities 有重复")
        for cap in caps:
            _require(cap in CAPABILITIES, f"{self.task_id}: 未知能力 {cap!r}")
        object.__setattr__(self, "capabilities", caps)
        _require(self.role in ROLES, f"{self.task_id}: 未知 role {self.role!r}")
        _require(self.quality_grade in QUALITY_GRADES,
                 f"{self.task_id}: 未知 quality_grade {self.quality_grade!r}")
        layer = str(self.source_layer or "").strip()
        _require(layer in SOURCE_LAYERS, f"{self.task_id}: 未知或缺失 source_layer {self.source_layer!r}")
        object.__setattr__(self, "source_layer", layer)
        version = int(self.schema_version)
        _require(version >= 1, f"{self.task_id}: schema_version 必须 >= 1")
        object.__setattr__(self, "schema_version", version)

        input_cls, expected_cls = LAYER_SCHEMAS[layer]
        object.__setattr__(self, "identity", _coerce_model(self.identity, TaskIdentity, self.task_id, "identity"))
        object.__setattr__(self, "env", _coerce_model(self.env, TaskEnv, self.task_id, "env"))
        object.__setattr__(self, "input", _coerce_model(self.input, input_cls, self.task_id, "input"))
        object.__setattr__(self, "expected", _coerce_model(self.expected, expected_cls, self.task_id, "expected"))

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "dataset": self.dataset,
            "capabilities": list(self.capabilities),
            "identity": self.identity.model_dump(mode="json"),
            "input": self.input.model_dump(mode="json"),
            "expected": self.expected.model_dump(mode="json"),
            "env": self.env.model_dump(mode="json"),
            "license": self.license,
            "quality_grade": self.quality_grade,
            "source_layer": self.source_layer,
            "sample_uid": self.sample_uid,
            "role": self.role,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "TaskSpec":
        version = int(raw.get("schema_version") or 1)
        data = _migrate_v1_task(raw) if version < TASK_SCHEMA_VERSION else dict(raw)
        data["capabilities"] = tuple(data.get("capabilities") or ())
        return cls(**data)


@dataclass(frozen=True)
class Observation:
    task_id: str
    dataset: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    cost_cny: float = 0.0
    wall_ms: int = 0
    sample_uid: str = ""
    skip_reason: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        _require(self.status in OBSERVATION_STATUSES,
                 f"{self.task_id}: 未知 observation status {self.status!r}")
        _require(isinstance(self.payload, dict), f"{self.task_id}: payload 必须是对象")
        _require(self.cost_cny >= 0, f"{self.task_id}: cost_cny 不能为负")
        _require(self.wall_ms >= 0, f"{self.task_id}: wall_ms 不能为负")
        for cap in self.capabilities:
            _require(cap in CAPABILITIES, f"{self.task_id}: 未知能力 {cap!r}")

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["capabilities"] = list(self.capabilities)
        return payload

    @classmethod
    def from_dict(cls, raw: dict) -> "Observation":
        data = dict(raw)
        data["capabilities"] = tuple(data.get("capabilities") or ())
        return cls(**data)


@dataclass(frozen=True)
class ScoreRecord:
    """一条可计分记录。`value` 已归一化到 0..1，聚合层只做加权。"""

    task_id: str
    dataset: str
    capability: str
    metric: str
    numerator: float
    denominator: float
    value: float
    sample_uid: str = ""
    sample_size: int = 1
    interval: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        _require(self.capability in CAPABILITIES,
                 f"{self.task_id}: 未知能力 {self.capability!r}")
        _require(bool(str(self.metric).strip()), f"{self.task_id}: metric 不能为空")
        _require(self.numerator >= 0, f"{self.task_id}: numerator 不能为负")
        _require(self.denominator >= 0, f"{self.task_id}: denominator 不能为负")
        _require(0.0 <= float(self.value) <= 1.0,
                 f"{self.task_id}: value 必须在 0..1（当前 {self.value}）")
        _require(self.sample_size >= 0, f"{self.task_id}: sample_size 不能为负")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunMeta:
    run_id: str
    started_at: str
    git_sha: str = ""
    registry_sha256: str = ""
    models: dict[str, str] = field(default_factory=dict)
    repeat: int = 1
    offline: bool = True
    budget_cny: float = 0.0
    notes: str = ""
    profile: str = ""
    patch_source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


__all__ = [
    "TASK_SCHEMA_VERSION", "CORE_LAYERS", "EXTERNAL_LAYERS", "SOURCE_LAYERS",
    "TASK_PROFILES", "NORMALIZED_SEVERITIES", "LAYER_SCHEMAS",
    "CAPABILITIES", "CAPABILITY_NAMES", "CAPABILITY_WEIGHTS", "DIAGNOSTIC_CAPABILITIES",
    "ROLES", "DEFAULT_ROLE", "PRIMARY_REQUIRED", "PRIMARY_WEIGHTED", "INTEGRATION",
    "SUPPLEMENTARY_WEIGHTED", "COVERAGE_TARGETS",
    "OBSERVATION_STATUSES", "DATASET_STATUSES",
    "QUALITY_GRADES", "GRADE_WEIGHTS", "license_is_clear",
    "TaskIdentity", "TaskEnv", "GoldFinding",
    "FixInput", "FixExpected", "ReviewInput", "ReviewExpected",
    "SecurityInput", "SecurityExpected", "ProbeInput", "ProbeExpected",
    "ReviewerReplayInput", "ReviewerReplayExpected", "CoreCaseInput", "CoreCaseExpected",
    "TaskInput", "TaskExpected",
    "DatasetEntry", "TaskSpec", "Observation", "ScoreRecord", "RunMeta",
]
