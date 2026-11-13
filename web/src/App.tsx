import { useEffect, useState } from "react";
import { api } from "./api";
import { useAsync, useRepo } from "./hooks";
import { navigate, useRoute } from "./router";
import { RepoPicker, RepoSwitcher } from "./components/RepoSwitcher";
import { BoardIcon, ClockIcon, GearIcon, RouteIcon, ShieldIcon } from "./components/icons";
import { DashboardPage } from "./pages/DashboardPage";
import { JobsPage } from "./pages/JobsPage";
import { JobDetailPage } from "./pages/JobDetailPage";
import { SettingsPage } from "./pages/SettingsPage";
import { WatchersPage } from "./pages/WatchersPage";

export default function App() {
  const route = useRoute();
  const [repo, setRepo] = useRepo();
  const [pickerOpen, setPickerOpen] = useState(false);

  const config = useAsync(() => api.config(), []);
  const health = useAsync(() => api.health(), [], 20000);
  const jobs = useAsync(() => api.jobs(repo), [repo], 15000);

  // 路由变化时关闭弹层，避免遮住新页面。
  useEffect(() => {
    setPickerOpen(false);
  }, [route.name, route.name === "job" ? route.id : ""]);

  const waiting = (jobs.data?.jobs || []).filter((job) => job.status === "waiting_human").length;
  const running = (jobs.data?.jobs || []).filter((job) => job.status === "running" || job.status === "queued").length;
  const online = health.data?.ok === true;

  const nav: Array<{ key: string; label: string; icon: JSX.Element; count?: number; path: string }> = [
    { key: "dashboard", label: "PR 看板", icon: <BoardIcon />, path: "/" },
    { key: "jobs", label: "任务", icon: <RouteIcon />, count: running + waiting || undefined, path: "/jobs" },
    { key: "watchers", label: "定时查看", icon: <ClockIcon />, path: "/watchers" },
    { key: "settings", label: "设置与健康", icon: <GearIcon />, path: "/settings" },
  ];

  const activeKey = route.name === "job" ? "jobs" : route.name;

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><ShieldIcon width={19} height={19} /></div>
          <div className="brand-text">
            <strong>CodeGuard</strong>
            <span>PR Review Console</span>
          </div>
        </div>

        <nav className="nav">
          <div className="nav-label">工作台</div>
          {nav.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`nav-item ${activeKey === item.key ? "active" : ""}`}
              onClick={() => navigate(item.path)}
            >
              <span className="nav-icon">{item.icon}</span>
              <span>{item.label}</span>
              {item.count ? <span className="nav-count">{item.count}</span> : null}
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="health-card">
            <div className="health-row">
              <span className={`pill ${online ? "ok" : "danger"}`} style={{ padding: "0 7px" }}>
                <i className="dot" />
                {online ? "online" : "offline"}
              </span>
              <span style={{ marginLeft: "auto" }} className="value">
                {config.data?.sandbox_backend || "—"}
              </span>
            </div>
            <div className="health-row">
              <span className="label">GitHub</span>
              <span className="value" title={config.data?.github_app?.slug || config.data?.github_login || ""}>
                {config.data?.github_auth === "app"
                  ? `app · ${config.data?.github_app?.slug || "connected"}`
                  : config.data?.github_auth === "pat"
                    ? "pat"
                    : "not connected"}
              </span>
            </div>
            <div className="health-row">
              <span className="label">队列</span>
              <span className="value">{running} run · {waiting} hitl</span>
            </div>
          </div>
          <div style={{ fontSize: 10.5, color: "#4d5768", textAlign: "center", fontFamily: "var(--mono)" }}>
            v0.1 · LangGraph
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <RepoSwitcher repo={repo} onSelect={setRepo} />
          <div className="topbar-actions">
            {waiting ? (
              <button type="button" className="btn btn-sm" onClick={() => navigate("/jobs")} style={{ borderColor: "rgba(243,187,90,0.4)" }}>
                <span className="pill warn" style={{ background: "none", border: 0, padding: 0 }}>
                  <i className="dot" /> {waiting} 个待人工
                </span>
              </button>
            ) : null}
            <button type="button" className="btn btn-sm" onClick={() => navigate("/watchers")}>
              <ClockIcon width={14} height={14} />
              定时查看
            </button>
            <div className="avatar" title={config.data?.github_login || "CodeGuard"}>
              {(config.data?.github_login || "CG").slice(0, 2).toUpperCase()}
            </div>
          </div>
        </header>

        <main className="content">
          {route.name === "dashboard" ? (
            <DashboardPage repo={repo} onPickRepo={() => setPickerOpen(true)} />
          ) : null}
          {route.name === "jobs" ? <JobsPage repo={repo} /> : null}
          {route.name === "job" ? <JobDetailPage jobId={route.id} /> : null}
          {route.name === "watchers" ? <WatchersPage repo={repo} /> : null}
          {route.name === "settings" ? <SettingsPage /> : null}
        </main>
      </div>

      {pickerOpen ? (
        <RepoPicker
          current={repo}
          onClose={() => setPickerOpen(false)}
          onPick={(value) => {
            setRepo(value);
            setPickerOpen(false);
          }}
        />
      ) : null}
    </div>
  );
}
