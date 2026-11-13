import { useState } from "react";
import { api } from "../api";
import type { McpStatusResponse } from "../types";
import { useAsync } from "../hooks";
import { Alert, KeyValue, Pill, SubCard } from "../components/ui";
import { GithubConnect } from "../components/GithubConnect";
import { RefreshIcon } from "../components/icons";

const ENDPOINTS = [
  ["GET /api/config", "运行配置与能力开关（只读）"],
  ["GET /api/mcp/status", "MCP Server 列表、工具数与连通性状态"],
  ["GET /api/repos", "可切换仓库列表（GitHub 或 CODEGUARD_REPOS）"],
  ["GET /api/pulls?repo=&state=", "PR 列表，附最近一次审查任务"],
  ["POST /api/reviews", "按 repo + pr_number 触发审查"],
  ["GET /api/jobs", "活动任务 + reports/jobs 历史记录"],
  ["GET /api/jobs/{id}", "完整 ReviewState 快照（含 interrupt）"],
  ["GET /api/jobs/{id}/trace", "由 checkpoint 历史还原的节点时间线"],
  ["GET /api/jobs/{id}/stream", "SSE：progress / 节点事件 / interrupt / done"],
  ["POST /api/jobs/{id}/resume", "HITL 回答：approve / reject / finding_outcomes"],
  ["GET|POST /api/watch", "定时查看配置与手动轮询"],
  ["GET /api/github/app", "GitHub App 状态（掩码，不含私钥）"],
  ["POST /api/github/app/manifest", "生成 manifest 与一次性 state，跳 GitHub 创建页"],
  ["GET /api/github/app/callback", "GitHub 回调：换凭据并热加载"],
  ["POST /api/github/app/refresh", "同步 installation 与仓库缓存"],
  ["DELETE /api/github/app", "断开连接（需输入 App slug 确认）"],
];

export function SettingsPage() {
  const config = useAsync(() => api.config(), []);
  const health = useAsync(() => api.health(), [], 15000);
  const mcp = useAsync(() => api.mcpStatus(false), []);
  const [probing, setProbing] = useState(false);
  const [probed, setProbed] = useState<McpStatusResponse | null>(null);
  const [probeError, setProbeError] = useState("");
  const cfg = config.data;
  const h = health.data;
  const mcpData = probed || mcp.data;

  async function handleLiveProbe() {
    setProbing(true);
    setProbeError("");
    try {
      const res = await api.mcpStatus(true);
      setProbed(res);
    } catch (err: any) {
      setProbeError(err?.message || "MCP 实时探测失败");
    } finally {
      setProbing(false);
    }
  }

  return (
    <div className="section">
      <div className="page-head">
        <div className="grow">
          <h1>设置与健康</h1>
          <p>控制台只读展示后端配置；策略收紧仍以服务端环境变量与 <span className="mono">.codeguard/policy.yml</span> 为准。</p>
        </div>
        <button type="button" className="btn" onClick={() => { config.reload(); health.reload(); mcp.reload(); }}>
          <RefreshIcon width={14} height={14} /> 刷新
        </button>
      </div>

      {config.error ? <Alert tone="error">{config.error}</Alert> : null}

      <div className="card connect-card">
        <div className="card-head">
          <h2>GitHub 接入</h2>
          <span className="spacer" />
          <span style={{ fontSize: 11.5, color: "var(--muted)" }}>
            凭据只落在服务端 data/github-app.json（0600），接口仅返回掩码状态
          </span>
        </div>
        <div className="card-body">
          <GithubConnect
            config={cfg}
            onChanged={() => { config.reload(); health.reload(); }}
          />
        </div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <div className="card-head"><h2>后端能力</h2></div>
          <div className="card-body">
            {cfg ? (
              <KeyValue
                items={[
                  [
                    "GitHub 接入",
                    cfg.github_auth === "app"
                      ? <Pill tone="ok">App · {cfg.github_app.slug || "connected"}</Pill>
                      : cfg.github_auth === "pat"
                        ? <Pill tone="warn">PAT</Pill>
                        : <Pill tone="muted">未接入</Pill>,
                  ],
                  ["登录身份", cfg.github_login || "—"],
                  ["公网地址", cfg.public_url || "—"],
                  ["installation / 仓库", `${cfg.github_app.installations} / ${cfg.github_app.repos}`],
                  ["合并模式", <Pill tone={cfg.merge_mode === "auto" ? "ok" : "warn"}>{cfg.merge_mode}</Pill>],
                  ["合并方式", cfg.merge_method],
                  ["审查模式", cfg.review_mode],
                  ["沙箱后端", <Pill tone={cfg.sandbox_backend === "docker" ? "ok" : "violet"}>{cfg.sandbox_backend}</Pill>],
                  ["预算上限", `¥${cfg.budget_max_cny}`],
                  ["审查 / 修复轮次", `${cfg.max_review_rounds} / ${cfg.max_fix_rounds}`],
                  ["预置仓库", cfg.default_repos.length ? cfg.default_repos.join(", ") : "—"],
                ]}
              />
            ) : null}
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <h2>服务健康</h2>
            <span style={{ marginLeft: "auto" }} />
            {h ? <Pill tone={h.ok ? "ok" : "danger"} dot>{h.ok ? "在线" : "异常"}</Pill> : null}
          </div>
          <div className="card-body">
            {h ? (
              <KeyValue
                items={[
                  ["ok", String(h.ok)],
                  ["GitHub", h.github_configured ? "已配置" : "未配置"],
                  ["沙箱", String(h.sandbox_backend)],
                  ["Docker", `${h.docker?.ok ? "可用" : "不可用"} · ${h.docker?.message ?? ""}`],
                  ["沙箱错误", h.sandbox_error || "无"],
                ]}
              />
            ) : null}
            {h && !h.docker?.ok && h.sandbox_backend === "docker" ? (
              <div style={{ marginTop: 10 }}>
                <Alert tone="warn">Docker 不可用时任务会判 INFRA_FAILED，不会静默放行合并。</Alert>
              </div>
            ) : null}
          </div>
        </div>
      </div>

      <SubCard
        title="MCP Client 与外部工具源"
        extra={(
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {mcpData ? (
              <Pill tone={mcpData.enabled ? "ok" : "muted"}>
                {mcpData.enabled ? `已启用 · 协议 ${mcpData.tool_protocol}` : "已关闭"}
              </Pill>
            ) : null}
            <button type="button" className="btn" disabled={probing} onClick={handleLiveProbe}>
              {probing ? "探测中..." : "实时探测"}
            </button>
          </div>
        )}
      >
        {probeError ? <Alert tone="error">{probeError}</Alert> : null}
        {mcpData ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Server</th>
                  <th>传输</th>
                  <th>能力上限</th>
                  <th>允许角色</th>
                  <th>工具数</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {mcpData.servers.map((srv) => (
                  <tr key={srv.name} style={{ cursor: "default" }}>
                    <td className="mono" style={{ fontSize: 12 }}>{srv.name}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>{srv.transport}</td>
                    <td><Pill tone="muted">{srv.capability}</Pill></td>
                    <td style={{ fontSize: 11.5, color: "var(--text-dim)" }}>
                      {srv.allowed_roles.length ? srv.allowed_roles.join(", ") : "全部"}
                    </td>
                    <td className="mono" style={{ fontSize: 12 }}>{srv.tools_count}</td>
                    <td>
                      {srv.ok
                        ? <Pill tone="ok">就绪 ({srv.latency_ms}ms)</Pill>
                        : <Pill tone={srv.enabled ? "danger" : "muted"}>{srv.error || "离线"}</Pill>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </SubCard>

      <SubCard title="控制台接口（前端即按此扩展）">
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>接口</th><th>说明</th></tr>
            </thead>
            <tbody>
              {ENDPOINTS.map(([path, desc]) => (
                <tr key={path} style={{ cursor: "default" }}>
                  <td className="mono" style={{ color: "var(--accent)", fontSize: 11.5 }}>{path}</td>
                  <td style={{ color: "var(--text-dim)" }}>{desc}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </SubCard>

      <SubCard title="不可绕过的确定性边界">
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: "var(--text-dim)", lineHeight: 1.9 }}>
          <li>合并只由 <span className="mono">merge_blockers</span> 决定，前端无法直接触发 merge。</li>
          <li>人工审批绑定 head SHA + 树哈希，代码变动后自动失效。</li>
          <li>HITL 的 finding 处置只影响 blocking 状态，不会替代沙箱与审查结论。</li>
          <li>追踪与事件均为只读旁路，失败不影响主流程终态。</li>
        </ul>
      </SubCard>
    </div>
  );
}
