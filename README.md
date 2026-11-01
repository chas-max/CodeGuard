# CodeGuard-Agent · 基于 LangGraph 与沙箱自愈的 Repo 自动化 Bug 修复与单测生成 Multi-Agent 系统

> 本项目由 AI 漫剧多 Agent 制作系统（12 节点 LangGraph 状态机）**同构迁移改造**而来，
> 保留 12 节点图拓扑、Keyed Reducers 幂等更新、SqliteSaver 三处 HITL 中断恢复、
> 异常统筹节点、成本熔断与 SSE 事件流等工程底座，将"漫剧制作流水线"替换为
> **"异常发现 → 方案规划 → 执行处理 → 结果检测"** 的 Bug 修复四能力闭环。
> 改造设计文档见 [PATH_A_CODING_AGENT_PLAN.md](PATH_A_CODING_AGENT_PLAN.md)。

## 四能力闭环

| 能力 | 承载节点/模块 | 机制 |
|---|---|---|
| ① 自动发现异常 | `machine_review` + `sandbox_execute` | 确定性静态门控（AST 语法/高危调用/冻结签名契约，毫秒级）+ 沙箱 F2P/P2P 测试失败与 Traceback 捕获 |
| ② 规划处理方案 | `director_plan` + `director_resolve` | AST 调用图两遍扫描 → 词法+符号+1-hop 调用链传播定位 Top-K 故障文件 → 冻结符号契约圣经（Symbol Contract Bible）→ 异常统筹路由 retry/downgrade/skip/escalate |
| ③ 执行处理 | `write_draft` + `sandbox_execute` | Search-Plan-Execute-Reflect 四阶段补丁编写 + 逐槽位 ReAct 沙箱自愈（Act→Observe→Think→Retry，Traceback 驱动定向重写） |
| ④ 检测处理结果 | 沙箱三重门控 | **F2P 复现测试（Red 锚定态先行验证）+ P2P 回归测试 + 分支覆盖率** 全确定性判定，交付零退化 `fix.patch`（可 `git apply`，含新增单测） |

## 12 节点状态流转

```
understand(故障解析) → clarify_ask(HITL#1 澄清) → director_plan(AST定位+契约冻结)
→ write_draft(四阶段补丁) → machine_review(静态门控)
  ├─ 不通过 → write_draft（封顶 3 次）
  ├─ 工具级异常 → director_resolve（异常统筹，封顶 3 次硬检查）
  └─ 通过 → director_supervise(Tech Lead 两阶段自省) → human_review(HITL#2 Diff 预览)
→ build_anchors(生成 F2P 复现测试, 沙箱验证 Red 态) → confirm_anchors(HITL#3 确认锚点)
→ sandbox_execute(逐槽位 ReAct 沙箱自愈 + 三重门控)
  ├─ F2P全过 + P2P零退化 → END（fix.patch + 覆盖率报告）
  ├─ 未超上限 → sandbox_execute（封顶 2 轮）
  └─ 沙箱环境异常 → director_resolve
```

6 个独立防死循环计数器（clarify/machine/human/anchor/prod/resolve）+ `budget.py` 成本熔断，
超限统一走 `escalate` 确定性终止并保留产物。

## 评测基准：Micro-SWE-Bench（50 题确定性沙箱）

**以下数字由 `python -m eval.run_benchmark --suite 50 --out reports/` 真实跑测产出**
（seed=20260101，11 类 Bug 模板 × seeded 变分，全程零 LLM 调用、完全可复现）：

| 指标 | Baseline(单轮) | +AST定位(单轮) | +AST定位+多轮自愈 |
|---|---|---|---|
| Localization Recall@3 | 0% | **100%** | 100% |
| One-shot Pass@1 | 64% | 64% | 64% |
| **Resolved Rate** | 64% | 64% | **100%（自愈净增益 +36%）** |
| P2P Regression Rate | 18% | 18% | **0%（零退化交付）** |

详细分类统计见 [reports/benchmark_report.md](reports/benchmark_report.md)。

## 快速开始

```bash
# 1. 配置
copy .env.example .env   # 填入 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY

# 2. Web UI（Gradio，端口 7861）
python codeguard_app.py

# 3. CLI
python codeguard_app.py --cli --repo D:\path\to\buggy_repo --issue "describe the bug or paste traceback"

# 4. FastAPI + SSE 服务（端口 8001）
python -m codeguard.server
#   POST /api/jobs        {"issue": "...", "repo_dir": "..."}
#   GET  /api/jobs/{id}          # 轮询快照
#   GET  /api/jobs/{id}/stream   # SSE 实时事件
#   POST /api/jobs/{id}/resume   # HITL 恢复（clarify/human_review/confirm_anchors）

# 5. 运行评测基准
python -m eval.run_benchmark --suite 50 --out reports/

# 6. 测试（全部 mock，无需 API key；沙箱类测试真实跑 pytest 子进程）
python -m pytest tests/ -v
```

**交付产物**（`data/codeguard_output/<job_id>/`）：
- `fix.patch` — 标准 Unified Diff，可直接 `git apply`（代码修复 + 新增 F2P 单测）
- `coverage_report.md` — 分支覆盖率与门控记录
- `sandbox_log.md` — 逐槽位自愈过程与 Traceback 审计

## 工程底座（自漫剧系统同构保留）

| 底座 | 实现 |
|---|---|
| 状态图编排 | LangGraph `StateGraph`，12 节点 + 7 条件路由，节点签名 `fn(state, ctx)` 经 `functools.partial` 注入容器 |
| 幂等更新 | `merge_patch_units`（按 unit_id）/ `merge_symbol_contracts`（按 symbol_id）Keyed Reducers，支持断点恢复与单槽位增量重试 |
| HITL 中断 | `langgraph.types.interrupt` × 3（澄清/Diff审查/锚点确认），`SqliteSaver` 持久化断点，进程重启可恢复 |
| 异常统筹 | `open_issues` 覆盖式路由信号 + `agent_issues` append-only 审计日志（修复了原系统 append reducer 无法清空导致的 resolve 死循环） |
| 成本治理 | `track_usage` token 采集 → `budget_events` → `budget_max_cny` 熔断；6 计数器独立封顶 |
| 可观测 | SSE 事件流（progress/issue_parsed/plan_ready/static_gate_done/anchors_ready/unit_progress/patch_done/interrupt/error）+ 实时进度 |
| 稳定性 | LLM 请求 `request_timeout`（防单请求挂起）、沙箱 `timeout=30s`（防死循环）、后台任务异常回调（防 job 静默卡死） |

## 技术栈

Python 3.11+ · LangGraph 1.x（`StateGraph`/`SqliteSaver`/`interrupt`）· Pydantic v2 ·
标准库 `ast`（符号契约/调用图）· pytest & pytest-cov（沙箱三重门控）·
DeepSeek/Qwen/MiMo（OpenAI 兼容，角色化路由）· FastAPI + SSE · Gradio

## 目录结构

```
├── codeguard_app.py                  # 统一入口：Gradio UI / CLI
├── eval/
│   ├── micro_swe_bench.py            # 11 类 Bug 模板 × seeded 变分 = 50 题确定性套件
│   └── run_benchmark.py              # 评测 CLI：三组消融，落盘 JSON+Markdown
├── src/codeguard/
│   ├── coding_agent/                 # 四能力核心引擎
│   │   ├── models.py                 #   强类型模型 + Keyed Reducers
│   │   ├── ast_analyzer.py           #   AST 符号提取 + Top-K 故障文件定位
│   │   ├── static_guard.py           #   确定性静态门控（语法/高危调用/冻结签名）
│   │   ├── patch_writer.py           #   上下文裁剪 + Unified Diff 合成
│   │   └── sandbox_runner.py         #   隔离沙箱 F2P/P2P/覆盖率 + Red 验证 + 宿主隔离校验
│   ├── nodes/pipeline_nodes.py       # 12 节点 + 7 条件路由
│   ├── prompts/                      # 各 Agent 提示词（纯函数）
│   ├── graph.py / state.py / runtime.py / server.py / config.py / llm.py
│   └── budget.py / errors.py / retry.py
├── tests/                            # 57 项测试（FakeLLM mock + 真实沙箱子进程）
└── reports/benchmark_report.{json,md}
```

## 安全红线

- 沙箱执行在 `tempfile.TemporaryDirectory` 隔离副本中进行，宿主仓库经全树哈希前后校验，绝无污染
- 生成的补丁代码静态拦截 `eval` / `exec` / `os.system` / `shutil.rmtree` 等高危调用
- 冻结符号契约：公共函数签名一经 `director_plan` 冻结，任何破坏签名的补丁在静态门控即被拦截
- 所有评测指标真实跑测落盘，无任何硬编码数字
