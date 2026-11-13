# TaskSpec 契约规范化与上下文对齐实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `eval/bench` 的 `TaskSpec` 建立强类型 Pydantic v2 子模型契约、补齐跨数据集关键上下文字段、统一基于 `source_layer` 的执行路由，并实现 v1 → v2 产物自动迁移。

**Architecture:** 保留 `TaskSpec` 外层 `@dataclass(frozen=True)` 以与同层数据类及 `dataclasses.replace` 兼容，将其内部四桶（`identity`、`input`、`expected`、`env`）约束为 `ConfigDict(frozen=True, extra="forbid")` 的 Pydantic v2 模型，并按 `source_layer` 显式判别 `input` / `expected` 结构。`TaskSpec.from_dict()` 自动识别并无损迁移 v1 格式（`schema_version < 2`、`input["instance"]` 透传、core `"security"` 层重名、`env` 冗余字段）。

**Tech Stack:** Python 3.12, Pydantic v2 (`pydantic>=2,<3`), `dataclasses`, `difflib`, `pytest` (`D:\CodeGuard\.venv\Scripts\python.exe -m pytest --basetemp=.tmp/pytest`)

## Global Constraints

- 所有工作仅在工作树 `D:\CodeGuard\.worktrees\feat-taskspec-contract`（分支 `feat/taskspec-contract`）内进行，严禁修改主工作区 `D:\CodeGuard`。
- Windows 下运行 `pytest` 必须使用 `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest` 避免默认临时目录权限错误。
- 所有 `TaskSpec` 强类型子模型使用 Pydantic v2 `BaseModel`，配置 `ConfigDict(frozen=True, extra="forbid")`。
- 常量命名：`TASK_SCHEMA_VERSION = 2`（不修改 `registry.py` 中的 `SCHEMA_VERSION = 1`）、`CORE_LAYERS = ("probe", "reviewer", "det", "semantic", "gate", "core_security", "e2e", "external")`、`EXTERNAL_LAYERS = ("fix", "review", "security")`、`SOURCE_LAYERS = CORE_LAYERS + EXTERNAL_LAYERS`。
- `TaskSpec` 不再允许 `source_layer = ""`，所有跨模块代码不得依赖 `input["instance"]` 或 `env["window"]` / `env["snapshot_root"]`。
- 每个函数必须带完整 Python type hints，优先 guard clauses 提前返回，保留无关已有注释与文档字符串。

---

### Task 1: `TaskSpec` v2 Pydantic 子模型契约与 v1 → v2 自动迁移

**Files:**
- Modify: `eval/bench/models.py`
- Modify: `eval/bench/__init__.py`
- Test: `tests/test_bench_models.py`

**Interfaces:**
- Consumes: `eval.bench.metrics.normalize_severity`
- Produces:
  - Constants: `TASK_SCHEMA_VERSION`, `CORE_LAYERS`, `EXTERNAL_LAYERS`, `SOURCE_LAYERS`, `TASK_PROFILES`, `NORMALIZED_SEVERITIES`, `LAYER_SCHEMAS`
  - Models: `TaskIdentity`, `TaskEnv`, `GoldFinding`, `FixInput`, `FixExpected`, `ReviewInput`, `ReviewExpected`, `SecurityInput`, `SecurityExpected`, `ProbeInput`, `ProbeExpected`, `ReviewerReplayInput`, `ReviewerReplayExpected`, `CoreCaseInput`, `CoreCaseExpected`
  - Updated `TaskSpec`: `source_layer: str`（必填，属 `SOURCE_LAYERS`）, `schema_version: int = TASK_SCHEMA_VERSION`, `to_dict() -> dict`, `from_dict(raw: dict) -> TaskSpec`

- [ ] **Step 1: 在 `tests/test_bench_models.py` 编写契约校验、不可变性、全层覆盖及 v1 → v2 迁移失败测试**

在 `tests/test_bench_models.py` 顶部导入新增模型与常量，更新现有夹具中缺失 `source_layer` 的 `TaskSpec` 构造以匹配新契约（保持验证非法 `capabilities`、`quality_grade`、`role` 的原意），并添加以下测试用例：

```python
from pydantic import ValidationError

from eval.bench.models import (
    CORE_LAYERS,
    CORE_LAYERS as _CORE,
    EXTERNAL_LAYERS,
    SOURCE_LAYERS,
    TASK_SCHEMA_VERSION,
    CoreCaseExpected,
    CoreCaseInput,
    FixExpected,
    FixInput,
    GoldFinding,
    ProbeExpected,
    ProbeInput,
    ReviewExpected,
    ReviewerReplayExpected,
    ReviewerReplayInput,
    ReviewInput,
    SecurityExpected,
    SecurityInput,
    TaskEnv,
    TaskIdentity,
    TaskSpec,
)
```

更新已有测试中的 `TaskSpec(...)`（为其补全 `source_layer="core_security"` 或 `"fix"` 与合法 `input` 最小结构，确保这些测试只测各自目标断言），然后新增测试：

```python
def test_task_spec_rejects_empty_or_unknown_source_layer():
    with pytest.raises(ValueError, match="source_layer"):
        TaskSpec(
            task_id="t", dataset="d", capabilities=("F2",),
            identity={"repo": "demo/repo"},
            input={"case": {"id": "t"}},
            expected={"block": True},
            env={}, license="MIT", quality_grade="A",
        )
    with pytest.raises(ValueError, match="source_layer"):
        TaskSpec(
            task_id="t", dataset="d", capabilities=("F2",),
            identity={"repo": "demo/repo"},
            input={"case": {"id": "t"}},
            expected={"block": True},
            env={}, license="MIT", quality_grade="A",
            source_layer="unknown_layer",
        )


def test_task_spec_submodels_are_frozen_and_forbid_extra():
    task = TaskSpec(
        task_id="fix.1", dataset="swebench_verified", capabilities=("F4", "F5"),
        identity={"repo": "django/django", "commit": "a" * 40, "instance_id": "django-1"},
        input={"problem_statement": "bug", "patch": "diff", "profile": "bugfix"},
        expected={"fail_to_pass": ["test_a"]},
        env={"image": "img:1", "version": "4.2", "language": "python", "test_command": ""},
        license="MIT", quality_grade="A", source_layer="fix",
    )
    assert isinstance(task.identity, TaskIdentity)
    assert isinstance(task.input, FixInput)
    assert isinstance(task.expected, FixExpected)
    assert isinstance(task.env, TaskEnv)
    assert task.schema_version == TASK_SCHEMA_VERSION == 2
    assert task.identity.base_commit == "a" * 40
    assert task.identity.pr_or_issue == "instance:django-1"
    assert task.env.test_command == "python -m pytest -q"

    with pytest.raises(ValidationError):
        task.identity.repo = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        task.input.patch = "x"  # type: ignore[misc]

    with pytest.raises(ValueError):
        TaskSpec(
            task_id="fix.2", dataset="swebench_verified", capabilities=("F4",),
            identity={"repo": "a/b"},
            input={"instance": {"raw": 1}, "patch": "p"},
            expected={}, env={}, license="MIT", quality_grade="A", source_layer="fix",
        )
    with pytest.raises(ValueError):
        TaskSpec(
            task_id="rev.1", dataset="reviewbench", capabilities=("F2",),
            identity={"repo": "a/b"},
            input={"diff": ""}, expected={}, env={"window": 3},
            license="MIT", quality_grade="B", source_layer="review",
        )


def test_task_spec_covers_all_source_layers_and_roundtrips():
    samples = [
        ("probe", ("F1",), {"probe": "deleted", "diff": "d"}, {"paths": ["a.py"]}),
        ("reviewer", ("F3",), {"case_name": "c1", "response": {"verdict": "pass"}}, {"blocking": []}),
        ("det", ("F2",), {"case": {"id": "core.det.1"}}, {"block": True, "rules": ["R1"]}),
        ("semantic", ("F2",), {"case": {"id": "core.sem.1"}}, {"block": False, "semantic": True}),
        ("gate", ("F6",), {"case": {"id": "core.gate.1"}}, {"block": True, "approvals": 2}),
        ("core_security", ("F6",), {"case": {"id": "core.sec.1", "layer": "security"}}, {"block": True}),
        ("e2e", ("F5", "F6"), {"case": {"id": "core.e2e.1"}}, {"block": False}),
        ("external", ("F2",), {"case": {"id": "core.ext.1"}}, {"block": True}),
        ("fix", ("F4", "F5"), {"patch": "diff", "problem_statement": "p"}, {"fail_to_pass": ["t1"]}),
        (
            "review", ("F2", "F3"), {"diff": "d", "title": "t"},
            {"gold_findings": [{"path": "a.py", "line": 3, "severity": "critical", "category": "bug"}]},
        ),
        ("security", ("F2", "F3"), {"code": "x = 1", "fixed_code": "x = 2"}, {"vulnerable": True, "cwe": "CWE-78"}),
    ]
    assert tuple(row[0] for row in samples) == SOURCE_LAYERS
    assert set(CORE_LAYERS) | set(EXTERNAL_LAYERS) == set(SOURCE_LAYERS)
    for layer, caps, inp, exp in samples:
        task = TaskSpec(
            task_id=f"t.{layer}", dataset="demo", capabilities=caps,
            identity={"repo": "demo/repo"}, input=inp, expected=exp, env={},
            license="MIT", quality_grade="A", source_layer=layer,
        )
        dumped = task.to_dict()
        assert dumped["schema_version"] == 2
        assert "instance" not in dumped["input"]
        restored = TaskSpec.from_dict(dumped)
        assert restored == task
    review_task = TaskSpec.from_dict({
        "task_id": "t.rev", "dataset": "demo", "capabilities": ["F2"],
        "identity": {"repo": "demo/repo"},
        "input": {"diff": "d"},
        "expected": {"gold_findings": [{"path": "a.py", "line": 1, "severity": "critical"}]},
        "env": {}, "license": "MIT", "quality_grade": "A", "source_layer": "review",
        "schema_version": 2,
    })
    assert review_task.expected.gold_findings[0] == GoldFinding(path="a.py", line=1, severity="high")


def test_task_spec_from_dict_migrates_v1_payloads():
    v1_fix = {
        "task_id": "swe.1", "dataset": "swebench_verified", "capabilities": ["F4", "F5"],
        "identity": {"repo": "django/django", "base_commit": "a" * 40, "pr_or_issue": "instance:1", "instance_id": "1"},
        "input": {
            "instance": {
                "instance_id": "1", "repo": "django/django", "base_commit": "a" * 40,
                "problem_statement": "fix bug", "title": "Fix crash",
                "version": "4.2", "environment_setup_commit": "b" * 40,
                "language": "python", "python_version": "3.11",
            },
            "patch": "diff --git a/a.py b/a.py", "test_patch": "tpatch",
        },
        "expected": {"fail_to_pass": ["test_1"], "pass_to_pass": []},
        "env": {
            "image": "img:1", "test_command": "pytest",
            "snapshot_root": "/tmp/snap", "prepare_commands": ["pip install -e ."],
        },
        "license": "MIT", "quality_grade": "A", "source_layer": "fix",
    }
    fix_task = TaskSpec.from_dict(v1_fix)
    assert fix_task.schema_version == 2
    assert fix_task.input.problem_statement == "fix bug"
    assert fix_task.input.title == "Fix crash"
    assert fix_task.env.version == "4.2"
    assert fix_task.env.environment_setup_commit == "b" * 40
    assert fix_task.env.python_version == "3.11"

    v1_review = {
        "task_id": "rb.1", "dataset": "reviewbench", "capabilities": ["F2", "F3"],
        "identity": {"repo": "org/r", "base_commit": "c" * 40, "pr_or_issue": "pr:9", "instance_id": "9"},
        "input": {
            "instance": {"head_commit": "d" * 40, "problem_statement": "review body", "title": "PR title"},
            "diff": "diff --git a/x.py b/x.py", "title": "Outer title",
        },
        "expected": {"gold_findings": [{"path": "x.py", "line": 4, "severity": "major", "category": "sec"}]},
        "env": {"window": 3},
        "license": "MIT", "quality_grade": "B", "source_layer": "review",
    }
    rev_task = TaskSpec.from_dict(v1_review)
    assert rev_task.identity.head_commit == "d" * 40
    assert rev_task.input.title == "Outer title"
    assert rev_task.input.problem_statement == "review body"
    assert rev_task.expected.gold_findings[0].severity == "high"

    v1_security = {
        "task_id": "devign.1", "dataset": "devign", "capabilities": ["F2", "F3"],
        "identity": {"repo": "qemu", "base_commit": "e" * 40, "pr_or_issue": "instance:1", "instance_id": "1"},
        "input": {"instance": {"code": "system(x);", "fixed_code": "safe(x);"}, "code": "system(x);"},
        "expected": {"vulnerable": True, "severity": "critical", "cwe": "CWE-78"},
        "env": {}, "license": "MIT", "quality_grade": "B", "source_layer": "security",
    }
    sec_task = TaskSpec.from_dict(v1_security)
    assert sec_task.source_layer == "security"
    assert sec_task.input.fixed_code == "safe(x);"
    assert sec_task.expected.severity == "high"

    v1_core_sec = {
        "task_id": "core.sec.1", "dataset": "core", "capabilities": ["F6"],
        "identity": {"repo": "core", "base_commit": "", "pr_or_issue": "sec.1"},
        "input": {"case": {"id": "sec.1", "layer": "security"}},
        "expected": {"block": True, "rules": [], "tier": ""},
        "env": {}, "license": "internal", "quality_grade": "A", "source_layer": "security",
    }
    core_sec = TaskSpec.from_dict(v1_core_sec)
    assert core_sec.source_layer == "core_security"
    assert core_sec.input.case["layer"] == "security"

    v1_core_reviewer = {
        "task_id": "core.reviewer.bad", "dataset": "core", "capabilities": ["F3"],
        "identity": {"repo": "core", "base_commit": "", "pr_or_issue": "core.reviewer.bad"},
        "input": {"reviewer": {"id": "core.reviewer.bad", "case_name": "bad", "response": {"verdict": "fail"}}},
        "expected": {"blocking": ["R1"]},
        "env": {}, "license": "internal", "quality_grade": "A", "source_layer": "reviewer",
    }
    rev_replay = TaskSpec.from_dict(v1_core_reviewer)
    assert isinstance(rev_replay.input, ReviewerReplayInput)
    assert rev_replay.input.case_name == "bad"
    assert rev_replay.input.response == {"verdict": "fail"}
```

- [ ] **Step 2: 运行单测确认按预期失败**

Run: `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest tests/test_bench_models.py`
Expected: FAIL with `ImportError` (`cannot import name 'TASK_SCHEMA_VERSION' from 'eval.bench.models'`).

- [ ] **Step 3: 在 `eval/bench/models.py` 与 `eval/bench/__init__.py` 实现 Pydantic v2 子模型与 `_migrate_v1_task`**

在 `eval/bench/models.py` 中：
1. 引入 `Literal`、`ConfigDict`、`BaseModel`、`field_validator`、`model_validator` 及 `from .metrics import normalize_severity`。
2. 添加常量 `TASK_SCHEMA_VERSION = 2`、`CORE_LAYERS`、`EXTERNAL_LAYERS`、`SOURCE_LAYERS`、`TASK_PROFILES`、`NORMALIZED_SEVERITIES`。
3. 实现强类型不可变子模型：

```python
class _StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskIdentity(_StrictModel):
    repo: str = ""
    base_commit: str = ""
    head_commit: str = ""
    pr_or_issue: str = ""
    instance_id: str = ""

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
            if key in data:
                val = data[key]
                data[key] = "" if val is None else str(val).strip()
        if not data.get("pr_or_issue") and data.get("instance_id"):
            data["pr_or_issue"] = f"instance:{data['instance_id']}"
        return data


class TaskEnv(_StrictModel):
    image: str = ""
    language: str = ""
    version: str = ""
    python_version: str = ""
    environment_setup_commit: str = ""
    prepare_commands: tuple[str, ...] = ()
    test_command: str = "python -m pytest -q"

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        data = dict(raw)
        for key in ("image", "language", "version", "python_version", "environment_setup_commit"):
            if key in data and data[key] is None:
                data[key] = ""
        if "prepare_commands" in data and data["prepare_commands"] is not None:
            data["prepare_commands"] = tuple(str(x) for x in data["prepare_commands"])
        cmd = data.get("test_command")
        if "test_command" in data and (cmd is None or not str(cmd).strip()):
            data["test_command"] = "python -m pytest -q"
        return data


class GoldFinding(_StrictModel):
    path: str
    line: int = 0
    severity: Literal["high", "medium", "low"] = "medium"
    category: str = ""

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        cleaned = str(value).strip()
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
    problem_statement: str = ""
    patch: str = ""
    test_patch: str = ""
    title: str = ""
    diff: str = ""
    profile: Literal["change", "bugfix", ""] = "bugfix"


class FixExpected(_StrictModel):
    fail_to_pass: tuple[str, ...] = ()
    pass_to_pass: tuple[str, ...] = ()


class ReviewInput(_StrictModel):
    diff: str
    title: str = ""
    problem_statement: str = ""
    profile: Literal["change", "bugfix", ""] = "change"


class ReviewExpected(_StrictModel):
    gold_findings: tuple[GoldFinding, ...] = ()


class SecurityInput(_StrictModel):
    code: str
    fixed_code: str = ""
    diff: str = ""
    path: str = "sample.py"
    language: str = "python"

    @field_validator("path")
    @classmethod
    def _default_path(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        return cleaned or "sample.py"


class SecurityExpected(_StrictModel):
    vulnerable: bool = False
    severity: Literal["high", "medium", "low"] = "high"
    cwe: str = ""


class ProbeInput(_StrictModel):
    probe: str
    diff: str = ""

    @field_validator("probe")
    @classmethod
    def _non_empty_probe(cls, value: str) -> str:
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("ProbeInput.probe 不能为空")
        return cleaned


class ProbeExpected(_StrictModel):
    paths: tuple[str, ...] = ()


class ReviewerReplayInput(_StrictModel):
    case_name: str
    response: dict[str, Any]

    @field_validator("case_name")
    @classmethod
    def _non_empty_case(cls, value: str) -> str:
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("ReviewerReplayInput.case_name 不能为空")
        return cleaned


class ReviewerReplayExpected(_StrictModel):
    blocking: tuple[str, ...] = ()


class CoreCaseInput(_StrictModel):
    case: dict[str, Any]

    @field_validator("case")
    @classmethod
    def _check_case_id(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not str(value.get("id") or "").strip():
            raise ValueError("CoreCaseInput.case 必须含非空 id")
        return value


class CoreCaseExpected(_StrictModel):
    block: bool = False
    rules: tuple[str, ...] = ()
    tier: str = ""
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
```

4. 实现校验工具 `_coerce_model(value, model_cls, task_id, bucket)` 和 `_migrate_v1_task(raw: dict) -> dict`：

```python
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
            ("version", "python_version", "environment_setup_commit", "image", "test_command", "prepare_commands", "language"),
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
                inp["case_name"] = str(rev.get("case_name") or str(data.get("task_id") or "").rsplit(".", 1)[-1])
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
```

5. 更新 `TaskSpec`：
   - 字段 `identity: TaskIdentity`、`input: TaskInput`、`expected: TaskExpected`、`env: TaskEnv`、`source_layer: str`（移至默认参数之前或设为必填字段 `source_layer: str`? Wait: `license: str`、`quality_grade: str`、`source_layer: str` 均为必填，`sample_uid: str = ""`、`role: str = DEFAULT_ROLE`、`schema_version: int = TASK_SCHEMA_VERSION` 为带默认值字段）。
   - 注意调整位置参数兼容性：原有签名 `(task_id, dataset, capabilities, identity, input, expected, env, license, quality_grade, source_layer="", sample_uid="", role=DEFAULT_ROLE)`，去掉 `source_layer` 的空默认值或保持位置并设默认 `source_layer: str = ""` 在 `__post_init__` 拒绝 `""`——**保持 `source_layer: str = ""` 在原第10位且在 `__post_init__` 强校验 `self.source_layer in SOURCE_LAYERS`**，这样不破坏任何按位置参数传参的已有调用方，同时确保构造期对空 `source_layer` 抛出明确 `ValueError`！
   - `to_dict(self) -> dict` 使用 `self.identity.model_dump(mode="json")` 等展开子模型，`capabilities` 转为 `list(self.capabilities)`，保留 `schema_version`。
   - `from_dict(cls, raw: dict) -> TaskSpec` 检查 `int(raw.get("schema_version") or 1) < TASK_SCHEMA_VERSION` 时先调用 `_migrate_v1_task(raw)` 再转为 `cls(**data)`。
   - 在 `eval/bench/models.py` `__all__` 与 `eval/bench/__init__.py` 导出所有新常量与模型。

- [ ] **Step 4: 运行测试验证 Task 1 通过**

Run: `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest tests/test_bench_models.py`
Expected: PASS

- [ ] **Step 5: 提交 Task 1**

```bash
git add eval/bench/models.py eval/bench/__init__.py tests/test_bench_models.py
git commit -m "feat(bench): add Pydantic v2 TaskSpec v2 submodels and v1 migration"
```

---

### Task 2: 探针与去重模块（`probes.py`、`dedup.py`）适配

**Files:**
- Modify: `eval/bench/probes.py`
- Modify: `eval/bench/dedup.py`
- Test: `tests/test_bench_dedup.py`

**Interfaces:**
- Consumes: `TaskIdentity`, `ProbeInput`, `ProbeExpected`, `TaskEnv`, `TaskSpec`
- Produces:
  - `sample_uid(identity: TaskIdentity | dict, patch_text: str = "") -> str`
  - `task_patch(task: TaskSpec) -> str`
  - `attach_uids(tasks: list[TaskSpec]) -> list[TaskSpec]`
  - `dedup_tasks(tasks: list[TaskSpec], training_tasks: list[TaskSpec] | None = None) -> tuple[list[TaskSpec], DedupResult]`

- [ ] **Step 1: 在 `tests/test_bench_dedup.py` 更新夹具并新增 v2 模型与 `SecurityInput.diff` 去重失败测试**

在 `tests/test_bench_dedup.py` 中：
1. 将 `_task(...)` 夹具的 `TaskSpec(...)` 更新为合法 `source_layer: str = "fix"` 并传入 `FixInput` 字段（保留原测试去重语义）；
2. 新增测试验证 `sample_uid` 同时兼容 `TaskIdentity` 实例与 `dict`，且 `SecurityInput`（带合成 `diff`）与 `ReviewInput` 均可通过 `task_patch` + `attach_uids` + `dedup_tasks` 正确去重：

```python
from eval.bench.models import TaskIdentity


def test_sample_uid_accepts_task_identity_and_dict():
    uid_dict = sample_uid({"repo": "Org/Repo", "base_commit": "ABC", "pr_or_issue": "pr:1"}, "+x = 1\n")
    uid_model = sample_uid(
        TaskIdentity(repo="Org/Repo", base_commit="ABC", pr_or_issue="pr:1"),
        "+x = 1\n",
    )
    assert uid_dict == uid_model


def test_dedup_works_with_review_and_security_synthetic_diff():
    s1 = TaskSpec(
        task_id="sec.1", dataset="devign", capabilities=("F2",),
        identity={"repo": "r", "base_commit": "a" * 40, "instance_id": "1"},
        input={"code": "old()", "fixed_code": "new()", "diff": "-old()\n+new()\n"},
        expected={"vulnerable": True}, env={}, license="MIT", quality_grade="B",
        source_layer="security",
    )
    s2 = TaskSpec(
        task_id="sec.2", dataset="primevul", capabilities=("F2",),
        identity={"repo": "r", "base_commit": "a" * 40, "instance_id": "1"},
        input={"code": "old()", "fixed_code": "new()", "diff": "-old()\n+new()\n"},
        expected={"vulnerable": True}, env={}, license="MIT", quality_grade="B",
        source_layer="security",
    )
    r1 = TaskSpec(
        task_id="rev.1", dataset="reviewbench", capabilities=("F2", "F3"),
        identity={"repo": "r", "base_commit": "b" * 40, "pr_or_issue": "pr:2"},
        input={"diff": "--- a/x.py\n+++ b/x.py\n+y = 1\n"},
        expected={"gold_findings": []}, env={}, license="MIT", quality_grade="B",
        source_layer="review",
    )
    assert task_patch(s1) == "-old()\n+new()\n"
    assert task_patch(r1).startswith("--- a/x.py")
    kept, result = dedup_tasks([s1, s2, r1])
    assert len(kept) == 2
    assert len(result.within_eval_dropped) == 1
```

- [ ] **Step 2: 运行测试确认按预期失败**

Run: `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest tests/test_bench_dedup.py`
Expected: FAIL with `AttributeError: 'TaskIdentity' object has no attribute 'get'` (或 `task.input` 无 `.get`)。

- [ ] **Step 3: 更新 `eval/bench/dedup.py` 与 `eval/bench/probes.py`**

在 `eval/bench/dedup.py`：
1. 引入 `TaskIdentity` 类型提示及只读 helper `_id_get(identity: TaskIdentity | dict, key: str, default: Any = "") -> Any`：

```python
from .models import TaskIdentity, TaskSpec


def _id_get(identity: TaskIdentity | dict, key: str, default: Any = "") -> Any:
    if isinstance(identity, dict):
        return identity.get(key, default)
    return getattr(identity, key, default)
```

2. `_subject(identity: TaskIdentity | dict) -> str` 与 `sample_uid(identity: TaskIdentity | dict, patch_text: str = "") -> str` 改用 `_id_get`；
3. `task_patch(task: TaskSpec) -> str` 直接读取属性（`isinstance(task.input, dict)` 保留给仅构造极简模拟字典时的兜底）：

```python
def task_patch(task: TaskSpec) -> str:
    payload = task.input
    if isinstance(payload, dict):
        return str(payload.get("patch") or payload.get("diff") or "")
    return str(getattr(payload, "patch", "") or getattr(payload, "diff", "") or "")
```

在 `eval/bench/probes.py` 的 `probe_tasks()`：显式构造 `TaskIdentity`、`ProbeInput`、`ProbeExpected`、`TaskEnv`。

- [ ] **Step 4: 运行测试验证 Task 2 通过**

Run: `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest tests/test_bench_dedup.py tests/test_bench_models.py`
Expected: PASS

- [ ] **Step 5: 提交 Task 2**

```bash
git add eval/bench/probes.py eval/bench/dedup.py tests/test_bench_dedup.py
git commit -m "feat(bench): align dedup and probes with TaskSpec v2 models"
```

---

### Task 3: 适配器层（`base` / `swebench` / `bugsinpy` / `reviewbench` / `security` / `core`）契约与字段补齐

**Files:**
- Modify: `eval/bench/adapters/base.py`
- Modify: `eval/bench/adapters/swebench.py`
- Modify: `eval/bench/adapters/bugsinpy.py`
- Modify: `eval/bench/adapters/reviewbench.py`
- Modify: `eval/bench/adapters/security.py`
- Modify: `eval/bench/adapters/core.py`
- Test: `tests/test_bench_adapters.py`

**Interfaces:**
- Consumes: `TaskSpec`, `TaskIdentity`, `TaskEnv`, `FixInput`, `FixExpected`, `ReviewInput`, `ReviewExpected`, `SecurityInput`, `SecurityExpected`, `ProbeInput`, `ProbeExpected`, `ReviewerReplayInput`, `ReviewerReplayExpected`, `CoreCaseInput`, `CoreCaseExpected`, `GoldFinding`, `CAPABILITIES`
- Produces:
  - `eval.bench.adapters.base.entry_capabilities(entry: DatasetEntry, fallback: tuple[str, ...], *, supported: tuple[str, ...] | None = None) -> tuple[str, ...]`
  - `SwebenchAdapter` / `BugsInPyAdapter` / `ReviewBenchAdapter` / `SecurityAdapter` / `CoreAdapter` 均产出无 `input["instance"]`、上下文完整、能力受 `registry` 约束的 v2 `TaskSpec`。

- [ ] **Step 1: 在 `tests/test_bench_adapters.py` 更新夹具并新增适配器字段对齐与能力派生测试**

1. 将 `_task(...)` 夹具更新为传入有效 `source_layer`（按 `caps` 或显式参数分派 `det`/`core_security`/`fix`/`review` 并填入匹配 `input`）；将第 109-111 行字典下标断言改为 `.identity.base_commit` / `.env.image`。
2. 添加以下测试：
   - 验证 `SwebenchAdapter` 对 `swebench_verified` 保留 `version`、`environment_setup_commit`、`problem_statement`、`language="python"`、无 `input["instance"]`、无 `env["snapshot_root"]`；对 `multi_swe_bench` 未声明 `language` 时置 `""` 而保留真实 `raw["language"]`（如 `"go"`）。
   - 验证 `BugsInPyAdapter` 显式保留 `python_version` 至 `env.python_version` 并默认 `language="python"`。
   - 验证 `ReviewBenchAdapter` 补齐 `identity.head_commit`、`input.problem_statement`、无 `env["window"]`，且当 `registry` 仅声明 `{"F1": 0.5, "F2": 1.0}` 时通过 `supported=("F2", "F3")` 交集只输出 `("F2",)` 而不误声明 `F1`。
   - 验证 `SecurityAdapter` 在 `code` 与 `fixed_code` 均非空时生成标准 unified `diff` 并遵守 `entry_capabilities(..., supported=("F2", "F3"))`。
   - 验证 `CoreAdapter` 将 `layer == "security"` 映射为 `source_layer = "core_security"`，同时保留 `input.case["layer"] == "security"`，并在 `CoreCaseExpected` 中保留 `approvals` 与 `semantic`。

```python
def test_swebench_and_multi_swebench_fields_and_no_instance_dump():
    adapter = get_adapter("swebench")
    reg = load_registry()
    swe = reg.get("swebench_verified")
    mswe = reg.get("multi_swe_bench")
    inst = adapter.normalize_instance({
        "instance_id": "repo-1",
        "repo": "org/repo",
        "base_commit": "a" * 40,
        "patch": "diff --git a/a.py b/a.py\n+x = 1\n",
        "test_patch": "tpatch",
        "problem_statement": "Crash on empty input",
        "title": "Fix empty",
        "version": "2.1",
        "environment_setup_commit": "b" * 40,
        "language": "go",
        "python_version": "3.11",
    })
    tasks_swe = adapter.to_tasks(swe, [inst])
    t = tasks_swe[0]
    assert "instance" not in t.to_dict()["input"]
    assert "snapshot_root" not in t.to_dict()["env"]
    assert t.env.version == "2.1"
    assert t.env.environment_setup_commit == "b" * 40
    assert t.env.python_version == "3.11"
    assert t.input.problem_statement == "Crash on empty input"
    assert t.input.title == "Fix empty"
    assert t.input.profile == "bugfix"

    inst_no_lang = adapter.normalize_instance({
        "instance_id": "go-1", "repo": "org/go", "base_commit": "a" * 40, "patch": "p",
    })
    assert adapter.to_tasks(swe, [inst_no_lang])[0].env.language == "python"
    assert adapter.to_tasks(mswe, [inst_no_lang])[0].env.language == ""
    assert adapter.to_tasks(mswe, [inst])[0].env.language == "go"


def test_bugsinpy_preserves_python_version_and_language():
    adapter = get_adapter("bugsinpy")
    entry = load_registry().get("bugsinpy")
    inst = adapter.normalize_instance({
        "project": "tqdm", "bug_id": 1, "buggy_commit": "c" * 40,
        "python_version": "3.8.5", "patch": "diff --git a/a.py b/a.py\n+pass\n",
        "tests": ["tests/test_a.py::test_1"],
    })
    tasks = adapter.to_tasks(entry, [inst])
    assert tasks[0].env.python_version == "3.8.5"
    assert tasks[0].env.language == "python"


def test_reviewbench_head_commit_no_window_and_supported_capabilities():
    from eval.bench.models import DatasetEntry

    adapter = get_adapter("reviewbench")
    entry = DatasetEntry(
        name="custom_rb", kind="external", adapter="reviewbench",
        role="supplementary", capabilities={"F1": 0.5, "F2": 1.0},
        license="MIT", quality_grade="B", status="approved",
    )
    inst = adapter.normalize_instance({
        "repo": "org/r", "pr_number": 12, "base_commit": "a" * 40,
        "head_commit": "d" * 40, "diff": "diff --git a/a.py b/a.py\n+x = 1\n",
        "title": "PR title", "problem_statement": "PR description",
        "gold_findings": [{"path": "a.py", "line": 1, "severity": "major"}],
    })
    tasks = adapter.to_tasks(entry, [inst])
    t = tasks[0]
    assert t.capabilities == ("F2",)
    assert t.identity.head_commit == "d" * 40
    assert t.input.problem_statement == "PR description"
    assert t.input.profile == "change"
    assert "window" not in t.to_dict()["env"]


def test_security_generates_synthetic_diff_and_uses_supported_caps():
    from eval.bench.models import DatasetEntry

    adapter = get_adapter("security")
    entry = DatasetEntry(
        name="sec_sub", kind="external", adapter="security",
        role="supplementary", capabilities={"F2": 1.0},
        license="MIT", quality_grade="B", status="approved",
    )
    inst = adapter.normalize_instance({
        "id": "s1", "repo": "org/sec", "commit": "e" * 40,
        "code": "def f(x):\n    return os.system(x)\n",
        "fixed_code": "def f(x):\n    return safe_run(x)\n",
        "vulnerable": True, "severity": "critical", "cwe": "CWE-78",
    })
    tasks = adapter.to_tasks(entry, [inst])
    t = tasks[0]
    assert t.capabilities == ("F2",)
    assert t.source_layer == "security"
    assert "-    return os.system(x)" in t.input.diff
    assert "+    return safe_run(x)" in t.input.diff


def test_core_adapter_disambiguates_core_security_and_preserves_expected_flags():
    from eval.bench.models import DatasetEntry

    adapter = get_adapter("core")
    entry = DatasetEntry(
        name="core", kind="core", adapter="core",
        role="primary", capabilities={"F2": 1.0, "F6": 1.0},
        license="internal", quality_grade="A", status="approved",
    )
    t_sec = adapter._to_task(entry, {
        "id": "security.no_new_priv", "layer": "security",
        "expect": {"block": True, "rules": [], "tier": ""},
    })
    t_gate = adapter._to_task(entry, {
        "id": "gate.approvals", "layer": "gate",
        "expect": {"block": False, "approvals": 2, "semantic": True},
    })
    assert t_sec is not None and t_sec.source_layer == "core_security"
    assert t_sec.input.case["layer"] == "security"
    assert t_gate is not None
    assert t_gate.expected.approvals == 2
    assert t_gate.expected.semantic is True
```

- [ ] **Step 2: 运行测试确认按预期失败**

Run: `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest tests/test_bench_adapters.py`
Expected: FAIL (`KeyError` / `AssertionError` / `ValidationError` 因适配器仍在写 `input["instance"]`、`"window"`、`source_layer="security"` 等旧字段)。

- [ ] **Step 3: 更新全部适配器实现**

1. **`eval/bench/adapters/base.py`**：导入 `CAPABILITIES` 并添加共享函数：

```python
from ..models import CAPABILITIES


def entry_capabilities(
    entry: DatasetEntry,
    fallback: tuple[str, ...],
    *,
    supported: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """能力由 registry 声明与适配器支持能力共同决定；未声明时退回 fallback。"""
    declared = tuple(
        cap for cap in CAPABILITIES if float(entry.capabilities.get(cap, 0.0) or 0.0) > 0
    )
    if not declared:
        return fallback
    if supported is None:
        return declared
    matched = tuple(cap for cap in declared if cap in supported)
    return matched or fallback
```

在 `eval/bench/adapters/base.py` `__all__` 中补充 `"entry_capabilities"`。

2. **`eval/bench/adapters/swebench.py`**：
   - 从 `.base` 导入 `entry_capabilities`（不再在 `swebench.py` 重复定义，保持重导出兼容）；
   - `normalize_instance` 补充 `title`、`language`、`python_version`：
     - `"title": str(first_present(raw, "title", default=""))`
     - `"language": str(first_present(raw, "language", "lang", default="")).strip().lower()`
     - `"python_version": str(first_present(raw, "python_version", default="")).strip()`
   - `to_tasks` 构造强类型 `TaskIdentity`、`FixInput`、`FixExpected`、`TaskEnv`：

```python
            language = str(raw.get("language") or "") or (
                "" if entry.name == "multi_swe_bench" else "python"
            )
            tasks.append(TaskSpec(
                task_id=f"{entry.name}.{instance_id}",
                dataset=entry.name,
                capabilities=capabilities,
                identity=TaskIdentity(
                    repo=str(raw.get("repo") or ""),
                    base_commit=str(raw.get("base_commit") or ""),
                    pr_or_issue=f"instance:{instance_id}",
                    instance_id=instance_id,
                ),
                input=FixInput(
                    problem_statement=str(raw.get("problem_statement") or ""),
                    patch=str(raw.get("patch") or ""),
                    test_patch=str(raw.get("test_patch") or ""),
                    title=str(raw.get("title") or ""),
                    profile="bugfix",
                ),
                expected=FixExpected(
                    fail_to_pass=tuple(str(x) for x in (raw.get("fail_to_pass") or [])),
                    pass_to_pass=tuple(str(x) for x in (raw.get("pass_to_pass") or [])),
                ),
                env=TaskEnv(
                    image=str(raw.get("image") or "") or entry.image or "codeguard-sandbox-python:3.12",
                    language=language,
                    version=str(raw.get("version") or ""),
                    python_version=str(raw.get("python_version") or ""),
                    environment_setup_commit=str(raw.get("environment_setup_commit") or ""),
                    prepare_commands=tuple(str(x) for x in (raw.get("prepare_commands") or [])),
                    test_command=str(raw.get("test_command") or entry.test_command),
                ),
                license=entry.license,
                quality_grade=entry.quality_grade,
                source_layer="fix",
                role=entry.role,
            ))
```

3. **`eval/bench/adapters/bugsinpy.py`**：
   - `normalize_instance()` 显式补充 `"language": "python"` 与 `"python_version": str(first_present(raw, "python_version", "version", default="")).strip()`（保留 `"version": str(first_present(raw, "version", "python_version", default=""))`）；
   - `aggregate()` 中 `gold = _patch_paths(str(task.input.patch or ""))`。

4. **`eval/bench/adapters/reviewbench.py`**：
   - 使用 `capabilities = entry_capabilities(entry, ("F2", "F3"), supported=("F2", "F3"))`；
   - `to_tasks` 构造 `TaskIdentity(repo=..., base_commit=..., head_commit=str(raw.get("head_commit") or ""), pr_or_issue=..., instance_id=...)`、`ReviewInput(diff=..., title=..., problem_statement=str(raw.get("problem_statement") or ""), profile="change")`、`ReviewExpected(gold_findings=tuple(GoldFinding.model_validate(item) for item in (raw.get("gold_findings") or [])))`、`TaskEnv()`；
   - `aggregate()` 读取：

```python
            gold = [
                item.model_dump() if hasattr(item, "model_dump") else dict(item)
                for item in (by_id[obs.task_id].expected.gold_findings or ())
            ]
```

5. **`eval/bench/adapters/security.py`**：
   - 添加 `_synthetic_diff(code: str, fixed_code: str, path: str = "sample.py") -> str`：当 `code.strip()` 与 `fixed_code.strip()` 均非空时用 `difflib.unified_diff(code.splitlines(True), fixed_code.splitlines(True), fromfile=f"a/{path}", tofile=f"b/{path}")` 生成 unified diff，否则返回 `""`；
   - `capabilities = entry_capabilities(entry, ("F2", "F3"), supported=("F2", "F3"))`；
   - `to_tasks` 构造 `TaskIdentity`、`SecurityInput`、`SecurityExpected`、`TaskEnv()`；
   - `aggregate()` 改用 `by_id[obs.task_id].expected.vulnerable` 与 `by_id[obs.task_id].expected.severity`。

6. **`eval/bench/adapters/core.py`**：
   - `_to_task` 使用 `TaskIdentity`、`ProbeInput`/`ProbeExpected`、`ReviewerReplayInput`/`ReviewerReplayExpected`、`CoreCaseInput`/`CoreCaseExpected`、`TaskEnv()`；
   - `layer == "security"` 时 `source_layer = "core_security"`，同时保持 `input.case` 中的原 `layer`；
   - `CoreCaseExpected` 保留 `approvals=expect.get("approvals")`（int 或 None）与 `semantic=bool(expect.get("semantic"))`。

- [ ] **Step 4: 运行测试验证 Task 3 通过**

Run: `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest tests/test_bench_adapters.py`
Expected: PASS

- [ ] **Step 5: 提交 Task 3**

```bash
git add eval/bench/adapters/ tests/test_bench_adapters.py
git commit -m "feat(bench): align all adapters with TaskSpec v2 schema and drop raw instance dumps"
```

---

### Task 4: Harness 执行桥接层（`patch_source` / `fix_task` / `review_task` / `security_task`）属性访问与审查上下文对齐

**Files:**
- Modify: `eval/bench/harness/patch_source.py`
- Modify: `eval/bench/harness/fix_task.py`
- Modify: `eval/bench/harness/review_task.py`
- Modify: `eval/bench/harness/security_task.py`
- Test: `tests/test_bench_harness.py`

**Interfaces:**
- Consumes: `TaskSpec` v2 属性（`task.identity.*`, `task.input.*`, `task.expected.*`, `task.env.*`）
- Produces:
  - `run_review_task` 构造完整 `claim` 与 `profile` 透传至 `codeguard.agents.reviewer.review`
  - 所有 harness 模块零 `input["instance"]` / `.get()` 字典穿透。

- [ ] **Step 1: 在 `tests/test_bench_harness.py` 更新夹具并添加上下文对齐失败测试**

1. 更新第 253-255 行 `replace(_fix_task(), env={**_fix_task().env, ...})` 为：

```python
    base_task = _fix_task()
    task = replace(base_task, env={
        **base_task.env.model_dump(), "prepare_commands": ("printf 'v=1\\n' > prepared.txt",),
    })
```

2. 在 `test_run_review_task_with_fake_provider` 或新增 `test_run_review_task_forwards_claim_context` 中验证：当 `ReviewInput` 提供 `title`、`problem_statement`、`profile="change"` 时，传给 LLM `FakeProvider` 的 prompt 中包含该 `title` 与 `problem_statement`，且 `patch_source.resolve_patch(..., "system")` 可直接读取 `task.input.problem_statement`（无 `input["instance"]`）：

```python
def test_run_review_task_forwards_claim_context():
    provider = FakeProvider([json.dumps({
        "verdict": "fail", "summary": "issue found",
        "blocking": [], "advisory": [], "needs_human": False,
    })])
    task = TaskSpec(
        task_id="reviewbench.rb-ctx", dataset="reviewbench",
        capabilities=("F2", "F3"),
        identity={"repo": "demo/repo", "base_commit": "b" * 40, "pr_or_issue": "pr:102"},
        input={
            "diff": "diff --git a/app.py b/app.py\n@@ -1 +1 @@\n-return 1\n+return 2\n",
            "title": "Fix division guard",
            "problem_statement": "Reject zero denominator before dividing",
            "profile": "bugfix",
        },
        expected={"gold_findings": []}, env={},
        license="MIT", quality_grade="B", source_layer="review",
    )
    status, payload = run_review_task(task, provider=provider)
    assert status == "ok"
    user_prompt = provider.calls[0]["messages"][-1]["content"]
    assert "Fix division guard" in user_prompt
    assert "Reject zero denominator before dividing" in user_prompt
```

3. 在 `tests/test_bench_harness.py` 为 `security_task` 补一个自定义 `path="sub/mod.py"` 测试验证直接按 `task.input.path` 写入。

- [ ] **Step 2: 运行测试确认按预期失败**

Run: `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest tests/test_bench_harness.py`
Expected: FAIL (`AttributeError` / prompt 缺少 `title` 与 `problem_statement`)。

- [ ] **Step 3: 更新四个 Harness 文件**

1. **`eval/bench/harness/patch_source.py`**：移除 `_instance(task)`；`gold_patch` 直接读取 `str(getattr(task.input, "patch", "") or "")`；`system_patch` 直接读取 `str(getattr(task.input, "problem_statement", "") or "")`。
2. **`eval/bench/harness/fix_task.py`**：
   - `snapshot_path(task, snapshot_root)` 读取 `task.identity.repo` 与 `task.identity.base_commit`；
   - `run_fix_task` 直接读取 `task.input.test_patch`、`task.env.image`、`task.env.prepare_commands`、`task.env.test_command`、`task.expected.fail_to_pass`、`task.expected.pass_to_pass`。
3. **`eval/bench/harness/review_task.py`**：

```python
    diff = str(task.input.diff or "")
    title = str(task.input.title or "").strip()
    statement = str(task.input.problem_statement or "").strip()
    profile = str(task.input.profile or "change").strip() or "change"
    claim = (
        {"kind": profile, "title": title, "statement": statement}
        if (title or statement)
        else {}
    )
    ...
    decision, findings, costs = review(
        ctx,
        claim=claim,
        hybrid_report={},
        validation_report={"status": "passed"},
        diff_text=diff,
        changed_paths=patch_paths(diff),
        profile=profile,
    )
```

4. **`eval/bench/harness/security_task.py`**：直接读取 `task.input.code` 与 `rel_path = str(task.input.path or "sample.py")`，校验相对路径安全后写入 `root / rel_path` 并调用 `scan_tree(root, [rel_path])`。

- [ ] **Step 4: 运行测试验证 Task 4 通过**

Run: `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest tests/test_bench_harness.py`
Expected: PASS

- [ ] **Step 5: 提交 Task 4**

```bash
git add eval/bench/harness/ tests/test_bench_harness.py
git commit -m "feat(bench): align harness task runners with TaskSpec v2 and review claim context"
```

---

### Task 5: `BenchRunner` 纯按 `source_layer` 路由与 CLI v1 产物端到端重算

**Files:**
- Modify: `eval/bench/runner.py`
- Modify: `eval/bench/cli.py` (仅检查提示或类型，如有需要保持接口兼容)
- Test: `tests/test_bench_cli.py`

**Interfaces:**
- Consumes: `EXTERNAL_LAYERS`, `TaskSpec` v2 属性, `load_registry`
- Produces:
  - `BenchRunner._run_task` 纯按 `task.source_layer` 路由
  - `eval.bench score` 对 v1 格式 `tasks.jsonl` 无缝迁移重算。

- [ ] **Step 1: 在 `tests/test_bench_cli.py` 编写 v1 `tasks.jsonl` 端到端重评分测试与 `core_security` 路由测试**

```python
def test_cli_score_migrates_legacy_v1_tasks_jsonl(tmp_path: Path):
    run_dir = tmp_path / "legacy_run"
    run_dir.mkdir()
    v1_task = {
        "task_id": "reviewbench.org__r#1",
        "dataset": "reviewbench",
        "capabilities": ["F2", "F3"],
        "identity": {"repo": "org/r", "base_commit": "a" * 40, "pr_or_issue": "pr:1", "instance_id": "1"},
        "input": {
            "instance": {"head_commit": "b" * 40, "problem_statement": "desc"},
            "diff": "diff --git a/x.py b/x.py\n+x = 1\n",
            "title": "Fix",
        },
        "expected": {"gold_findings": [{"path": "x.py", "line": 1, "severity": "major", "category": "bug"}]},
        "env": {"window": 3},
        "license": "MIT",
        "quality_grade": "B",
        "source_layer": "review",
        "sample_uid": "u1",
        "role": "supplementary",
    }
    obs = {
        "task_id": "reviewbench.org__r#1",
        "dataset": "reviewbench",
        "status": "ok",
        "payload": {"findings": [{"path": "x.py", "line": 1, "severity": "high"}]},
        "capabilities": ["F2", "F3"],
        "sample_uid": "u1",
        "cost_cny": 0.0,
        "wall_ms": 1.0,
        "skip_reason": "",
        "error": "",
    }
    (run_dir / "tasks.jsonl").write_text(json.dumps(v1_task) + "\n", encoding="utf-8")
    (run_dir / "observations.jsonl").write_text(json.dumps(obs) + "\n", encoding="utf-8")
    (run_dir / "run_meta.json").write_text(json.dumps({"run_id": "r1"}), encoding="utf-8")
    (run_dir / "dedup.json").write_text("{}", encoding="utf-8")

    rc = main(["score", "--input", str(run_dir)])
    assert rc == 0
    scores = [
        json.loads(line)
        for line in (run_dir / "scores.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(row["capability"] == "F2" and row["value"] == 1.0 for row in scores)
```

- [ ] **Step 2: 运行 `tests/test_bench_cli.py` 观察运行情况**

Run: `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest tests/test_bench_cli.py`
Expected: 先确认当前 `BenchRunner` 属性访问变更前后的差异；在修改 `runner.py` 前，先运行现有 CLI 测试基线（注意 Task 1 已完成，此时旧 `runner.py` 中的 `task.input.get("case")` 会因 `CoreCaseInput` 无 `.get()` 而 FAIL！）。

- [ ] **Step 3: 更新 `eval/bench/runner.py` 路由与属性读取**

在 `eval/bench/runner.py`：
1. 导入 `EXTERNAL_LAYERS`（来自 `.models`）。
2. 更新 `_run_task`：

```python
        try:
            if task.source_layer == "probe":
                observation = self._run_probe(task)
            elif task.source_layer == "reviewer":
                observation = self._run_reviewer(task)
            elif task.source_layer in EXTERNAL_LAYERS:
                observation = self._run_external(task)
            else:
                observation = self._run_core(task, repeat_index)
```

3. 更新 `_run_core`、` _run_probe`、` _run_reviewer`：
   - `_run_core`: `raw = dict(task.input.case)`
   - `_run_probe`: `diff = str(task.input.diff or "")`, `expected = [str(item) for item in (task.expected.paths or ())]`, `"probe": str(task.input.probe or "")`
   - `_run_reviewer`: `case_name = str(task.input.case_name or task.task_id.rsplit(".", 1)[-1])`

- [ ] **Step 4: 运行 CLI 与全量 `test_bench_*.py` 验证 Task 5 通过**

Run: `D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest tests/test_bench_cli.py`
Expected: PASS

- [ ] **Step 5: 提交 Task 5**

```bash
git add eval/bench/runner.py tests/test_bench_cli.py
git commit -m "feat(bench): route BenchRunner strictly by source_layer and migrate CLI reads"
```

---

### Task 6: 全量测试与契约清洁度终审

**Files:**
- Test: `tests/test_bench_*.py`, `tests/test_eval*.py`, `tests/test_scorecard.py`, `tests/test_build_cases.py`, `tests/test_pseudo_pr.py`, `tests/test_report_and_metrics.py`, `tests/test_reviewer_quality.py`

- [ ] **Step 1: 确认工作树内无残留 `input["instance"]` 或 `env["window"]` / `env["snapshot_root"]`**

Run: `Select-String -Path 'eval\bench\*.py','eval\bench\*\*.py' -Pattern 'input\["instance"\]|env\["window"\]|env\["snapshot_root"\]'`（预期无运行时引用，仅 `models.py` 的 v1 迁移逻辑处理 `"instance"` / `"window"` / `"snapshot_root"` 弹出）。

- [ ] **Step 2: 运行全部 bench 及 eval 相关测试**

Run:
```powershell
$benchTests = Get-ChildItem tests/test_bench_*.py | ForEach-Object { $_.FullName }
D:\CodeGuard\.venv\Scripts\python.exe -m pytest -q --basetemp=.tmp/pytest $benchTests tests/test_eval.py tests/test_eval_matrix.py tests/test_scorecard.py tests/test_build_cases.py tests/test_pseudo_pr.py tests/test_report_and_metrics.py tests/test_reviewer_quality.py
```
Expected: 全部 PASSED，0 failed，0 unexpected skips。

- [ ] **Step 3: 检查工作树与主分支 `git status`**

Run: `git status -sb`（工作树应 clean；主分支 `D:\CodeGuard` 未受影响）。
