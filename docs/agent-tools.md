# Agent 工具能力

Agent 默认拥有受控工具，可在多轮循环里检索代码、请求沙箱执行测试。工具结果只作为
Agent 的输入，**不能替代** `validate` 节点、`review` 结论与确定性 merge gate。

## 工具矩阵

| Agent | 检索（read） | 执行（exec） | 修改（write） |
|---|---|---|---|
| `semantic_detector` | read_file、list_dir、grep、related_files | - | - |
| `finding_verifier` | read_file、grep | - | - |
| `issue_verifier` | read_file、list_dir、grep | - | - |
| `reviewer` | read_file、list_dir、grep | run_tests、run_check | - |
| `fixer` | 全部检索工具 | run_tests、run_check、run_command | apply_patch、write_file |

分配理由：

- 语义检测按 aspect 并行、成本敏感，只补跨文件证据，不给执行与写。
- finding 复核刻意保持廉价、无副作用，维持"只降不升"的复核语义。
- bugfix 的 base/head 复现由 `verify_issue` 节点确定性完成，Agent 不重复跑。
- reviewer 需要"验证自己的怀疑"，所以可以跑测试与策略检查，但绝不能改代码。
- fixer 在 job 私有副本内"改码 → 跑测试 → 再改"，减少无效轮次；最终仍产出
  unified diff，经 `validate_fix_patch` 与复验节点把关。

## 工具契约

所有工具实现 `ToolSpec`（名称、描述、参数 Pydantic 模型、副作用等级、超时）与
`ToolResult`（status/content/error/output_chars/duration_ms/meta）。状态含义：

| status | 含义 | 循环行为 |
|---|---|---|
| `ok` | 成功 | 结果回喂 |
| `failed` | 业务失败（如测试不通过、补丁不可应用） | 结果回喂，允许 Agent 调整 |
| `invalid_args` | 参数不合法 | 回喂错误，允许纠正 |
| `denied` | 越权（路径/命令/工具） | 回喂错误，不允许绕过 |
| `infra_error` | 沙箱或工具基础设施故障 | 立即抛 `AgentError`，job 判 `INFRA_FAILED` |

检索工具在宿主机读取 job 副本，不执行任何代码；执行工具一律构造 `SandboxSpec`
走 `ctx.sandbox.run()`，测试阶段强制 `network=none`；写工具只服务 fixer，只写
`data/jobs/<job_id>/fix_<n>`。

## 多轮循环

Agent 每步只输出一个 JSON：

```json
{"tool": "read_file", "args": {"path": "src/a.py", "start_line": 1, "end_line": 80}}
```

信息足够时直接输出目标 schema 的最终 JSON（也接受 `{"final": {...}}` 包裹）。循环约束：

- `max_steps`：LLM 调用步数上限；
- `max_tool_calls`：工具调用次数上限；
- `max_wall_sec`：墙钟上限，逐层传导到容器 wait 超时；
- `max_output_chars`：单条观测与历史转录的截断上限；
- 达到上限后强制收敛一次"只输出最终 JSON"，仍请求工具则判 Agent 失败。

工具观测统一按不可信数据包装（`<tool_result>` + `<untrusted_data>`），只保留最近
4 条、总量 ≤ 8k 字符，避免提示注入与 checkpoint 膨胀。

## 能力收敛（默认开启）

策略 `agents.tools.enabled` 默认 `true`，但实际能力取四层上限的最小值：

```
生效能力 = min(role 档位, tier 上限, mode 上限, 环境变量上限)
序关系：none < read < exec < write
```

- tier：`T0` 最多 `read`，`T1/T2` 不额外限制；
- mode：`external` 最多 `external_max_capability`（默认 `read`）；
- 环境变量：`AGENT_TOOLS_MAX_CAPABILITY` 只能下调；`AGENT_TOOLS_DISABLED=1` 整体关闭。

关闭后所有 Agent 退回改造前的单轮 `call_json`，提示词与行为逐字一致。

## 策略示例

```yaml
agents:
  tools:
    enabled: true
    max_steps: 6
    max_tool_calls: 6
    max_wall_sec: 120
    max_output_chars: 8000
    sandbox_timeout_sec: 120
    allow_commands:
      - "python -m pytest"
      - "ruff"
    external_max_capability: read
    roles:
      semantic_detector: read
      finding_verifier: read
      issue_verifier: read
      reviewer: exec
      fixer: write
```

`policy/default.yml` 已包含该段；仓库可用 `.codeguard/policy.yml` 覆盖，但策略只从
base 树读取，PR 内修改无效。schema 非法即 fail closed 转人工。

## 审计与预算

每次工具调用写入 `ReviewState.tool_events`（`append_bounded`，最近 200 条）：

```json
{"role": "reviewer", "tool": "run_tests", "args": "{\"paths\": ...}",
 "status": "ok", "duration_ms": 812, "output_chars": 1200, "error": ""}
```

`tool_calls` 是累计次数。LLM 成本仍按原方式并入 `budget_events`；沙箱调用只计耗时。
工具构造失败、能力为 `none` 或没有可用工具时返回空工具箱，调用方自动回退单轮。

`finalize` 写出的 `run_record` 还会带 `tool_calls` 与按 `role:tool` 聚合的
`tool_usage`（calls / failed / duration_ms），方便在 job 报告里看 Agent 实际做了什么。

## 扩展新工具

1. 在 `tools/fs_tools.py` / `sandbox_tools.py` / `edit_tools.py` 实现 handler，
   输入是 Pydantic 参数模型，输出 `ToolResult`。
2. 在 `tools/registry.py` 注册：`ARGS_MODELS`、`TOOL_CAPABILITY`、`TOOL_DESCRIPTIONS`、
   `TOOL_TIMEOUTS`，并加入对应角色的 `ROLE_TOOLS`。
3. 在 `AgentToolBox.bind_default_handlers()` 绑定 handler；写工具必须同时校验
   `allowed_files`、受保护路径与大小上限。
4. 补测试：参数越界、权限拒绝、失败/基础设施错误、审计字段与工具矩阵。

## fixer 的沙箱内迭代

`fix_node` 在 fixer 获得 `write` 能力时：

1. `materialize` 到 `data/jobs/<job_id>/fix_<n>`，`init_baseline_repo` 建立 git 基线；
2. fixer 用 `read_file` / `apply_patch` / `write_file` / `run_tests` 迭代；
3. 节点用 `worktree_diff` 生成 unified diff；
4. 在干净副本上跑 `validate_fix_patch`（allowed_files、受保护前缀、200KB、
   `git apply --check`）；
5. 之后仍走既有推送、复验、review 与 merge gate。

Agent 只改了文件没给 patch 也没问题：以工作区最终状态为准。能力不足（`read`/`exec`
或环境关闭）时回退为原来的单轮 patch 流程。
