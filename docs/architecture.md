# CodeGuard v2 架构

## 核心设计思想

CodeGuard v2 在 LangGraph `StateGraph` 之上实现了**并行多视角审查 + QA/Verify 质量对齐 + 多轮复审闭环（上限 5 轮）+ 确定性分级门控 + 本地向量程序化记忆**架构。

## LangGraph 状态机（16 节点）

主流程：
`prepare_context -> pr_reader -> dispatch_reviewers -> [review_logic | review_security | review_architecture | review_tests] -> synthesize_findings -> coder_agent -> qa_checker -> verify_gate -> (回流 dispatch_reviewers 增量复审) -> summary_and_submit -> merge_gate -> [human_approval] -> merge -> persist_memory_and_finalize`

1. **`prepare_context`（Node 1）**：并行完成工作区校验、`RepoMap` 构建、仅从 base 分支加载 `policy.yml`/`review.md`、代码分区（源码/测试/依赖配置/文档/CI）、静态与规则快扫以及确定性风险下限 `classify_tier`。
2. **`pr_reader`（Node 2）**：提取结构化 `PRBrief`（问题背景、修改前后行为、分区摘要），在确定性下限上做**单调不降级**的重要等级判定（`low / medium / critical`，LLM 仅可升级不可降级）；`bugfix` 场景运行 base/head 复现验证。
3. **`dispatch_reviewers` + 4 路并行分支（Node 3）**：
   - `review_logic`（逻辑错误、边界处理）
   - `review_security`（安全规范、鉴权与漏检）
   - `review_architecture`（项目规范、架构设计）
   - `review_tests`（测试覆盖、回归守卫）
   各视角配置**最小权限只读工具箱**，通过 `merge_dict_by_key` Reducer 在状态中保留各自独立的短期记忆 `ReviewerMemory`，避免跨轮重复提出已解决/已拒绝问题。
4. **`synthesize_findings`（Node 4）**：确定性路径与证据对齐、跨视角去重、保留确定性阻塞项、状态生命周期对账，并签发不可变修复契约 `FixDirective`（仅当测试审查明确指出需补测时开放 `allowed_test_addition_files`，且受 AST `preserves_existing_tests` 防删测守卫约束）。
5. **`coder_agent`（Node 5）**：在隔离副本工作区中运行，拥有读写与 Docker 沙箱命令权限以及本地程序化记忆检索工具 `search_procedural_memory`（最小权限绑定修复白名单）。
6. **`qa_checker`（Node 6，参考 EMNLP 2024 CodeAgent）**：同时审查 `FixDirective`、Coder 总结与**真实 `git diff`**，执行范围/空补丁硬校验与对齐判定（`pass | revise | escalate`）；未对齐则退回 `coder_agent` 重修（内层闭环）。
7. **`verify_gate`（Node 9 前置）**：在 PR 提交与合并前执行 Python AST 语法校验、`prepare_commands`、策略检查（lint/typecheck）与 Docker 回归测试；失败直接退回 `coder_agent`（内层闭环）。通过后标记 `verified_tree_hash`，并在 `candidate=fix` 且 `review_round < max_review_rounds (5)` 时自动回流到 `dispatch_reviewers` 触发下一轮**增量复审**（外层闭环）。
8. **`summary_and_submit`（Node 7）**：生成继承不降级重要等级的 `ReviewSummary`，依据配置推回修复 commit（`submit_mode="push_commit"`）或创建关联修复 PR（Fork PR 或 `submit_mode="new_pr"`）。
9. **`merge_gate` / `human_approval` / `merge`（Node 8）**：确定性门控强制校验 SHA 与 tree hash 一致性、QA/Verify 全绿及分级审批门槛（`low=0`、`medium=1`、`critical=2` 且至少一名非作者）。
10. **`persist_memory_and_finalize`（终态）**：仅在结果确定/合并审批完成后，将经验提取为 `ProceduralMemoryRecord`（向量嵌入 + 结构化元数据）持久化到本地 `sqlite-vec` 数据库（含 `numpy` 余弦回退），并写入报告与指标。

## 责任边界与安全红线

- **只读与写权严格隔离**：PR Reader、4 路 Reviewer 与 QA Checker 均无写文件与宿主机执行权；只有 Coder Agent 拥有写工具与 Docker 沙箱。
- **策略与测试完整不可篡改**：策略只认 base 分支；测试文件仅在明确授权下允许新增测试函数，严禁删除/跳过既有断言。
- **确定性门控不可被 LLM 推翻**：分级门控与多轮上限（默认 5 轮）全部由状态字段和路由代码确定性执行。
