# GitHub App 接入

CodeGuard 推荐用 GitHub App 而不是 PAT：权限按仓库授权、installation token 1 小时自动续期、
webhook 在创建 App 时自动注册，凭据只落服务端。

## 前置条件

1. 服务已启动：`python codeguard_app.py serve --port 8002`
2. 一个公网 HTTPS 地址指向本机（本机/内网必须内网穿透）：

```bash
# Cloudflare Tunnel（免费、临时域名）
cloudflared tunnel --url http://127.0.0.1:8002

# 或 ngrok
ngrok http 8002
```

把输出的 `https://xxx.trycloudflare.com` 填到设置页，或写进 `.env`：

```dotenv
CODEGUARD_PUBLIC_URL=https://xxx.trycloudflare.com
```

> GitHub 的 manifest 回调与 webhook 都要求公网 HTTPS。`http://` 或 `localhost` 一律拒绝。

## 三步接入

1. 打开 `http://127.0.0.1:8002/ui/#/settings`，在「GitHub 接入」卡片第 1 步填入公网地址。
2. 第 2 步点「创建并注册 webhook」→ 跳转 GitHub 创建页并提交预置 manifest。
3. 创建完成后点「打开安装页」，只勾选需要审查的仓库；回到控制台点「同步安装」。

状态从「未接入」变为「已接入」后，仓库选择器会直接列出 installation 内的仓库（来源标注 `App`）。

## manifest 预置内容

| 项 | 值 |
| --- | --- |
| Webhook URL | `<public_url>/webhooks/github` |
| Redirect URL | `<public_url>/api/github/app/callback` |
| 事件 | 只声明 `pull_request` |
| 权限 | `contents: write`、`pull_requests: write`、`checks: write`、`statuses: write`、`issues: write`、`metadata: read` |

> `installation` 与 `installation_repositories` **不能写进 manifest 的 `default_events`**，
> 否则 GitHub 会报 `Default events unsupported` / `Default events are not supported by permissions`。
> 这两个事件由 GitHub 自动投递给 App，服务端的 webhook 仍然会收到并刷新 installation 缓存。

`contents: write` 用于克隆与推送修复分支；`pull_requests: write` 用于 review / inline 评论 / 合并；
`statuses: write` 用于提交状态；`issues: write` 用于 issue 评论兜底。

## 凭据与热生效

- 凭据文件：`data/github-app.json`（原子写、权限 0600，已被 `.gitignore` 覆盖）。
- 内容：`app_id`、`slug`、`private_key`(PEM)、`webhook_secret`、`client_id/client_secret`、`html_url`。
- 接口只返回 `configured / slug / installations / repos / webhook_configured` 等掩码状态，
  **绝不回传 PEM 或 webhook secret**。
- 连接成功后无需重启：`CodeGuardRuntime.refresh_github()` 会重建 `GitHubClient` 的 token provider。

## 鉴权链路

```
private key → RS256 JWT（iat-60s，exp+540s，iss=app_id）
  → POST /app/installations/{id}/access_tokens
  → installation token（1h，提前 60s 视为过期）
```

- 401 时 `GitHubClient` 调 `GitHubAppAuth.invalidate(repo)` 后**只重试一次**。
- 仓库 → installation 映射缓存 60 秒；`installation` / `installation_repositories` 事件到来时失效并后台刷新。
- App JWT 没有用户身份，`/api/config` 的 `github_login` 在 App 模式下展示 App slug。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `GITHUB_AUTH_MODE` | `auto`（默认，App 优先）/ `pat` / `app` |
| `CODEGUARD_PUBLIC_URL` | 公网 HTTPS 地址，manifest 与 webhook 依赖 |
| `GITHUB_APP_STORE` | 凭据路径，默认 `data/github-app.json` |
| `CODEGUARD_ADMIN_TOKEN` | 非本机调用 App 写接口时的管理令牌（`X-CodeGuard-Admin`） |

`GITHUB_TOKEN` 仍然可用：CLI、离线评测与单测走 PAT 路径，不受 App 影响。

## 安全边界

- `POST /api/github/app/manifest`、`POST /api/github/app/refresh`、`DELETE /api/github/app`
  默认只允许本机调用；远程调用需要 `X-CodeGuard-Admin` 与 `CODEGUARD_ADMIN_TOKEN` 一致。
- manifest 使用一次性 `state`（10 分钟 TTL），重放直接 403。
- 断开连接需要输入 App slug 确认；断开后回到未接入状态，PAT 配置不受影响。
- 已接入 App 时，`/webhooks/github` 只接受
  `X-GitHub-Hook-Installation-Target-Type: integration` 的投递，避免仓库级 webhook 冒充 App 事件。
- 克隆失败信息会脱敏 `x-access-token:***@` 与 `gh?_***`，不会把 installation token 写进 job report。

## 排障

| 现象 | 原因与处理 |
| --- | --- |
| 点创建后 GitHub 报 `redirect_uri mismatch` | `CODEGUARD_PUBLIC_URL` 与实际隧道地址不一致，重新填地址再创建 |
| 回调 403 `state 无效或已过期` | 超过 10 分钟或服务重启；重新发起创建 |
| 回调 502 `manifest 交换失败` | `code` 已被使用（一次性），重新发起创建 |
| 仓库列表为空 | 安装时没勾选仓库，或 installation 被挂起；到 GitHub App 设置里调整后点「同步安装」 |
| webhook 401 `未配置 GitHub webhook secret` | App 未接入且 `GITHUB_WEBHOOK_SECRET` 为空；先完成接入 |
| 拉 PR 报 403 `Resource not accessible by integration` | 权限不足或 installation 未覆盖该仓库，检查安装范围后重装 |
| 隧道重启导致域名变化 | 临时隧道域名会变；生产请用固定域名并更新 `CODEGUARD_PUBLIC_URL`，再改 App 的 Webhook URL |
