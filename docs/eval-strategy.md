# 评测集选型：为什么必须"自建为主 + 外部切片"

本文回答一个问题：**CodeGuard 能不能直接拿网络上的公开测试集来做评测？**

结论：**不能。** 没有任何公开测试集同时满足本项目的五项适用性要求。因此核心评测集自建，
公开数据集只作为"外部有效性切片"，用来验证"合法修复不会被误拦"和"真实缺陷能被召回"。

## 1. 判定"完全适用"的五项标准

CodeGuard 不是单点模型，而是一条带确定性门控的流水线。一个测试集要能完整评测它，必须同时提供：

| # | 标准 | 为什么必须有 | 对应代码 |
|---|---|---|---|
| C1 | PR 级 diff + 可本地固化的 base 树 | `preflight` / `hybrid_scan` / `review` 都读双树；diff 文本决定审查输入 | `nodes/pipeline.py`、`engine/diff.py` |
| C2 | 可执行的测试/回归 | `validate` 节点在 Docker 中跑 `policy.sandbox.test_commands`，状态直接进 merge gate | `sandbox/`、`github/merge.py::merge_blockers` |
| C3 | "应拦截 / 应放行"的门控标签 | 这是本项目的核心指标（false merge / false block），不是 review comment 质量 | `engine/hybrid.py`、`github/merge.py` |
| C4 | policy / tier / 审批标签 | tier 决定审批人数与审查深度；审批绑定 `sha + tree_hash` | `engine/tier.py`、`github/merge.py::valid_approvals` |
| C5 | 许可证明确、可离线复现 | 跑批必须可重复、可审计，不能每次回源 GitHub | `eval/` 全部 |

只满足 C1/C2 的数据集能测"改得对不对"，测不了"该不该合并"；只满足 C3 的数据集能测"评论准不准"，
测不了门控与审批。**本项目的评测对象是判定链路，不是代码生成或评论生成。**

## 2. 公开数据集核查矩阵

核查时间：2026-11。结论只对当时的公开信息负责。

| 数据集 | 规模 | C1 diff/base | C2 可执行测试 | C3 门控标签 | C4 policy/tier | C5 许可/离线 | 可用边界 |
|---|---|---|---|---|---|---|---|
| **SWE-CARE / CodeFuse-CR-Bench**（`codefuse-ai/SWE-CARE`，HF `inclusionAI/SWE-CARE`，arXiv 2509.14856） | 601 实例 / 70 个 Python 项目；9 类 PR 问题域 | 有 commit SHA、PR number、`diff_hunk`；**无完整仓库快照**，仓库级上下文需按 SHA 自行 clone | 数据集本身不含测试执行 | ❌ 只有人工参考评审意见与合并补丁 | ❌ 无 tier/审批/policy | 仓库有 `LICENSE`（以文件为准，惯例 Apache-2.0）；自带 `rule_based_evaluator` 与 `llm_evaluator` | **reviewer/语义召回切片** |
| **Qodo PR-Review-Bench**（HF `Qodo/PR-Review-Bench`） | 100 个真实 PR / 580 个注入问题 / 7 语言 | ❌ 只给 `pr_url_to_review` 与问题片段；无 base/head SHA、无快照 | ❌ | 行级注入缺陷标签（非门控） | ❌ | 页面**未声明许可证**；两个 JSONL 列不一致导致 Hub Viewer 报错，需手动下载 | 仅本地评测的召回参照 |
| **CR-Bench**（Nutanix，arXiv 2603.11078） | 584 / Verified 174，由 SWE-bench 转换 | 输入只给 repo + PR 号 + 标题 + 描述 + diff | 隐含（源自 SWE-bench） | 缺陷级 + taxonomy，非门控 | ❌ | 论文未给下载入口、未提许可证 | 公开性存疑，**不作基线依赖** |
| **Greptile AI Code Review Benchmark 2025** | 50 个真实 bug / 50 PR / 5 语言 | 有 fork 仓库 + base 分支 + PR diff | ❌ 未提及测试 | 行级是否命中原始 bug | ❌ | 无许可证、无 harness/脚本，仅 25 个 fork PR 记录 | 人工复现成本高，仅作 recall 参照 |
| **SWE-bench / Lite / Verified** | 2294 / 300 / 500 | ✅ base commit + Docker 环境 | ✅ `FAIL_TO_PASS` / `PASS_TO_PASS` | ❌ 无 review 标签 | ❌ | MIT、公开、可离线固化 | **"合法修复不被误拦"与 validate 切片** |

补充结论：

- 五个候选里只有 SWE-bench 同时满足 C1+C2+C5，它恰好**只覆盖"修复正确性"**，与审查质量无关。
- 只有 SWE-CARE 提供仓库级上下文与参考评审，但它没有门控标签，且需要回源 GitHub 才能拿到完整树。
- Qodo / Greptile / CR-Bench 的许可与分发条件不清晰，**不进入基线依赖**，最多做一次性对照实验。

## 3. 决策：自建核心 + 外部切片

### 3.1 自建核心（唯一基线）

自建 150–250 条分层用例，覆盖公开数据集**完全没有**的部分：

| 层 | 覆盖对象 | 首轮规模 | 是否可离线 |
|---|---|---|---|
| `det` | 13 个确定性 rule_id 的正例/负例/边界：`SYN-001`、`SEC-001`、`SEC-002`、`ENV-001`、`SIZE-001`、`PATH-001`、`PATH-002`、`CI-001`、`DEP-001`、`POL-001`、`SCOPE-001`、`API-001`、`TEST-001`（含 5 种子语义） | 100–130 | ✅ 零 LLM、零 Docker |
| `semantic` | 注入缺陷的语义召回：空值解引用、off-by-one、吞异常、资源未释放、共享可变状态、跨文件契约破坏 | 20–30 | 离线回放 / 实时二选一 |
| `gate` | tier 分级、审批人数、`sha + tree_hash` 绑定、stale、高风险路径、非作者审批 | 25–35 | ✅ |
| `security` | secret 外泄、受保护路径绕过、沙箱隔离不变量、工具能力收敛、预算与步数熔断 | 20–30 | ✅ |
| `e2e` | 全链路：干净变更放行、规则拦截、语义拦截、fix 收敛、人工审批 | 10–20 | fake 全链路 / 真实 Docker+LLM |

### 3.2 外部切片（只做外部有效性验证）

| 切片 | 用途 | 规模 | 前置条件 |
|---|---|---|---|
| SWE-bench Lite | 合法修复是否被误拦、`validate` 通过率 | 10–15 | 本地快照 + Docker 镜像 |
| SWE-CARE test 抽样 | reviewer 语义召回与噪声比 | 10–20 | 按 commit SHA checkout 的仓库副本 |

约束：

- 外部数据**不进入**基线评分卡的分母，单独出"外部有效性"小节；
- 快照未固化时该层标记为 `skipped`，不得算作通过；
- 许可不明确的数据集（Qodo / Greptile / CR-Bench）只在本地评测，**不提交数据本体**，manifest 只记来源与哈希。

## 4. 这套设计能回答什么问题

| 优化方向 | 对应指标 | 证据层 |
|---|---|---|
| 规则误报太多 | 每 rule 的 precision、`false_block` | `det` |
| 语义漏检 | 语义 recall、`semantic_blocked` 对照 | `semantic`、`external` |
| 门控太松/太严 | `false_merge`、`false_block`、gate 一致率 | `gate`、`e2e` |
| 审批形同虚设 | `valid_approvals` 作废率、非作者审批缺口 | `gate` |
| 跑得太贵/太慢 | 成本、墙钟、轮次、工具收敛率 | `e2e` |
| 安全边界被绕过 | 不变量通过率、注入抵抗率 | `security` |

## 5. 不做什么

- 不改 `run_pr_eval.py` 的判定逻辑，不为了跑分放宽 `merge_blockers` 或 `validate`；
- 不把 LLM 自评当门控结论；语义 finding 必须与确定性 finding 分开归因；
- 不在跑批时联网拉取外部数据；
- 不把未经实测的指标写进基线报告。
