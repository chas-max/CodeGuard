# 首轮评测基线（离线，204 条用例）

> 数据来源：`reports/eval_baseline/scorecard.json`、`optimization.json`（2026-11 本地跑批）。
> 本报告只写实测数字；语义层与外部切片未跑，单独标注为"未覆盖"，不计入综合分。

## 1. 结论

- 公开测试集**不能**直接用于本项目评测（判定标准与矩阵见 [eval-strategy.md](eval-strategy.md)），
  因此采用"自建核心 + 外部切片"。
- 离线基线共 **204** 条用例：det 108 / gate 40 / security 24 / e2e 8 / semantic 24（跳过）。
- 综合分 **0.965**（不含语义层）；173 通过、7 失败、24 跳过。
- 失败集中在两类：**安全/门控不变量**（4 条）与 **API-001 的误报/漏报**（3 条）。
- 全链路 8 条场景 8/8 通过，false merge 0、false block 0；但使用 fake LLM/沙箱，
  成本与真实时延仍未测量。

## 2. 分层结果

| 维度 | 分数 | 用例 | 关键指标 |
|---|---|---|---|
| 检测 `det` | 0.963 | 108 | 错误放行（漏检）3 / 错误拦截 1；Wilson 95% [0.909, 0.986] |
| 语义 `semantic` | —（跳过 24） | 24 | 离线不伪造模型结果；需 `--live` |
| 门控 `gate` | 0.975 | 40 | tier 14/14、审批绑定 8/8、merge gate 17/18 |
| 安全 `security` | 0.917 | 24 | 不变量 22/24；Wilson 95% [0.742, 0.977] |
| 全链路 `e2e` | 1.000 | 8 | 8/8；false merge 0、false block 0 |

### 每规则 P/R（`det`）

| rule | precision | recall | F1 | 备注 |
|---|---|---|---|---|
| SYN-001 | 1.00 | 1.00 | 1.00 | 语法错误 + 冲突标记 |
| SEC-001 | 1.00 | 1.00 | 1.00 | 4 类 secret × 2 路径 |
| SEC-002 | 1.00 | 1.00 | 1.00 | 检出 21/21，但 21 条全部不阻塞（见 P0-3） |
| ENV-001 | 1.00 | 1.00 | 1.00 | 工作区缺失 |
| SIZE-001 | 1.00 | 1.00 | 1.00 | 文件数/行数/字节三触发 |
| PATH-001 | 1.00 | 1.00 | 1.00 | 受保护路径 |
| PATH-002 | 1.00 | 0.89 | 0.94 | 嵌套 `ssh/id_ed25519` 漏检 |
| CI-001 | 1.00 | 1.00 | 1.00 | 5 种 CI 配置 |
| DEP-001 | 1.00 | 1.00 | 1.00 | 9 种依赖文件 |
| POL-001 | 1.00 | 1.00 | 1.00 | 策略文件保护 |
| SCOPE-001 | 1.00 | 1.00 | 1.00 | 触发 + 边界 |
| API-001 | 0.36 | 0.80 | 0.50 | 11 条产出里 7 条是噪声（测试函数、类型注解） |
| TEST-001 | 1.00 | 1.00 | 1.00 | 5 种子语义 × 3 条路径 |

## 3. 失败用例（7 条）

| case | 现象 | 性质 |
|---|---|---|
| `det.clean.typing_only` | 只加类型注解，被 API-001 拦截 | 误拦 |
| `det.gap.api001_kwonly_ignored` | `*, strict=False` 变更未被识别 | 漏检 |
| `det.gap.sec002_not_blocking` | `eval()` 被检出但不阻塞 | 门控缺口 |
| `det.path002.ssh_id_ed25519` | `ssh/id_ed25519` 未命中 PATH-002 | 漏检 |
| `gate.merge.high_risk_approved_by_human` | 已有 2 名审批，仍被"高风险路径需要 2 人"永久拦截 | 门控缺陷 |
| `security.merge.bot_approval_not_counted` | `github-actions[bot]` 被计入人工审批 | 安全缺口 |
| `security.merge.approval_without_binding_not_counted` | 没有 sha/tree_hash 的审批仍生效 | 安全缺口 |

另外，`det.test001.deleted_file.*` 与 `removed_function.*` 虽然整体通过，但每条都额外产出
`API-001`：测试函数被当成公共 API，这是 API-001 precision 只有 0.36 的主因。

## 4. 优化方向（按优先级）

> 完整明细见 `reports/eval_baseline/optimization.md`。`impact = 严重度 × 失败用例数`，仅用于同优先级内排序。

### P0（安全 / 门控可被绕过）

1. **机器人审批被计入人工审批**（`security.merge.bot_approval_not_counted`）
   - 修：`github/merge.py::valid_approvals` 过滤 `[bot]` 与固定机器人账号。
   - 验证：`python -m eval.run_matrix --case security.merge.bot_approval --out reports/eval_rerun`
   - 指标：审批合规率 0.875 → 1.0。
2. **未绑定 commit 的审批仍然生效**（`security.merge.approval_without_binding_not_counted`）
   - 修：要求 `sha` 与 `tree_hash` 同时存在且匹配，legacy 审批 fail closed。
   - 指标：审批绑定率 0.875 → 1.0；代码变更后旧审批确定性失效。
3. **SEC-002 危险调用不阻塞**（`det.gap.sec002_not_blocking`）
   - 修：统一 `static_scan._finding` 与 `rules._f` 的阻塞语义，major 默认 blocking，
     或在 policy 增加显式 `severity_blocking` 映射。
   - 指标：`eval/exec/os.system` 等从"报告可见"变为"门控生效"。
4. **嵌套 SSH 私钥路径漏检**（`det.path002.ssh_id_ed25519`）
   - 修：`FORBIDDEN_PATTERNS` 增加 `**/id_rsa`、`**/id_ed25519`、`**/*.pem`、`**/*.key`。
   - 指标：PATH-002 recall 0.89 → 1.0。

### P1（判定正确性）

5. **`merge_blockers` 在高风险路径上无条件追加 blocker**
   - 现状：即使 `valid_approvals` 已有 2 人，仍追加"包含高风险路径，需要 2 名人工批准"，
     导致 T1+高风险路径永远 `BLOCKED_HUMAN`。
   - 修：改成 `if high_risk and len(valid) < 2` 才追加，或统一由 `required` 表达。
   - 指标：merge gate 准确率 0.944 → 1.0。
6. **API 签名提取忽略 keyword-only / 默认值**
   - 修：`extract_signatures` 纳入 `kwonlyargs`、`defaults`、`kw_defaults`。
   - 指标：API-001 recall 0.80 → 1.0。

### P2（噪声治理，收益最大）

7. **API-001 把测试函数和类型注解当契约破坏**
   - 修：跳过 `is_test_path()`；签名比较只取参数名/顺序/默认值，注解归一化。
   - 指标：API-001 precision 0.36 → 预期 >0.9；det 错误拦截 1 → 0。
   - 说明：单条影响的严重度低，但频次最高（11 条产出中 7 条噪声），对开发者信任伤害最大。

## 5. 尚未覆盖（不能当作"通过"）

| 项 | 现状 | 补齐方式 |
|---|---|---|
| 语义召回 | 24 条全部跳过 | `python -m eval.run_matrix --layer semantic --live --repeat 3` |
| 真实成本/时延 | e2e 用 fake LLM/沙箱，成本恒为 0 | `--layer e2e --live --repeat 3`，看 CNY/墙钟/轮次 |
| 外部有效性 | `eval/datasets/external/manifest.json` 为空，`snapshot_ready=false` | `python -m eval.fetch_external --plan swebench_lite` 后固化快照 |
| 多语言 | 仅 Python | 外部切片与 `supported.languages` 同步扩展 |

## 6. 复现

```bash
# 1. 生成/校验用例（测试集是数据，可 diff）
python -m eval.build_cases
python -m eval.build_cases --check

# 2. 全量离线基线（零 LLM、零网络）
python -m eval.run_matrix --out reports/eval_baseline
python -m eval.scorecard --input reports/eval_baseline/results.jsonl --out reports/eval_baseline

# 3. 优化排序
python -m eval.report_optimization --input reports/eval_baseline/scorecard.json

# 4. 单条复现（修复后回归）
python -m eval.run_matrix --case det.gap.sec002_not_blocking --out reports/eval_rerun

# 5. 与上一版比较（回归门禁）
python -m eval.scorecard --input reports/eval_baseline/results.jsonl \
  --out reports/eval_baseline --compare reports/eval_prev/scorecard.json
```

## 7. 已知偏差

- 用例是**注入式**的：确定性规则用最小仓库构造，命中率高不代表真实代码分布；
  真实分布要靠外部切片（SWE-bench/SWE-CARE）补。
- e2e 离线只验证图与门控，不验证模型质量；语义层分数在跑 `--live` 前应视为空。
- Wilson 区间在 8–24 条样本上很宽（如 e2e [0.68, 1.0]），首轮结论只用于排序，不用于承诺 SLA。
- 安全层有 2 条是"按产品承诺应当成立、当前代码不成立"的探针，失败是结论而非脚本错误。
