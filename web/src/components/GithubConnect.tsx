import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { errorText, useAsync } from "../hooks";
import type { ConsoleConfig, GithubAppStatus } from "../types";
import { CopyButton, Pill } from "./ui";
import { BoltIcon, ExternalIcon, RefreshIcon } from "./icons";

const TUNNEL_CMD = "cloudflared tunnel --url http://127.0.0.1:8002";
const NGROK_CMD = "ngrok http 8002";

function submitManifest(action: string, manifest: Record<string, unknown>) {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = action;
  form.style.display = "none";
  const field = document.createElement("input");
  field.type = "hidden";
  field.name = "manifest";
  field.value = JSON.stringify(manifest);
  form.appendChild(field);
  document.body.appendChild(form);
  form.submit();
}

function Step({
  index, title, hint, state, children,
}: {
  index: number; title: string; hint?: string;
  state: "done" | "active" | "todo";
  children?: React.ReactNode;
}) {
  return (
    <div className={`connect-step ${state}`}>
      <span className="step-num">{state === "done" ? "✓" : index}</span>
      <div className="step-body">
        <div className="step-title">
          {title}
          {state === "done" ? <Pill tone="ok">已完成</Pill> : null}
          {state === "active" ? <Pill tone="info">进行中</Pill> : null}
        </div>
        {hint ? <div className="step-hint">{hint}</div> : null}
        {state !== "done" ? children : null}
      </div>
    </div>
  );
}

export function GithubConnect({ config, onChanged }: { config: ConsoleConfig | null; onChanged: () => void }) {
  const statusQuery = useAsync(() => api.githubApp(), [], 15000);
  const status: GithubAppStatus | null = statusQuery.data?.status ?? config?.github_app ?? null;
  const [publicUrl, setPublicUrl] = useState("");
  const [name, setName] = useState("CodeGuard");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [banner, setBanner] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [confirmText, setConfirmText] = useState("");

  useEffect(() => {
    if (!publicUrl && config?.public_url) setPublicUrl(config.public_url);
  }, [config?.public_url, publicUrl]);

  useEffect(() => {
    if (window.location.hash.includes("github=connected")) {
      setBanner("GitHub App 已创建，继续完成安装即可选择仓库。");
      window.history.replaceState(null, "", `${window.location.pathname}#/settings`);
      onChanged();
    }
    // 只处理一次回跳提示
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const httpsReady = publicUrl.trim().startsWith("https://");
  const configured = !!status?.configured;
  const installed = (status?.installations ?? 0) > 0;
  const repoCount = status?.repos ?? 0;
  const authKind = config?.github_auth || status?.auth || "none";

  const headline = useMemo(() => {
    if (configured && installed) return `App ${status?.slug} 已安装，可审查 ${repoCount} 个仓库`;
    if (configured) return `App ${status?.slug} 已创建，等待安装到仓库`;
    if (authKind === "pat") return "当前使用 PAT，可继续用 App 获得细粒度授权";
    return "尚未接入 GitHub";
  }, [authKind, configured, installed, repoCount, status?.slug]);

  const createApp = () => {
    setError("");
    setBusy("manifest");
    api.githubAppManifest({ name: name.trim() || "CodeGuard", public_url: publicUrl.trim() })
      .then((result) => {
        submitManifest(result.action, result.manifest);
      })
      .catch((err) => {
        console.error(err);
        setError(errorText(err));
        setBusy("");
      });
  };

  const refresh = () => {
    setError("");
    setBusy("refresh");
    api.githubAppRefresh()
      .then(() => {
        statusQuery.reload();
        onChanged();
      })
      .catch((err) => {
        console.error(err);
        setError(errorText(err));
      })
      .finally(() => setBusy(""));
  };

  const disconnect = () => {
    setError("");
    setBusy("disconnect");
    api.githubAppDisconnect(confirmText.trim())
      .then(() => {
        setConfirming(false);
        setConfirmText("");
        setBanner("");
        statusQuery.reload();
        onChanged();
      })
      .catch((err) => {
        console.error(err);
        setError(errorText(err));
      })
      .finally(() => setBusy(""));
  };

  return (
    <div className="connect">
      <div className="connect-hero">
        <div className="connect-mark"><BoltIcon width={22} height={22} /></div>
        <div className="grow">
          <div className="connect-title">
            GitHub App
            <Pill tone={configured ? "ok" : authKind === "pat" ? "warn" : "muted"} dot>
              {configured ? "已接入" : authKind === "pat" ? "仅 PAT" : "未接入"}
            </Pill>
          </div>
          <div className="connect-sub">{headline}</div>
        </div>
        {configured ? (
          <button type="button" className="btn btn-sm" onClick={refresh} disabled={busy === "refresh"}>
            <RefreshIcon width={13} height={13} />
            {busy === "refresh" ? "同步中" : "同步安装"}
          </button>
        ) : null}
      </div>

      {banner ? <div className="alert info">{banner}</div> : null}
      {error ? <div className="alert error">{error}</div> : null}
      {status?.last_error && !error ? <div className="alert warn">{status.last_error}</div> : null}

      <div className="connect-grid">
        <Step
          index={1}
          title="暴露本机服务"
          hint="GitHub 需要公网 HTTPS 回调；App 的 webhook 与 manifest 回调共用这个地址。"
          state={httpsReady ? "done" : "active"}
        >
          <div className="field">
            <label htmlFor="cg-public-url">公网地址</label>
            <input
              id="cg-public-url"
              className="input"
              placeholder="https://your-tunnel.trycloudflare.com"
              value={publicUrl}
              onChange={(event) => setPublicUrl(event.target.value)}
            />
          </div>
          <div className="cmd-row">
            <code className="cmd">{TUNNEL_CMD}</code>
            <CopyButton text={TUNNEL_CMD} />
            <code className="cmd">{NGROK_CMD}</code>
            <CopyButton text={NGROK_CMD} />
          </div>
          <div className="step-hint">
            长期使用建议把地址写进 <span className="mono">CODEGUARD_PUBLIC_URL</span>，服务重启后 webhook 仍有效。
          </div>
        </Step>

        <Step
          index={2}
          title="创建 GitHub App"
          hint="在 GitHub 创建页提交预置的 manifest：权限、事件与 webhook 一次写完。"
          state={configured ? "done" : httpsReady ? "active" : "todo"}
        >
          <div className="field">
            <label htmlFor="cg-app-name">App 名称</label>
            <input
              id="cg-app-name"
              className="input"
              value={name}
              maxLength={60}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <button
            type="button"
            className="btn btn-primary"
            onClick={createApp}
            disabled={!httpsReady || busy === "manifest"}
            title={httpsReady ? "跳转 GitHub 创建页" : "先填写 https 公网地址"}
          >
            <ExternalIcon width={13} height={13} />
            {busy === "manifest" ? "正在跳转 GitHub" : "创建并注册 webhook"}
          </button>
        </Step>

        <Step
          index={3}
          title="安装到仓库"
          hint="只勾选需要审查的仓库；安装完成后回到这里点「同步安装」。"
          state={installed ? "done" : configured ? "active" : "todo"}
        >
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <a
              className={`btn btn-primary ${status?.install_url ? "" : "disabled"}`}
              href={status?.install_url || undefined}
              target="_blank"
              rel="noreferrer"
              aria-disabled={!status?.install_url}
            >
              <ExternalIcon width={13} height={13} />
              打开安装页
            </a>
            <button type="button" className="btn" onClick={refresh} disabled={busy === "refresh"}>
              <RefreshIcon width={13} height={13} />
              同步安装
            </button>
          </div>
        </Step>
      </div>

      {configured ? (
        <div className="connect-meta">
          <div>
            <span className="label">App</span>
            <span className="mono">{status?.slug || "—"}</span>
          </div>
          <div>
            <span className="label">installation</span>
            <span className="mono">{status?.installations ?? 0}</span>
          </div>
          <div>
            <span className="label">仓库</span>
            <span className="mono">{repoCount}</span>
          </div>
          <div>
            <span className="label">webhook</span>
            <span className="mono">{status?.webhook_configured ? "已注册" : "缺失"}</span>
          </div>
          <div className="grow">
            <span className="label">回调</span>
            <span className="mono">{status?.public_url ? `${status.public_url}/webhooks/github` : "—"}</span>
          </div>
          {!confirming ? (
            <button type="button" className="btn btn-sm btn-danger" onClick={() => setConfirming(true)}>
              断开连接
            </button>
          ) : (
            <div className="disconnect-box">
              <input
                className="input"
                placeholder={`输入 ${status?.slug} 确认`}
                value={confirmText}
                onChange={(event) => setConfirmText(event.target.value)}
              />
              <button
                type="button"
                className="btn btn-sm btn-danger"
                disabled={busy === "disconnect" || confirmText.trim() !== (status?.slug || "")}
                onClick={disconnect}
              >
                确认断开
              </button>
              <button type="button" className="btn btn-sm btn-ghost" onClick={() => setConfirming(false)}>
                取消
              </button>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
