# 控制台（Web 前端）

面向操作者的 PR 审查控制台：选仓库、看 PR、触发审查、实时追踪 LangGraph 全链路、回答 HITL。

前端代码在 `web/`（Vite + React + TypeScript，无 UI 框架依赖），构建产物由 FastAPI 挂在 `/ui`。

## 运行

```bash
# 生产/联调：先构建，再由后端托管
cd web && npm install && npm run build
cd .. && python codeguard_app.py serve --port 8002
# 打开 http://127.0.0.1:8002/ui/

# 开发态（热更新，vite 代理 /api 到 8002）
cd web && npm run dev   # http://127.0.0.1:5173
```

`WEB_DIST` 可以覆盖前端产物目录；目录不存在时不挂载 `/ui`，后端接口不受影响。

后端可选配置：`CODEGUARD_REPOS=owner/a,owner/b`（无 token 时前端仍能切换仓库）。

## 界面能力

| 视图 | 能力 |
| --- | --- |
| PR 看板 | 仓库切换（GitHub 仓库列表 / 最近访问 / 手动输入）、PR 列表与最近任务状态、一键入队审查、定时查看开关 |
| 任务 | 活动任务 + `reports/jobs` 历史记录，按仓库/状态筛选，成本与 findings 汇总 |
| 任务详情 | 11 节点流程链、节点时间线（耗时/摘要/变更键）、各节点结果面板、原始状态、SSE 实时事件 |
| HITL | 门控原因、审查与沙箱结论、blocking findings 逐条处置、审批人/反馈、通过或驳回 |
| 定时查看 | 创建/调整/删除轮询任务，倒计时、上次轮询、发现列表（head 变化、是否已入队） |
| 设置与健康 | GitHub App 三步接入向导（公网地址 / 创建 App / 安装仓库 / 断开）、后端能力、Docker/GitHub 状态、接口清单与确定性边界说明 |

### 全链路追踪怎么来的

`GET /api/jobs/{id}/trace` 读取 LangGraph 的 checkpoint 历史（`graph.get_state_history`），
把「上一个快照的 `next` + 下一个快照的时间」还原为节点步骤：

- 节点名、耗时、开始/结束时间、后续节点；
- 本节点写入的 state 键（对比前后快照，跳过 `diff_text` 等大字段）；
- 一句话摘要（`trace.node_summary`，例如 `tier=T1 · profile=bugfix · 文件 2`）。

追踪是**只读旁路**：读不到历史时降级为空轨迹，不影响任务本身。

### HITL 的约束

前端只能调用 `POST /api/jobs/{id}/resume`，且必须：

- 通过审批要填审批人（后端校验，机器人评论不会替代人工批准）；
- 审批绑定 `head_sha + tree_hash`，代码变动后自动失效；
- `finding_outcomes` 只能把 blocking finding 标记为 `resolved / accepted_risk / false_positive`，
  不能绕过 `validate` 与 `merge_blockers`。

## 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/config` | 合并模式、沙箱、预算、预置仓库等只读配置 |
| `GET /api/repos?q=` | 仓库列表（App installation → PAT `/user/repos` → 回退 `CODEGUARD_REPOS`，`source` 分别为 `app` / `github` / `config`） |
| `GET /api/pulls?repo=&state=` | PR 列表，附最近一次任务（`job` 字段） |
| `POST /api/reviews` | `{repo, pr_number, ...}` 触发审查，返回 `job_id` |
| `GET /api/jobs?repo=&limit=` | 活动 + 历史任务 |
| `GET /api/jobs/{id}` | ReviewState 快照 + `interrupt` |
| `GET /api/jobs/{id}/trace` | 节点时间线 |
| `GET /api/jobs/{id}/stream` | SSE：`progress` / 节点事件 / `interrupt` / `done` / `error` |
| `POST /api/jobs/{id}/resume` | HITL 回答 |
| `GET/POST /api/watch`、`PATCH/DELETE /api/watch/{id}`、`POST /api/watch/{id}/poll` | 定时查看 |
| `GET /api/github/app` | App 接入状态（掩码） |
| `POST /api/github/app/manifest` | 生成 manifest 与一次性 state |
| `GET /api/github/app/callback` | GitHub 回调：换凭据并热加载 |
| `POST /api/github/app/refresh` | 同步 installation 与仓库缓存 |
| `DELETE /api/github/app` | 断开连接（需输入 App slug 确认） |

## 定时轮询

`console.py::ConsoleAPI` 用 `asyncio` 任务按 `interval_sec`（30s–24h）拉取 PR：

- 记录每个 PR 的 `head_sha`，只有变化才算「新提交」；
- `auto_review=true` 时每次轮询最多入队 3 个，跳过 draft/已合并/已在审查的 PR；
- 配置与已知 head 持久化在 `<DATA_DIR>/watchers.json`，重启后恢复并立即同步一次；
- 轮询失败只写 `last_error`，不会影响 webhook/审查主流程。

## 扩展指引

1. 新增后端能力：在 `src/codeguard/console.py` 的 `_register_routes` 里加路由（依赖通过 `ConsoleAPI` 构造参数注入，便于测试替身）。
2. 新增前端页面：`web/src/pages/*.tsx` + `web/src/router.ts` 增加一条解析规则 + `App.tsx` 的侧边栏导航。
3. 新增节点结果展示：`web/src/components/NodeResult.tsx` 按 `node` 分支渲染，新 state 键记得先在 `state.py::ReviewState` 声明。
4. 接口类型：`web/src/types.ts` 与 `web/src/api.ts` 同步补字段。

## 测试

```bash
python -m pytest tests/test_console_api.py tests/test_trace.py -q
cd web && npm run build   # 含 tsc 类型检查
```
