# eval.bench 使用说明

评测平台代码入口。默认离线（零 LLM、零网络），公开数据集只在 `fetch` 阶段联网物化。

## 1. 命令

| 命令 | 作用 | 是否需要网络 |
|---|---|---|
| `python -m eval.bench run --profile external --out reports/bench_ext` | 只跑外部主分数据集（默认） | 否 |
| `python -m eval.bench run --profile supplement --dataset core --fixtures` | 只跑自建回归集 | 否 |
| `python -m eval.bench run --profile full` | 两边都跑，但报告分栏、不混算 | 否 |
| `python -m eval.bench score --input reports/bench_ext` | 从观测重算三轨分数 | 否 |
| `python -m eval.bench report --input reports/bench_ext` | 重新渲染 Markdown | 否 |
| `python -m eval.bench fetch --list` | 列出数据集登记（含 `role`） | 否 |
| `python -m eval.bench fetch --status` | 查看物化/快照状态 | 否 |
| `python -m eval.bench fetch --verify` | 校验哈希、行数、快照 HEAD | 否 |
| `python -m eval.bench fetch --dataset X --source <path/url/hf://>` | 下载并切片 | 是（URL/HF） |
| `python -m eval.bench fetch --dataset X --snapshot --limit N` | checkout 仓库快照 | 是 |
| `python -m eval.bench fetch --dedup` | 生成跨数据集去重索引 | 否 |

`--dataset` 显式指定时覆盖 `--profile`；`core` 自动视为 supplementary。
`external` profile 没有外部数据时输出 `NO_EXTERNAL_EVIDENCE`，不会自动回退去跑 core。

外部修复任务：

```bash
# 先用 gold patch 自证 harness（应当 resolve），再跑 system
python -m eval.bench run --profile external --dataset swebench_verified --live \
  --patch-source gold --snapshot-root eval/datasets/bench/snapshots --out reports/bench_swe
python -m eval.bench run --profile external --dataset swebench_verified --live \
  --patch-source system --snapshot-root eval/datasets/bench/snapshots --out reports/bench_swe_system
```

`--live` 需要 Docker 与 LLM key；缺失时任务标 `skipped`，不计分母、不计通过。
`--keep-work` 保留外部任务工作区，便于复核补丁与日志；默认成功后清理。

实例级字段（写在 `instances.jsonl` 的单条记录里）：

| 字段 | 作用 |
|---|---|
| `image` | 覆盖该实例的沙箱镜像，例如带项目测试依赖的镜像 |
| `test_command` | 覆盖测试命令；`src` 布局仓库可用 `PYTHONPATH=/workspace/src python -m pytest -q` |
| `prepare_commands` | 测试前的一次性初始化，允许 `network=prepare`；例如生成 `_version.py` |

SWE-bench Lite 的部分参数化 nodeid 在数据集里被截断（如 `[assert`）。harness 会把
方括号不配对的 nodeid 标为 `invalid`、不计入分母；遇到 pytest exit 4 时退回按测试文件
执行，避免一个坏 nodeid 拖垮整批。若 pytest/依赖本身无法导入（例如旧版 pytest 跑在
Python 3.12 上），任务标 `error + env_error`，同样不计入分母，不当作系统修复失败。

## 2. 六维功能模型与三轨口径

| 能力 | 含义 | 核心指标 | 主分 |
|---|---|---|---|
| F1 | PR 理解与变更分析 | `probe_accuracy`（补丁解析路径 vs 实际变更路径） | 门禁维，不占权重 |
| F2 | 缺陷检测与定位 | `f1` / `precision` / `recall` / `localization_accuracy` | 计权 |
| F3 | 代码审查质量 | `f1` / `severity_macro_f1` | 计权 |
| F4 | 自动代码修复 | `resolve_rate`（FAIL_TO_PASS 全通过） | 计权 |
| F5 | 自动测试与回归 | `pass_to_pass_rate` / `validation_pass_rate` | 计权 |
| F6 | Agent 工作流与工具 | `task_success_rate` | 单独 `S_integration` |

三条轨道互不求和：

| 轨道 | 来源 | 维度 | 输出 |
|---|---|---|---|
| `primary` | `role=primary` 且 `status=approved`、许可证清晰的外部数据集 | F1–F5 | `S_primary`；缺失则 `PARTIAL` / `NO_EXTERNAL_EVIDENCE` |
| `supplementary` | `role=supplementary` 的自建集（`core`） | F1/F2/F3/F5（F1 仅诊断） | `S_supplementary`，标 `SELF_BUILT_ONLY` |
| `diagnostic` | F1 探针、F6 工作流 | F1、F6 | F1 门禁与 `S_integration` |

状态机：

| 状态 | 条件 | 输出 |
|---|---|---|
| `OK` | F1–F5 全部有结果且 F1 达标 | 各 F 分数 + 主分 |
| `PARTIAL` | 至少一个 F 有结果，但 F1–F5 未齐（或 F1 未达标） | 已有 F 分数，`total = null` |
| `NO_EXTERNAL_EVIDENCE` | F1–F5 全部无外部结果 | 只输出缺失原因 |
| `SUPPLEMENTARY_ONLY` | 只有自建证据 | 自建分数，标“非项目综合能力” |
| `INTEGRATION_ONLY` | 只有 F6 工作流证据 | `S_integration`，无主分 |

## 3. 评分与报告

- 数据集分 = registry 声明的主指标 × 100。
- 功能分 = 来源加权平均，`w = grade_weight × coverage`；A=1.0、B=0.7、C 不计分。
- 主分：`S_primary = Σ S_c·w_c / Σ w_c`，仅统计 F2/F3/F4/F5（原权重 25/25/25/15 在轨道内
  归一化为 0.2778/0.2778/0.2778/0.1667）；五维齐全且 F1 达标才输出 `total`。
- 缺维时仍输出已有 F 的分数，另给 `partial`（重归一化）与 `strict`（缺失记 0），`total = null`。
- 自建补充分只在 supplementary 轨道内归一化，禁止与主分相加。
- 门槛在 `eval/datasets/bench/thresholds.yml`；默认 `enforce: false`，只报告不阻断。
- 报告产物：`report.json` / `report.md` / `evaluation.json` / `observations.jsonl` /
  `tasks.jsonl` / `scores.json` / `dedup.json` / `run_meta.json`。

`report.json` 里的 `tracks` 分别给出 `primary` / `supplementary` / `integration` /
`diagnostic` 的状态、逐维分数与来源；`coverage_matrix` 给出每个 F 维的来源、样本数、
目标样本量、状态与许可证；`evidence_mix` 给出外部/自建样本数、权重占比与任务数。

## 4. 数据集登记

`eval/datasets/bench/registry.json` 每条声明：

- `role`：`primary` / `supplementary` / `diagnostic`（缺省 `supplementary`）；
- `adapter`：`core` / `swebench` / `bugsinpy` / `reviewbench` / `security`；
- `license`、`status`（`candidate/approved/quarantined/rejected`）、`quality_grade`；
- `capabilities`（该数据集对每项能力的覆盖权重 0..1）与 `primary_metrics`；
- `snapshot_required`、`repo_url_template`、`image`、`test_command`。

`core` 必须是 `supplementary`；`role=primary` 但要进主分还需要 `approved` 且许可证清晰。
`status=candidate` 或许可证 `UNKNOWN` 的数据集不进入主分，只作定性证据。
公开数据集的仓库、许可证、字段与规模标 `verification: pending`，核验前不得当作事实。

## 5. F1 外部探针

外部实例不单独寻找数据集，直接从补丁生成：

1. 应用候选 patch 后取工作树真实变更路径（`git diff --name-only` + 未跟踪文件）；
2. 用 `patch_paths(patch)` 得到解析路径；
3. 两者集合一致 → `f1_probe.passed = true`，写入 observation payload；
4. 适配器把 `f1_probe` 聚合成 F1 `probe_accuracy`。

F1 探针同时作为主分门禁：缺失或低于合格线时只输出其余维度的分数，不给总结果。

## 6. 去重与置信区间

- `sample_uid = sha256(repo | base_commit | pr_or_issue | diff_norm_hash)`；
- `diff_norm_hash` 忽略 hunk 行号、行尾空白，并按路径排序；
- 同一 uid 只计一次，`dedup.json` 输出跨数据集重叠矩阵；
- 比例类指标附 Wilson 95% 区间；报告同时给点估计与保守分（区间下界）；
- 样本量低于 `min_samples` 的来源不计分，低于 `recommended_samples` 标 `small_sample`；
- `env_error` 的任务不计入分母。

## 7. 已知边界

- 公开基准只实现了适配与 harness 骨架；快照未物化、无 Docker、无 key 时一律 `skipped`。
- `patch_source=system` 通过 `agents/fixer.propose_fix` 生成补丁，缺少 provider 时跳过。
- SWE-CARE / ReviewBench / BugsInPy / Devign / PrimeVul / Multi-SWE-bench 的许可证与字段
  仍待核验，保持 `candidate`，不进入主分。
- F6 没有公开基准，只能自建集成测试，单独输出 `S_integration`。
- 不修改旧 `eval/scorecard.py` 与 `eval/run_matrix.py` 的判定语义；旧评测命令继续可用。
