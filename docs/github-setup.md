# GitHub 接入

## GitHub App（推荐）

控制台「设置与健康 → GitHub 接入」提供三步向导：暴露公网地址 → 创建 App（自动注册 webhook）
→ 安装到仓库。凭据落 `data/github-app.json`，installation token 自动续期，无需手工配 webhook。

```bash
python codeguard_app.py serve --port 8002
cloudflared tunnel --url http://127.0.0.1:8002   # 或 ngrok http 8002
# 打开 http://127.0.0.1:8002/ui/#/settings
```

完整权限清单、环境变量与排障见 [github-app.md](github-app.md)。

## PAT（离线 / CLI / 单仓 MVP）

1. 创建 token，至少具备 repo / pull_request / contents 权限；需要 Check Run 时给 checks/statuses 权限。
2. 配置 `GITHUB_TOKEN`。
3. 启动 `python codeguard_app.py serve`，配置 webhook：
   - Payload URL：`https://<host>/webhooks/github`
   - Content type：`application/json`
   - Secret：与 `GITHUB_WEBHOOK_SECRET` 一致
   - Events：Pull requests

## 手动触发

```bash
curl -X POST http://127.0.0.1:8002/api/reviews \
  -H "Content-Type: application/json" \
  -d '{"repo":"owner/name","pr_number":123}'
```

## 人工审批

结论以 PR review（event=`COMMENT`）+ 逐条 inline 评论发布；nit 只进摘要。
审批通过 `resume` 提交，一条审批绑定当前 `head_sha` 与工作区树哈希：

```bash
curl -X POST http://127.0.0.1:8002/api/jobs/<job_id>/resume \
  -H "Content-Type: application/json" \
  -d '{"approved":true,"approver":"alice","feedback":"","finding_outcomes":{"<finding_id>":"fixed"}}'
```

- 未凑够 tier 要求的人数时，任务会再次进入 `human_approval`；
- `finding_outcomes` 记录每条 finding 的人工判定（fixed / dismissed / intentional），用于 `codeguard stats` 的 precision 口径。

## 合并

- 通过 PR merge API 合并，带 `sha` 守卫；不使用直接 push。
- 合并前重新校验 PR open、非 draft、mergeable、必需检查、分支保护、`head_sha` 与审查时一致、审批未过期。
- tier 要求人工时先审批；`MERGE_MODE=human` 可全局收紧。
- 高风险路径或 tier=T2 需要 2 名审批（含至少一名非作者）。
- fork PR 不能自动推送修复，只发布建议补丁评论。
