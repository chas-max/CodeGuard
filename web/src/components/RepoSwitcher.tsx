import { useMemo, useState } from "react";
import { api } from "../api";
import { useAsync, useRecentRepos } from "../hooks";
import { navigate } from "../router";
import { ChevronDownIcon, SearchIcon, ExternalIcon } from "./icons";
import { Alert, Empty, Pill, Spinner } from "./ui";

export function RepoSwitcher({ repo, onSelect }: { repo: string; onSelect: (value: string) => void }) {
  const [open, setOpen] = useState(false);
  const owner = repo ? repo.split("/")[0] : "";

  return (
    <>
      <button type="button" className="repo-switch" onClick={() => setOpen(true)}>
        <div style={{ minWidth: 0 }}>
          <div className="repo-name">{repo || "选择仓库"}</div>
          <div className="repo-hint">{repo ? (owner ? `owner · ${owner}` : "github") : "未选择 · 点击切换"}</div>
        </div>
        <ChevronDownIcon className="chevron" width={15} height={15} />
      </button>
      {open ? (
        <RepoPicker
          current={repo}
          onClose={() => setOpen(false)}
          onPick={(value) => {
            onSelect(value);
            setOpen(false);
          }}
        />
      ) : null}
    </>
  );
}

export function RepoPicker({
  current, onClose, onPick,
}: { current: string; onClose: () => void; onPick: (value: string) => void }) {
  const [query, setQuery] = useState("");
  const [manual, setManual] = useState("");
  const recent = useRecentRepos();
  const { data, error, loading, reload } = useAsync(() => api.repos(), []);
  const repos = data?.repos ?? [];

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return repos;
    return repos.filter(
      (item) =>
        item.full_name.toLowerCase().includes(needle) ||
        (item.description || "").toLowerCase().includes(needle),
    );
  }, [repos, query]);

  return (
    <div className="overlay" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <h3>切换 GitHub 仓库</h3>
          {data?.source === "app" ? (
            <Pill tone="ok">App · {data.app_slug || "connected"}</Pill>
          ) : null}
          <span className="spacer" style={{ flex: 1 }} />
          <button type="button" className="btn btn-sm btn-ghost" onClick={reload}>
            刷新
          </button>
        </div>
        <div className="modal-body" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ position: "relative" }}>
            <SearchIcon
              width={15}
              height={15}
              style={{ position: "absolute", left: 11, top: 11, color: "var(--muted)" }}
            />
            <input
              className="input"
              style={{ paddingLeft: 34 }}
              placeholder="搜索仓库名或描述"
              value={query}
              autoFocus
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>

          {error ? <Alert tone="error">{error}</Alert> : null}
          {data && data.source === "config" ? (
            <div className="connect-cta">
              <div>
                <strong>还没有从 GitHub 拉到仓库</strong>
                <p>{data.message || "未配置 GitHub 凭据"}。连接 GitHub App 后可自动同步 installation 内的仓库。</p>
              </div>
              <button
                type="button"
                className="btn btn-sm btn-primary"
                onClick={() => { onClose(); navigate("/settings"); }}
              >
                去连接
              </button>
            </div>
          ) : null}
          {data && data.source === "app" && !repos.length ? (
            <Alert tone="warn">{data.message || "App 已安装但还没有可访问仓库，请到 GitHub 调整安装范围"}</Alert>
          ) : null}

          {recent.length ? (
            <div>
              <div style={{ fontSize: 11, letterSpacing: 1, color: "var(--muted)", textTransform: "uppercase", marginBottom: 6 }}>
                最近访问
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {recent.map((item) => (
                  <button key={item} type="button" className="chip" onClick={() => onPick(item)}>
                    {item}
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          <div style={{ maxHeight: 320, overflow: "auto", margin: "0 -6px" }}>
            {loading && !repos.length ? (
              <div style={{ padding: 20, textAlign: "center" }}>
                <Spinner label="加载仓库列表" />
              </div>
            ) : null}
            {!loading && !filtered.length ? <Empty title="没有匹配的仓库" /> : null}
            {filtered.map((item) => (
              <div
                key={item.full_name}
                className="repo-row"
                onClick={() => onPick(item.full_name)}
                style={item.full_name === current ? { borderColor: "var(--line)", background: "rgba(53,214,164,0.06)" } : undefined}
              >
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div className="name">{item.full_name}</div>
                  <div className="desc">{item.description || "无描述"}</div>
                </div>
                <div className="meta">
                  {item.language ? <span className="tag">{item.language}</span> : null}
                  <span className={`pill ${item.private ? "violet" : "muted"}`}>{item.private ? "private" : "public"}</span>
                  {item.html_url ? (
                    <a
                      className="btn btn-sm btn-ghost btn-icon"
                      href={item.html_url}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(event) => event.stopPropagation()}
                    >
                      <ExternalIcon width={13} height={13} />
                    </a>
                  ) : null}
                </div>
              </div>
            ))}
          </div>

          <div style={{ borderTop: "1px solid var(--line-soft)", paddingTop: 12 }}>
            <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>手动指定仓库</div>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                className="input"
                placeholder="owner/name"
                value={manual}
                onChange={(event) => setManual(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && manual.includes("/")) onPick(manual.trim());
                }}
              />
              <button
                type="button"
                className="btn"
                disabled={!manual.includes("/")}
                onClick={() => onPick(manual.trim())}
              >
                切换
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
