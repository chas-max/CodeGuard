# 内部 PR 审查模式

CodeGuard 现在有两种部署形态，由仓库策略的 `mode` 决定：

| mode | 场景 | 信任前提 | 默认 profile |
|---|---|---|---|
| `internal` | 公司内部 PR（默认） | 作者可信但会误操作 | `change` |
| `external` | 外部/不可信 PR | 作者可能是攻击者 | `bugfix` |

internal 模式的目标函数是「**在错误放行率不上升的前提下，减少人类阅读时间**」，而不是把人类从流程里去掉。

## profile：问题验证 vs 变更审查

| profile | 触发 | 信任锚点 | 走不走 Docker 复现 |
|---|---|---|---|
| `bugfix` | API/CLI 显式指定、`codeguard:bugfix` label、`mode: external` | base 上复现测试失败 | 走 |
| `change` | 其余情况（feature/refactor/docs/chore） | 意图 + 检查结果 + 风险分级 + 人工审批 | 不走复现，只跑检查/回归 |

`change` 的意图判定是 **advisory**：PR 说不清要解决什么，不会阻断流程，只会进入审查上下文与报告。

## 风险分级（确定性）

`tiers.rules` 按路径与改动量判定，**由变更决定，不由作者决定**：

| 等级 | 默认判定 | 人工审批 | 审查深度 |
|---|---|---|---|
| T0 | 只改文档（`docs/**`、`**/*.md` 等）且 ≤200 行 | 0（需显式 `allow_zero_human`） | quick |
| T1 | 普通源码变更 | 1 | standard |
| T2 | auth/security/migrations/billing/CI/依赖，或 ≥1000 行 | 2，且至少一名非作者 | deep |

只升不降：作者不在 `trusted_authors`、PR 由 agent 创作（`Co-authored-by: Claude` 等 trailer 或 `agent` label）→ 至少 T1。

## 审批

- 一次审批 = 一个 `Approval(approver, sha, tree_hash, at)`；**绑定 commit**，head 或工作区树变化后自动作废；
- 同一 login 只计一次；T2 要求至少一名非作者；
- 环境变量 `MERGE_MODE=human` 只能收紧，不能放宽策略；
- 机器人永远不冒充人工审批：review event 固定 `COMMENT`。

未凑够人数时 `human_approval` 自环，继续收集；给出拒绝则直接终态。

## findings 生命周期

`findings` 是**权威快照**而不是只增不减的日志：

- 本轮仍出现 → `open`；
- 上一轮 open、本轮消失 → `resolved`，进 `finding_history`；
- 只有 `open and blocking` 会阻塞合并、才会进入修复指令。

这解决了「修好的问题被反复下达修复指令」的收敛问题。修复 Agent 按 `class_id` 分组，一次修完一类问题的所有实例。

## blocking finding 的两票复核

只对 **semantic 来源且 blocking** 的 finding 触发，最多 `review.max_findings_verified` 条：

| 票型 | 结果 |
|---|---|
| 两票都 drop | 降级为 advisory（`demoted_by=finding_verifier`） |
| 一票 drop 一票 keep | 降级为 advisory 并标注分歧（`demoted_by=verifier_tie`） |
| 复核模型不可用 | **保持 blocking** 并记录 issue（宁可让人看一眼，也不静默放行） |

两票刻意使用不同 provider（`deepseek-flash` + `qwen3.8-flash`），降低同源误差相关性。复核只降不升，因此不会扩大误报面；它不产出 verdict，也不构成第二个终审 Agent。

## 收敛

- 第 2 轮起只审增量 diff（`fix_patch`），不重新挑上一轮已通过的代码；
- 达到 `max_review_rounds` 仍 request_changes → 不再进入修复，置 `escalation_reason` 并以 `BLOCKED_HUMAN` 结束，要求改方案；
- 同类问题聚合、`resolved` 不重复提、同一 finding 的评论用 marker 去重。

## 发布与指标

每次运行产出 `reports/jobs/<job_id>.json`：

| 字段 | 含义 |
|---|---|
| `profile` / `tier` | 本次走的 profile 与风险等级 |
| `approvals` / `required_approvals` / `approvers` | 审批人数与审批人 |
| `approval_latency_sec` | PR 创建到第一次人工批准 |
| `review_rounds` / `fix_rounds` | 审查与修复轮次 |
| `findings_*` / `finding_outcomes` | 规模、blocking 数、人工判定结局 |
| `cost_cny` | LLM 成本 |

`codeguard stats --dir reports/jobs` 聚合出：human actions per PR、approval latency、rounds-to-pass、finding precision、零人工合并占比、cost per PR。

## 上线顺序（不要反过来）

1. 先提高信噪比：生命周期、review thread、nit 上限、pre-existing 标记；
2. 再让 reviewer 可信：`review.md`、两票复核、收敛、事故 eval；
3. 然后才做分级审批（tier）；
4. 最后才开 `allow_zero_human`，且必须先有回滚/监控。

## 与 claude.dev 那篇文章的对应关系

采纳：分层检查、findings 先复核再交人、按变更定风险、审批绑定 commit、`REVIEW.md`、review 必须收敛、事故即 eval、噪声度量、merge-queue-lite（`update_branch` 已就位，自动重跑尚未接入）。

不采纳：投票集群（>2 票）、自动回滚、feature flag 编排、GraphQL thread 解析。理由分别是成本/相关性、发布路径归属、超出审查系统边界、REST 无该能力。

## 已知边界

- 仍只完整支持 Python 仓库；
- `team:org/team` 可信作者需 GitHub team API，当前按不可信处理；
- `update_branch_on_stale` 打开后只重跑一次，失败仍转人工；
- 零人工合并默认关闭。
