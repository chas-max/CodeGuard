# 安全

- **不执行不可信代码**：PR 代码仅在 Docker 内运行；宿主机只做 git / AST。
- **提示词注入**：PR 文本与 `review.md` 都用 `<untrusted_data>` 包裹；Agent 工具经白名单与能力收敛控制（检索只读副本、执行只在 Docker 沙箱、写只给 fixer），工具观测同样按不可信数据回喂；输出经 Pydantic 校验。
- **Secret**：命中 secret 直接 blocking；日志/评论/LLM 输入不包含原文。
- **测试完整性**：删除测试、skip/xfail、删断言、降低覆盖率门槛直接 blocking，不允许修复 Agent 自动绕过。
- **策略信任**：只读 base 分支策略与审查指南；PR 修改策略无效且被标记。
- **合并安全**：merge API + SHA 守卫 + 树哈希 + 分支保护 + 高风险人工；绝不直接 push 目标分支。
- **审批安全**：一次审批绑定一个 commit（sha + tree_hash）；同一人只计一次；T2 至少一名非作者；机器人 review 固定 `COMMENT`，不冒充人工批准。
- **沙箱**：network none、read-only root、non-root、cap-drop ALL、无 Docker socket/宿主密钥。
- **幂等**：delivery 去重、job key、评论 marker、merge SHA 守卫；head 变化后旧结论与旧审批失效。

## internal 模式放松什么、不放松什么

放松：

- 审批人数（按 tier，T0 可 0 人，需显式开启）；
- 允许的变更类型（不再限制 bugfix）；
- 复现要求（`change` profile 不要求 base 失败）。

不放松：

- 输入隔离与提示词注入防护；
- secret / 危险调用 / 测试弱化 / 受保护路径的 blocking；
- 只读 base 的策略与指南；
- 树哈希与 SHA 守卫；
- 高风险路径的人工审批下限（至少 2 人）。

内部仓库的常见风险是**误操作**而不是恶意，但这不构成降低输入隔离的理由。
