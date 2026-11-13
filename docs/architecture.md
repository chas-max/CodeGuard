# 架构

## 分层

1. **L0 接入**：webhook 验签、delivery 去重、job 注册、配置/预算/策略。
2. **L1 确定性检测**：仓库地图、diff、Python AST、规则引擎、API 契约、测试完整性、补丁校验、风险分级。
3. **L2 LLM 语义检测**：`semantic_detector` 检测跨文件契约、数据流、并发/资源、整体设计；`finding_verifier` 对 blocking 语义 finding 做两票复核。
4. **L3 Agent 编排**：`issue_verifier`、`semantic_detector`、`finding_verifier`、`reviewer`、`fixer`；`Coordinator` 并行调度。
5. **L4 执行/动作**：Docker 沙箱、GitHub 评论/Review/Check/修复提交/Merge、人工中断。

## LangGraph 节点

`ingest → preflight → hybrid_scan → verify_issue → validate → review → merge_gate → merge`
辅助节点：`fix`、`human_approval`、`finalize`。

- `preflight` 解析 profile、计算风险等级、读取 base 分支的 `review.md`。
- `hybrid_scan` 可在 `candidate=pr` 或 `candidate=fix` 上重复执行；内部完成 findings 对账与 blocking 复核。
- `verify_issue` 按 profile 分派：`bugfix` 走 base/head 复现，`change` 只解析意图（advisory）。
- `review` 是唯一审查 Agent；修复后复用同一 Agent，第二轮起只审增量 diff。
- `human_approval` 在审批人数不足时自环，直到满足 tier 要求或被人拒绝；是图中唯一的自环。
- `merge` 前再次校验审批的 sha/树哈希，避免审批被复用。

## 协调器

`Coordinator` 负责阶段内任务 DAG：

- 并行：静态分片、规则、API 契约、测试守卫、语义分片、Docker 验证。
- 串行：依赖任务、补丁生成/应用（单写者）、GitHub 写操作。
- 限制：每 job 最大并行、LLM/沙箱并发、任务超时、取消、预算。
- 结果合并：按 task id 确定性排序，finding 按 id 去重。

LangGraph 负责状态机、断点、条件路由；Coordinator 不改变图拓扑，只改变阶段内执行方式。

## 状态

`ReviewState` 是唯一真相源；节点不持有跨 job 可变状态。所有计数器、候选版本、扫描/验证/审查结果都写入 state。

`findings` 是**权威快照**（每轮按 id 对账），`finding_history` 是有界审计日志——这一区分保证已解决的问题不会反复进入修复指令。

## 责任边界

- 两票复核不是第二个审查 Agent：它只判断单条 finding 的真伪，不能产出 verdict，也不能提升 blocking。
- 合并门控是确定性的：审批、证据、树哈希、blocking 数量都是硬条件，LLM 结论不能推翻。
- 发布与指标是旁路：报告失败不影响终态，合并成功也不因指标写失败而回滚。
