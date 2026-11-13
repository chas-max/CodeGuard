# 策略

默认策略在 `policy/default.yml`；仓库可在 base 分支放 `.codeguard/policy.yml` 覆盖。
`preflight` 只从 base 树读取策略，PR 对策略的修改不生效，并会触发 `POL-001`。

## 顶层

- `mode`：`internal`（默认，任意变更、按 tier 审批）或 `external`（bugfix 复现门禁）。
- `version`：策略版本，当前为 1。

## profiles

- `default`：`change` 或 `bugfix`；
- `allow`：允许的变更类型（bugfix/feature/refactor/docs/chore）。

`external` 模式强制 `bugfix`；`internal` 下 label `codeguard:bugfix` 或 API 显式传入可切到 `bugfix`。

## tiers（确定性风险分级）

```yaml
tiers:
  rules:
    T0: { paths: ["docs/**", "**/*.md"], max_changed_lines: 200 }
    T2: { paths: ["**/auth/**", ".github/**"], min_changed_lines: 1000 }
  default: T1
  min_human_approvals: { T0: 0, T1: 1, T2: 2 }
  require_non_author_for: [T2]
  force_human_paths: [".github/workflows/**", ".codeguard/**"]
```

- 命中 T0 路径且改动量在上限内、且不涉及依赖/CI/受保护路径 → T0；
- 命中 T2 路径或达到 T2 行数下限 → T2；
- 受保护/CI/依赖路径、公共 API 破坏 → 至少 T1；
- 作者不可信或 agent 创作 → 至少 T1（只升不降）；
- `min_human_approvals` 必须随风险单调不减；`allow_zero_human: false` 时 T0 的 0 票会被提升为 1 票。

## review

- `guidance_path`：仓库级审查指南（默认 `.codeguard/review.md`，只读 base）；
- `max_nits`：非阻塞 finding 的展示上限（阻塞项永远保留）；
- `depth_by_tier`：`quick` / `standard` / `deep`，写入审查上下文；
- `max_findings_verified`：每次最多复核多少条 blocking 语义 finding（0 = 关闭复核）。

## merge

- `allow_zero_human`：是否允许 T0 零人工（默认 false）；
- `queue.update_branch_on_stale`：预留开关，配套 `GitHubClient.update_branch` 已实现；
  **当前版本 base 移动仍按 `STALE` 转人工**，自动重跑留待后续接入；
- `queue.max_attempts`：重跑次数上限（同上，预留）。

## trusted_authors

只支持具体 login；`team:org/team` 需要 GitHub team API，当前按不可信处理。

## limits / gates / sandbox

- `limits`：文件数、改动行数、diff 字节数。
- `gates.require_reproduction`：`null` 表示由 mode 决定（internal 否 / external 是）。
- `sandbox.test_command_template`：`{tests}` 为测试文件占位符；
- `sandbox.checks`：额外检查（如 `ruff check .`），`required: true` 的失败会阻断；
- `sandbox.prepare_commands` + `prepare_network`：预装依赖阶段（默认 `none`）；
- `sandbox.test_commands` 保留兼容，`test_command_template` 优先。

`rules.disable`：禁用指定规则；critical 安全规则默认不建议禁用。

策略 schema 非法时 fail closed，转人工，不合并。
