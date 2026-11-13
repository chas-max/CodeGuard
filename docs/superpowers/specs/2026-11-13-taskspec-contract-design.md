# TaskSpec 契约规范化与上下文对齐设计

- **日期**：2026-11-13
- **分支**：`feat/taskspec-contract`
- **工作树**：`D:\CodeGuard\.worktrees\feat-taskspec-contract`
- **范围**：`eval/bench` 任务契约（`TaskSpec`）、外部/核心适配器（`adapters`）、执行桥接层（`harness` / `runner`）、去重（`dedup`）及配套测试（`tests/test_bench_*.py`）

---

## 1. 背景与问题陈述

当前 `eval/bench/models.py` 中的 `TaskSpec` 将 `identity`、`input`、`expected`、`env` 全部定义为无 Schema 约束的原始 `dict`，导致以下结构性缺陷：

1. **整包透传与双份冗余**：`SwebenchAdapter`、`ReviewBenchAdapter`、`SecurityAdapter` 在 `to_tasks()` 中把完整原始对象塞进 `input["instance"]`，导致 `tasks.jsonl` 重复存储大字段，`patch_source.py` / `security_task.py` 不得不穿透 `input["instance"]` 取值。
2. **关键外部字段丢失与上下文断层**：
   - `SwebenchAdapter` / `BugsInPyAdapter` 提取了 `version` 与 `environment_setup_commit` 却未写入 `TaskSpec.env`；
   - `ReviewBenchAdapter` 提取了 `head_commit` 却未写入 `TaskSpec.identity`；
   - `SecurityAdapter` 提取了 `fixed_code` 却未在 `TaskSpec.input` 中生成可用于差量检测/去重的对比信息；
   - 缺失与 `multi_swe_bench` 及 `CodeGuard` 自身 `ReviewProfile` 对齐的 `language` / `profile` 字段；
   - `review_task.py` 拥有 `title` / `problem_statement` 却未透传至 `reviewer` 的 `claim` 上下文。
3. **契约校验缺失与语义冲突**：
   - `source_layer` 默认 `""` 且无白名单校验；
   - `core` 集的 `"security"` 层（安全不变量探针）与外部脆弱性集（`devign` / `primevul`）的 `"security"` 层共用同一个 `source_layer="security"`，无法仅凭 `source_layer` 唯一确定 Schema 与执行路由；
   - `TaskEnv` 混入评估算法超参 `window` 与未消费字段 `snapshot_root`；
   - `ReviewBenchAdapter` 与 `SecurityAdapter` 硬编码 `capabilities=("F2", "F3")`，未遵循 `registry.json` 的能力裁剪声明。

---

## 2. 设计目标与约束

1. **契约范围**：采用**强类型 Pydantic v2 子模型 + 外层 `TaskSpec` 不可变容器**，保留单功能 Harness 运行模式，不改变 `Observation`、`ScoreRecord`、`scoring.py` 与 `report.py` 的三轨评分语义。
2. **向后兼容**：`TaskSpec.from_dict()` 必须自动迁移 `main` 分支产出的 v1 格式 `tasks.jsonl`（无 `schema_version` 或 `schema_version < 2`、含 `input["instance"]`、旧 `env["window"]`/`env["snapshot_root"]`、core `source_layer="security"`），保障 `eval.bench score` / `eval.bench report` 命令无缝重算历史产物。
3. **全局约束**：
   - 外部 `TaskSpec` 所有结构模型均使用 Pydantic v2 `BaseModel`，配置 `ConfigDict(frozen=True, extra="forbid")`；
   - `TaskSpec` 外层维持 `@dataclass(frozen=True)`（与同文件 `DatasetEntry`、`Observation`、`ScoreRecord`、`RunMeta` 一致，兼容 `dataclasses.replace`），在 `__post_init__` 中强制校验并归一化四桶字段；
   - 所有跨模块接口不得依赖 `input["instance"]`。

---

## 3. Schema 架构设计（`eval/bench/models.py`）

### 3.1 版本与分层常量

```python
TASK_SCHEMA_VERSION = 2
CORE_LAYERS = ("probe", "reviewer", "det", "semantic", "gate", "core_security", "e2e", "external")
EXTERNAL_LAYERS = ("fix", "review", "security")
SOURCE_LAYERS = CORE_LAYERS + EXTERNAL_LAYERS
TASK_PROFILES = ("change", "bugfix", "")
NORMALIZED_SEVERITIES = ("high", "medium", "low")
```

- **路由分离**：原 `core` 集的 `layer == "security"` 投影为 `source_layer = "core_security"`（其内部 `CoreCaseInput.case["layer"]` 仍保持 `"security"` 供 `eval.run_matrix.Runner` 使用），外部脆弱性样本独占 `source_layer = "security"`。

### 3.2 公共子模型

1. **`TaskIdentity`**（`frozen=True, extra="forbid"`）：
   - `repo: str = ""`
   - `base_commit: str = ""`
   - `head_commit: str = ""`
   - `pr_or_issue: str = ""`
   - `instance_id: str = ""`
   - 归一化规则（`model_validator(mode="before")`）：
     - 将旧别名 `commit` 归一化为 `base_commit`（当 `base_commit` 为空时）；
     - 所有字段强制转为 `str` 并去除首尾空白；
     - 若 `pr_or_issue` 为空且 `instance_id` 非空，自动补齐 `pr_or_issue = f"instance:{instance_id}"`。

2. **`TaskEnv`**（`frozen=True, extra="forbid"`）：
   - `image: str = ""`
   - `language: str = ""`
   - `version: str = ""`
   - `python_version: str = ""`
   - `environment_setup_commit: str = ""`
   - `prepare_commands: tuple[str, ...] = ()`
   - `test_command: str = "python -m pytest -q"`
   - 校验：`test_command` 若为空字符串则回退默认 `"python -m pytest -q"`。移除 `window` 与 `snapshot_root`。

### 3.3 按 `source_layer` 判别的 `input` / `expected` 模型

| `source_layer` | `input` 模型（`extra="forbid"`） | `expected` 模型（`extra="forbid"`） |
|---|---|---|
| `fix` | `FixInput`: `problem_statement: str = ""`, `patch: str = ""`, `test_patch: str = ""`, `title: str = ""`, `diff: str = ""`, `profile: Literal["change", "bugfix", ""] = "bugfix"` | `FixExpected`: `fail_to_pass: tuple[str, ...] = ()`, `pass_to_pass: tuple[str, ...] = ()` |
| `review` | `ReviewInput`: `diff: str`, `title: str = ""`, `problem_statement: str = ""`, `profile: Literal["change", "bugfix", ""] = "change"` | `ReviewExpected`: `gold_findings: tuple[GoldFinding, ...] = ()` |
| `security` | `SecurityInput`: `code: str`, `fixed_code: str = ""`, `diff: str = ""`, `path: str = "sample.py"`, `language: str = "python"` | `SecurityExpected`: `vulnerable: bool = False`, `severity: Literal["high", "medium", "low"] = "high"`, `cwe: str = ""` |
| `probe` | `ProbeInput`: `probe: str`, `diff: str = ""` | `ProbeExpected`: `paths: tuple[str, ...] = ()` |
| `reviewer` | `ReviewerReplayInput`: `case_name: str`, `response: dict[str, Any]` | `ReviewerReplayExpected`: `blocking: tuple[str, ...] = ()` |
| `det` / `semantic` / `gate` / `core_security` / `e2e` / `external` | `CoreCaseInput`: `case: dict[str, Any]`（校验 `case` 含非空 `id`） | `CoreCaseExpected`: `block: bool = False`, `rules: tuple[str, ...] = ()`, `tier: str = ""`, `approvals: int | None = None`, `semantic: bool = False` |

其中 `GoldFinding`（`frozen=True, extra="forbid"`）定义为：
- `path: str`（校验 `strip()` 后非空）
- `line: int = 0`（校验 `>= 0`）
- `severity: Literal["high", "medium", "low"] = "medium"`（构造前经 `normalize_severity` 对齐）
- `category: str = ""`

### 3.4 `TaskSpec` 构造与序列化契约

```python
@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    dataset: str
    capabilities: tuple[str, ...]
    identity: TaskIdentity
    input: TaskInput
    expected: TaskExpected
    env: TaskEnv
    license: str
    quality_grade: str
    source_layer: str
    sample_uid: str = ""
    role: str = DEFAULT_ROLE
    schema_version: int = TASK_SCHEMA_VERSION
```

- `__post_init__` 流程：
  1. 校验 `task_id`、`dataset` 非空，`capabilities` 非空且无重复且全部属于 `CAPABILITIES`，`role in ROLES`，`quality_grade in QUALITY_GRADES`，`schema_version >= 1`；
  2. 校验 `source_layer in SOURCE_LAYERS`（不再允许 `source_layer=""`）；
  3. 按 `LAYER_SCHEMAS[self.source_layer]` 获取 `(InputModel, ExpectedModel)`，分别对 `self.identity`、`self.input`、`self.expected`、`self.env` 执行 `model_validate`（支持直接传入模型实例或合法 `dict`），并通过 `object.__setattr__` 固化为不可变 Pydantic 实例。
- `to_dict()`：返回 JSON 兼容 `dict`，包含 `schema_version: 2`，子模型通过 `.model_dump(mode="json")` 展开（`tuple` 自动转为 `list`）。

---

## 4. 模块改造与数据流对齐

### 4.1 适配器层（`eval/bench/adapters/`）

1. **提取共享能力派生工具 `entry_capabilities(entry, fallback, *, supported=None)`** 到 [adapters/base.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/adapters/base.py)，供 `SwebenchAdapter`、`BugsInPyAdapter`、`ReviewBenchAdapter`、`SecurityAdapter` 统一调用：
   - 按 `entry.capabilities` 声明（权重 > 0）派生能力，未声明时回退 `fallback`；
   - 当传入 `supported`（适配器实际产出评分的能力集合）时对其求交集（若交集为空则用 `fallback`）。例如 `ReviewBenchAdapter` 仅产出 `F2`/`F3` 指标（`registry.json` 的 `primary_metrics` 亦仅列 `F2`/`F3`），设 `supported=("F2", "F3")`，既允许 `registry` 裁剪单个能力，又避免声明未实现的 `F1`；`SecurityAdapter` 实际产出 `F2`/`F3`，设 `supported=("F2", "F3")`。
2. **`SwebenchAdapter`**（[adapters/swebench.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/adapters/swebench.py)）：
   - `normalize_instance()` 新增读取可选字段 `language`（及 `title`、`python_version`）；
   - `to_tasks()` 构建 `TaskIdentity(repo, base_commit, pr_or_issue=f"instance:{instance_id}", instance_id)`；
   - `FixInput(problem_statement, patch, test_patch, title, profile="bugfix")`，不再写入 `input["instance"]`；
   - `FixExpected(fail_to_pass, pass_to_pass)`；
   - `TaskEnv(image, language=raw.get("language") or ("" if entry.name == "multi_swe_bench" else "python"), version, python_version, environment_setup_commit, prepare_commands, test_command)`，不再写入 `snapshot_root`。
3. **`BugsInPyAdapter`**（[adapters/bugsinpy.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/adapters/bugsinpy.py)）：
   - `normalize_instance()` 将 `python_version` 显式保留到归一化结果（同时保留 `version`），默认 `language="python"`；
   - 继承复用 `SwebenchAdapter.to_tasks()`，自动获得 `version`、`python_version`、`language="python"` 及 `FixInput`/`FixExpected` 强类型校验；
   - `aggregate()` 改由 `task.input.patch` 读取 gold patch（不再使用 dict `.get()`）。
4. **`ReviewBenchAdapter`**（[adapters/reviewbench.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/adapters/reviewbench.py)）：
   - `capabilities = entry_capabilities(entry, ("F2", "F3"), supported=("F2", "F3"))`；
   - `TaskIdentity` 补齐 `head_commit`；
   - `ReviewInput(diff, title, problem_statement, profile="change")`，不再写入 `input["instance"]`；
   - `ReviewExpected(gold_findings=...)`；
   - `TaskEnv()` 保持干净，不再注入 `window=3`；`aggregate()` 继续使用 `match_findings(predicted, gold, window=3)`（同时兼容读取 `GoldFinding` 模型与 dict）。
5. **`SecurityAdapter`**（[adapters/security.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/adapters/security.py)）：
   - `capabilities = entry_capabilities(entry, ("F2", "F3"), supported=("F2", "F3"))`；
   - `SecurityInput(code, fixed_code, diff=_synthetic_diff(code, fixed_code), path="sample.py", language="python")`，当 `code` 与 `fixed_code` 均非空时通过 `difflib.unified_diff` 生成标准 unified diff，供去重与差异检查复用；
   - `SecurityExpected(vulnerable, severity, cwe)`。
6. **`CoreAdapter`**（[adapters/core.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/adapters/core.py)）：
   - `layer == "security"` 映射为 `source_layer = "core_security"`；
   - 使用 `ProbeInput`/`ProbeExpected`、`ReviewerReplayInput`/`ReviewerReplayExpected`、`CoreCaseInput`/`CoreCaseExpected`（补充保留 `approvals` 与 `semantic`）。
7. **`probes.py`**（[probes.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/probes.py)）：同步适配 `TaskSpec` v2 构造。

### 4.2 执行与去重层

1. **`BenchRunner._run_task`**（[runner.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/runner.py)）：
   - 纯按 `task.source_layer` 路由：
     - `"probe"` → `_run_probe`
     - `"reviewer"` → `_run_reviewer`
     - `in ("fix", "review", "security")` → `_run_external`
     - `in ("det", "semantic", "gate", "core_security", "e2e", "external")` → `_run_core`
   - `_run_core`、` _run_probe`、`_run_reviewer` 改为属性访问（如 `task.input.case`、`task.input.diff`、`task.expected.paths`、`task.input.case_name`）。
2. **`patch_source.py`**（[harness/patch_source.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/harness/patch_source.py)）：
   - 删除 `_instance(task)`，直接读取 `task.input.patch` 与 `task.input.problem_statement`。
3. **`fix_task.py`**（[harness/fix_task.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/harness/fix_task.py)）：
   - 直接读取 `task.identity.repo`、`task.identity.base_commit`、`task.input.test_patch`、`task.env.image`、`task.env.prepare_commands`、`task.env.test_command`、`task.expected.fail_to_pass`、`task.expected.pass_to_pass`。
4. **`review_task.py`**（[harness/review_task.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/harness/review_task.py)）：
   - 直接读取 `task.input.diff`、`task.input.title`、`task.input.problem_statement`、`task.input.profile`；
   - 当 `title` 或 `problem_statement` 非空时构造 `claim = {"kind": profile or "change", "title": title, "statement": problem_statement}` 传入 `codeguard.agents.reviewer.review(..., profile=profile or "change")`，使审查上下文与项目主流程保持对齐。
5. **`security_task.py`**（[harness/security_task.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/harness/security_task.py)）：
   - 直接读取 `task.input.code` 与 `task.input.path`（缺省 `"sample.py"`），移除 `instance` 回退读取。
6. **`dedup.py`**（[dedup.py](file:///d:/CodeGuard/.worktrees/feat-taskspec-contract/eval/bench/dedup.py)）：
   - `sample_uid(identity, patch_text="")` 兼容 `TaskIdentity` 与普通 `dict`（提取只读 helper `_id_get`）；
   - `task_patch(task)` 直接读取 `getattr(task.input, "patch", "") or getattr(task.input, "diff", "")`。

---

## 5. v1 → v2 迁移兼容与错误处理

`TaskSpec.from_dict(raw)` 在检测到 `int(raw.get("schema_version") or 1) < TASK_SCHEMA_VERSION` 时触发 `_migrate_v1_task(raw)`：

1. **层名消歧**：若 `dataset == "core"` 且 `source_layer == "security"`，迁移为 `source_layer = "core_security"`。
2. **解包旧 `input.instance`**：
   - 提取 `input.get("instance")` 字典（若存在），按目标 `source_layer` 抽取有效字段并去除 `instance` 键：
     - `fix`：从 `instance` 补充 `problem_statement`、`title`、`patch`、`test_patch` 至 `input`，补充 `version`、`environment_setup_commit`、`image`、`test_command`、`prepare_commands` 至 `env`，补充 `instance_id`、`repo`、`base_commit` 至 `identity`；
     - `review`：从 `instance` 补充 `title`、`problem_statement`、`diff` 至 `input`，补充 `head_commit`、`instance_id` 至 `identity`；
     - `security`：从 `instance` 补充 `code`、`fixed_code` 至 `input`，补充 `cwe`、`severity` 至 `expected`；
   - 对已知外层 `input`/`expected` 字段保留原值（外层优先于 `instance`）。
3. **清理 `env` 死字段**：丢弃 `env` 中的 `window` 与 `snapshot_root`。
4. **统一校验落盘**：迁移后的字典补齐 `schema_version = 2`，交由 `cls(**migrated)` 走完整 Pydantic 校验。

**错误处理原则**：
- 任何缺失或非法的 `source_layer`、未知键（`extra="forbid"`）、类型错误在 `TaskSpec` 构造期直接抛出 `ValueError`（含 Pydantic `ValidationError`）；
- `from_dict()` 仅迁移已知 v1 结构，不吞掉非法字段或类型错误。

---

## 6. 测试与验收计划

1. **模型与契约单测**（`tests/test_bench_models.py`）：
   - 覆盖全部 `source_layer` 合法构造与非法字段/未知层名拦截；
   - 校验 `TaskSpec` 与子模型深不可变性（`frozen=True`）；
   - 校验 `to_dict()` 不包含 `input.instance` 且 `to_dict()` / `from_dict()` 往返无损；
   - 覆盖 `v1 -> v2` 迁移测试（包含 `fix`/`review`/`security`/`core_security` 的旧格式 JSON 反序列化）。
2. **适配器单测**（`tests/test_bench_adapters.py`）：
   - 验证 `ReviewBenchAdapter` / `SecurityAdapter` 正确通过 `entry_capabilities` 与 `supported` 交集派生 `capabilities`（如 `registry` 裁剪到仅 `F2` 时自动生效，且不误声明未实现的 `F1`）；
   - 验证 `SwebenchAdapter` / `BugsInPyAdapter` 将 `version`、`environment_setup_commit` 写入 `env`、`ReviewBenchAdapter` 将 `head_commit` 写入 `identity`、`SecurityAdapter` 为 `code`+`fixed_code` 合成 `diff`。
3. **Harness / Dedup / CLI 单测**（`tests/test_bench_harness.py`, `tests/test_bench_dedup.py`, `tests/test_bench_cli.py`）：
   - 验证 `review_task` 正确构造并透传 `claim`（含 `title` / `problem_statement`）；
   - 验证 `patch_source` / `fix_task` / `security_task` / `dedup` 基于 v2 属性正常执行；
   - 构造旧格式 v1 `tasks.jsonl` 测试 `eval.bench score` 端到端重算成功。
4. **全量回归**：在工作树中运行 `pytest -q --basetemp=.tmp/pytest tests/test_bench_*.py` 及相关 `eval` 测试，确保 0 失败、0 跳过新增测试。
