# CodeGuard

基于 **LangGraph + Docker + 多 Agent** 的内部 PR 审查系统：

> 对任意内部变更做审查：按变更风险分级、找出问题、给出证据、必要时让修复 Agent 生成补丁并在 Docker 里验证，最后由唯一审查 Agent 做整体审查；按 tier 收集人工审批后，满足确定性门控才合并。

也支持 `mode: external`：对不可信 PR 保留「必须复现 bug」的历史门禁。

## 核心流程（internal / change profile）

```
GitHub PR 事件 / API / CLI
  → 预检（PR、base/head、仓库地图、base 策略、review 指南、Docker 健康、风险分级）
  → 混合检测（确定性静态/规则/API 契约/测试守卫 + LLM 跨文件语义）
      → findings 生命周期对账（open/resolved）
      → blocking 语义 finding 两票复核（只降不升）
  → 意图验证（change：解析 PR 目标与验收证据，advisory）
  → Docker 验证（按策略跑检查/回归）
  → 唯一 reviewer 整体审查（guidance + nit 上限；第二轮起只审增量 diff）
  → 按 tier 收集人工审批（T0 0、T1 1、T2 2；审批绑定 commit）
  → 确定性 merge gate → 合并
  → 发布 review + 写 job record / 指标
```

`bugfix` profile 额外要求「base 上复现测试失败」，走的是历史流程。

## 设计要点

- **profile 分裂**：`change` 面向任意变更（feature/refactor/docs/chore），`bugfix` 面向带复现声明的修复；由 API/CLI、`codeguard:bugfix` label 或 `mode: external` 决定。
- **确定性风险分级**：`T0/T1/T2` 由路径与改动量决定，作者身份只能抬高等级；tier 决定审批人数与审查深度。
- **确定性检测 + LLM 语义检测**：语法、secret、路径、测试完整性、API 签名由确定性规则裁决；`semantic_detector` 补足跨文件契约、数据流、并发/资源生命周期等盲区。确定性结论优先。
- **唯一审查 Agent**：只有 `reviewer`。两票复核只判断「这条 finding 是否为真」，不产出 verdict，不构成第二个终审。
- **findings 生命周期**：`open/resolved` 对账；只有 open 且 blocking 才阻塞合并、才进入修复；修复按类聚合。
- **审查收敛**：第 2 轮只审增量 diff；达到轮次上限转人工并要求改方案；同类问题不重复提；评论用 marker 去重。
- **发布与指标**：结论以 review + inline 评论发布（机器人不冒充人工批准）；每次运行写 `reports/jobs/<job_id>.json`，`codeguard stats` 聚合 human actions / 审批延迟 / 轮次 / precision / 成本。
- **协调器**：`Coordinator` 在阶段内并行调度只读任务，统一超时、取消、预算与并发上限；LangGraph 负责状态机与断点。
- **Docker 隔离**：测试容器 `network=none`、只读根、非 root、cap-drop ALL、无 Docker socket、无宿主密钥；PR 代码不在宿主机执行。
- **安全合并**：走 GitHub PR merge API，校验 `head_sha`、树哈希、mergeable、必需检查、分支保护；高风险路径强制人工；不直接 push 目标分支。

## 快速开始

```bash
# 0. 安装（推荐虚拟环境）
pip install -e .

# 1. 配置
copy .env.example .env        # 填 LLM key；GitHub 可在启动后由控制台接入

# 1.1 GitHub App（推荐，无需手填 token）
#   a) 起内网穿透：cloudflared tunnel --url http://127.0.0.1:8002
#   b) 打开 http://127.0.0.1:8002/ui/#/settings，按三步向导填地址 / 创建 App / 安装仓库
#   c) 凭据写入 data/github-app.json（0600），installation token 自动续期
#   详见 docs/github-app.md；CLI 与离线评测仍可用 GITHUB_TOKEN

# 2. 构建沙箱镜像（需要 Docker daemon）
docker build -f dockerfiles/sandbox/python.Dockerfile -t codeguard-sandbox-python:3.12 .

# 3. 本地 dry-run（无需 GitHub；测试仍需要 Docker daemon）
python codeguard_app.py review --local D:\path\to\repo --base-root D:\path\to\base \
    --changed src/calc.py tests/test_calc.py --title "fix: zero divide" --dry-run

#    作者侧自检（pre-push）：只审相对 main 的改动，有 blocking 就返回 1
python -m codeguard review --local . --diff-from origin/main --fail-on-blocking

# 4. 启动 webhook / API 服务
python codeguard_app.py serve --port 8002
# POST /webhooks/github           GitHub webhook
# POST /api/reviews               手动触发（支持 local_repo）
# GET  /api/jobs/{id}             快照
# GET  /api/jobs/{id}/stream      SSE
# POST /api/jobs/{id}/resume      人工审批恢复（含 approver / finding_outcomes）
# GET  /api/health                配置 / Docker 状态

# 5. 测试（无 API key / 无 Docker 也可全绿）
python -m pytest -q
python -m pytest -m docker      # Docker 集成测试，daemon 不可用自动 skip

# 6. 离线评测（零 LLM、零网络）
python -m eval.run_pr_eval --out reports/pr_eval
python -m eval.run_reviewer_eval --out reports/reviewer_eval

# 7. 指标与事后归因
python -m codeguard stats --dir reports/jobs --out reports/metrics
python -m codeguard incidents sync --repo owner/name --sha <merge-sha> --fixture
```

## 评测平台（bench）

在旧五维 scorecard 之上新增六维功能评分（`eval/bench/`），默认离线可跑；
旧 `det/semantic/gate/security/e2e` 语义不变。

```bash
# 默认只跑外部主分数据集；没有已物化外部数据时输出 NO_EXTERNAL_EVIDENCE
python -m eval.bench run --profile external --out reports/bench_ext

# 自建回归：只跑 core，报告标 SELF_BUILT_ONLY
python -m eval.bench run --profile supplement --dataset core --fixtures \
  --limit 8 --out reports/bench_demo

# 两边都跑，但报告分栏，不混算
python -m eval.bench run --profile full --out reports/bench_full

# 从已有观测重算 / 重新渲染报告
python -m eval.bench score --input reports/bench_demo
python -m eval.bench report --input reports/bench_demo

# 数据集登记、物化状态与离线校验（数据本体不提交）
python -m eval.bench fetch --list
python -m eval.bench fetch --status
python -m eval.bench fetch --verify

# 物化公开基准切片（联网只发生在 fetch）
python -m eval.bench fetch --dataset swebench_verified --source <本地JSONL或HF直链> --limit 10
python -m eval.bench fetch --dataset swebench_verified --snapshot --limit 5
```

评分口径按三轨输出：`primary`（外部已核准数据集，F1–F5）给出 `S_primary`，
`supplementary`（自建集）给出标 `SELF_BUILT_ONLY` 的回归分，F6 单独给
`S_integration`。F1 是门禁维（不占权重）；F2/F3/F4/F5 在轨道内归一化为
`0.2778/0.2778/0.2778/0.1667`。任意维度缺失时仍输出其余 F 的分数，但
`total = null`（`PARTIAL`），不再整份报告判 `INVALID`；自建分永远不进入主分，
也不与主分相加。门槛（floors/regression）只写进报告，不改变退出码。
详见 [eval/bench/README.md](eval/bench/README.md)。

## 控制台（前端）

`web/` 是操作台前端（Vite + React + TS），构建后由后端在 `/ui` 托管：

```bash
cd web && npm install && npm run build
cd .. && python codeguard_app.py serve --port 8002   # http://127.0.0.1:8002/ui/
```

覆盖：仓库切换、PR 列表与定时查看、一键触发审查、11 节点全链路追踪与各节点结果、HITL 审批/驳回与 findings 处置。
新增接口在 `src/codeguard/console.py`（`/api/repos`、`/api/pulls`、`/api/jobs`、`/api/jobs/{id}/trace`、`/api/watch`）。
详见 [docs/console.md](docs/console.md)。

## 目录

```
src/codeguard/
├── models.py                 # Pydantic 领域模型（Finding/Approval/RiskTier…）
├── config.py                 # Settings + 启动校验
├── state.py                  # ReviewState + reducers
├── graph.py                  # 11 节点 LangGraph
├── runtime.py / server.py / cli.py
├── engine/                   # 仓库地图、diff、静态、规则、策略、API 契约、测试守卫、
│                             # 补丁、混合合并、风险分级(tier)、findings 生命周期、
│                             # 审查指南(guidance)、指标、事故归因
├── orchestration/            # Coordinator 并行 DAG、JobScheduler
├── sandbox/                  # Docker / fake 后端、工作区、健康检查、运行器
├── github/                   # REST 客户端、webhook 验签、合并门控、review 发布
├── agents/                   # issue_verifier / semantic_detector / finding_verifier /
│                             # reviewer / fixer
├── prompts/                  # 纯函数提示词
└── nodes/                    # 节点与路由
dockerfiles/sandbox/python.Dockerfile
web/                          # 控制台前端（Vite + React + TS，产物挂到 /ui）
policy/default.yml
eval/                         # 夹具、确定性评测、reviewer 离线评测
tests/                        # fake LLM/GitHub/Sandbox + e2e
```

## 策略

默认策略在 `policy/default.yml`；仓库可在 base 分支放 `.codeguard/policy.yml` 覆盖。
**只读取 base 分支策略**，PR 对策略的修改不会生效，并会被 `POL-001` 标记。

关键字段：

- `mode`：`internal`（默认）或 `external`。
- `profiles`：默认 profile 与允许的变更类型。
- `tiers`：`rules`（路径/行数）、`min_human_approvals`、`require_non_author_for`、`force_human_paths`。
- `review`：`guidance_path`、`max_nits`、`depth_by_tier`、`max_findings_verified`。
- `merge`：`allow_zero_human`、`queue.update_branch_on_stale`。
- `trusted_authors`：可信作者 login 列表（`team:` 前缀暂不支持）。
- `limits` / `gates` / `protected_paths` / `high_risk_paths` / `sandbox` / `rules.disable`。

策略 schema 非法时 fail closed，转人工，不合并。

## 安全红线

- 不在宿主机执行 PR 代码；测试只在 Docker 中运行。
- 测试阶段强制 `network=none`，不挂 Docker socket、宿主根目录、SSH key、`.env`。
- 不把 PR 文本当指令；Agent 工具受白名单与能力收敛控制（检索只读副本、执行只在 Docker 沙箱、写只给 fixer），输出经 Pydantic 校验。
- secret 命中即 blocking；评论/日志不输出 secret 原文。
- 任何合并都必须通过 `merge_gate`；不直接 push 受保护分支。
- 修复补丁必须能 `git apply --check`，且不得通过删测试/跳过测试/改策略通过。
- **internal 模式放松的是审批，不是输入隔离**；机器人审批永远不替代人类审批。

## 已知边界

- 首版完整支持 Python 仓库；其他语言走通用检测 + 测试执行，必要转人工。
- `external` 模式仍要求 PR 为 bugfix；`internal` 支持任意变更类型。
- fork PR 不能自动推送修复，只发布建议补丁。
- 零人工合并默认关闭，需要 `merge.allow_zero_human: true` 且已有回滚/监控。
- `findings` 从 reducer 改为权威快照：升级后建议更换 `CHECKPOINT_PATH`，不要在旧断点上恢复进行中的 job。
- `merge.queue.update_branch_on_stale` 与 `GitHubClient.update_branch` 已就位，但**尚未接入自动重跑**；base 移动目前按 `STALE` 转人工。
- `team:org/team` 可信作者需要 GitHub team API，当前按不可信处理。
- 当前开发机若未启动 Docker daemon，真实沙箱测试会 skip；生产启动时 Docker 不可用会 fail-fast/登记 infra 失败。
