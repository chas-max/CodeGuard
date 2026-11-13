"""CodeGuard CLI：review / serve / health。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from .config import load_settings
from .engine.policy import default_policy, load_policy
from .github.client import GitHubClient
from .mcp import McpClientManager
from .models import FileChange, PullRequestContext
from .runtime import CodeGuardRuntime
from .tools.registry import build_toolbox


def _pr_from_github(repo: str, number: int, token: str) -> PullRequestContext:
    client = GitHubClient(token)
    try:
        pr = client.get_pr(repo, number)
        files = client.list_files(repo, number)
    finally:
        client.close()
    base, head = pr["base"], pr["head"]
    return PullRequestContext(
        repo=repo, number=number,
        base_ref=base["ref"], base_sha=base.get("sha", ""),
        head_ref=head["ref"], head_sha=head.get("sha", ""),
        title=pr.get("title", ""), body=pr.get("body") or "",
        author=(pr.get("user") or {}).get("login", ""),
        is_fork=bool(head.get("repo") and head["repo"].get("full_name") != repo),
        is_draft=bool(pr.get("draft")), mergeable=bool(pr.get("mergeable", True)),
        additions=int(pr.get("additions", 0)), deletions=int(pr.get("deletions", 0)),
        changed_file_count=int(pr.get("changed_files", 0)),
        html_url=pr.get("html_url", ""),
        labels=[str((item or {}).get("name", "")) for item in (pr.get("labels") or [])],
        created_at=pr.get("created_at", ""),
        changed_files=[
            FileChange(path=f["filename"], status=f.get("status", "modified"),
                       additions=int(f.get("additions", 0)), deletions=int(f.get("deletions", 0)),
                       patch=f.get("patch", "") or "")
            for f in files
        ],
    )


def _changed_from_git(root: Path, ref: str) -> list[str]:
    """`--diff-from`：用 git 计算相对某个 ref 的变更文件，供 pre-push 自检使用。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", ref],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        print(f"无法执行 git: {exc}", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"git diff 失败: {result.stderr.strip()[:300]}", file=sys.stderr)
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def cmd_review(args) -> int:
    cfg = load_settings()
    runtime = CodeGuardRuntime(cfg)
    if args.local:
        root = Path(args.local).resolve()
        if not root.is_dir():
            print(f"本地仓库不存在: {root}", file=sys.stderr)
            return 2
        changed = list(args.changed)
        if args.diff_from:
            changed = _changed_from_git(root, args.diff_from)
        if not changed:
            changed = [
                p.relative_to(root).as_posix() for p in sorted(root.rglob("*")) if p.is_file()
            ]
        pr = PullRequestContext(
            repo=args.repo or "local/repo", number=args.pr or 1,
            base_ref=args.base, head_ref=args.head,
            base_sha=args.base_sha, head_sha=args.head_sha,
            title=args.title, body=args.body,
            profile=args.profile,
            changed_files=[FileChange(path=p) for p in changed],
        )
        overrides = {
            "repo_root": str(root),
            "base_root": args.base_root or str(root),
            "dry_run": bool(args.dry_run),
        }
    else:
        if not cfg.github_token:
            print("非 --local 模式需要 GITHUB_TOKEN", file=sys.stderr)
            return 2
        pr = _pr_from_github(args.repo, args.pr, cfg.github_token)
        overrides = {}
    job_id = uuid.uuid4().hex[:12]
    snap = runtime.start(pr, job_id, **overrides)
    while True:
        if snap.get("done"):
            values = snap.get("values", {})
            blocking = values.get("blocking_findings") or []
            print(json.dumps({
                "job_id": job_id,
                "outcome": values.get("outcome"),
                "error": values.get("error"),
                "merge_commit_sha": values.get("merge_commit_sha", ""),
                "tier": (values.get("risk_tier") or {}).get("tier"),
                "blocking_findings": len(blocking),
            }, ensure_ascii=False, indent=2))
            if args.fail_on_blocking:
                return 1 if blocking else 0
            return 0 if values.get("outcome") == "MERGED" else 1
        payload = snap.get("interrupt") or {}
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
        approver = args.approver or os.environ.get("GIT_AUTHOR_NAME") or "operator"
        try:
            answer = input(f"批准? (y/n, 审批人={approver}) > ").strip().lower()
        except EOFError:
            answer = "n"
        snap = runtime.resume(job_id, {
            "approved": answer in ("y", "yes", "1"),
            "feedback": "",
            "approver": approver,
            "finding_outcomes": {},
        })


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("codeguard.server:app", host=args.host, port=args.port, reload=False)
    return 0


def cmd_health(args) -> int:
    cfg = load_settings()
    from .sandbox.health import check_docker

    ok, message = check_docker() if cfg.sandbox_backend == "docker" else (True, "fake backend")
    print(json.dumps({
        "github_configured": bool(cfg.github_token),
        "sandbox_backend": cfg.sandbox_backend,
        "docker": {"ok": ok, "message": message},
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_stats(args) -> int:
    from .engine.metrics import load_records, render_markdown, summarize

    records = load_records(args.dir)
    report = summarize(records)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "metrics.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("total", "merged", "auto_merge_share",
                                            "findings_precision")}, ensure_ascii=False))
    return 0 if report["total"] else 1


def cmd_incidents(args) -> int:
    from .engine.incidents import build_fixture_stub, collect_incident, write_incident

    cfg = load_settings()
    if not cfg.github_token:
        print("incidents sync 需要 GITHUB_TOKEN", file=sys.stderr)
        return 2
    client = GitHubClient(cfg.github_token, api_url=cfg.github_api_url)
    try:
        incident = collect_incident(
            client, repo=args.repo, merged_sha=args.sha,
            hours=args.hours or cfg.post_merge_watch_hours,
        )
    finally:
        client.close()
    out = Path(args.out)
    path = write_incident(out, incident)
    if args.fixture:
        stub = build_fixture_stub(incident)
        (out / f"{stub['name']}.json").write_text(
            json.dumps(stub, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"incident": incident, "record": str(path)}, ensure_ascii=False, indent=2))
    return 0


def _load_cli_policy(policy_path: str | None):
    if policy_path:
        return load_policy(Path(policy_path))
    default_file = Path("policy/default.yml")
    if default_file.is_file():
        return load_policy(default_file)
    return default_policy()


def cmd_mcp(args: argparse.Namespace) -> int:
    policy = _load_cli_policy(getattr(args, "policy", None))
    cfg = load_settings()
    subcmd = args.mcp_command
    if subcmd == "list":
        data = {
            "enabled": bool(policy.agents.mcp.enabled and not cfg.mcp_disabled),
            "max_capability": policy.agents.mcp.max_capability,
            "servers": [s.model_dump() for s in policy.agents.mcp.servers],
        }
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    mgr = McpClientManager()
    if subcmd == "probe":
        statuses = mgr.probe_all(policy.agents.mcp, force_stdio=True)
        enabled_items = [s for s in statuses if s.enabled]
        all_ok = all(s.ok for s in enabled_items)
        print(
            json.dumps(
                {"ok": all_ok, "servers": [s.model_dump() for s in statuses]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if all_ok else 1

    if subcmd == "tools":
        role = args.role
        root = Path(args.root).resolve()

        class _SimpleCtx:
            def __init__(self, cfg_obj, mcp_mgr):
                self.cfg = cfg_obj
                self.mcp_manager = mcp_mgr
                self.sandbox = None

        box = build_toolbox(
            role=role,
            root=root,
            job_id="cli-inspect",
            ctx=_SimpleCtx(cfg, mgr),
            policy=policy,
            tier=args.tier,
            allowed_files=["__dummy__.py"] if role == "fixer" else None,
        )
        tools_list = []
        if box is not None:
            for spec in box.specs():
                tools_list.append({
                    "name": spec.name,
                    "source": spec.source,
                    "server": spec.server,
                    "capability": spec.capability,
                    "description": spec.description,
                })
        print(
            json.dumps(
                {"role": role, "tier": args.tier, "count": len(tools_list), "tools": tools_list},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codeguard", description="CodeGuard PR review agent")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="审查一个 PR 或本地变更")
    review.add_argument("--repo", default="")
    review.add_argument("--pr", type=int, default=1)
    review.add_argument("--local", default="", help="本地仓库路径（测试/离线）")
    review.add_argument("--base-root", default="")
    review.add_argument("--base", default="main")
    review.add_argument("--head", default="feature")
    review.add_argument("--base-sha", default="")
    review.add_argument("--head-sha", default="")
    review.add_argument("--title", default="")
    review.add_argument("--body", default="")
    review.add_argument("--changed", nargs="*", default=[])
    review.add_argument("--profile", choices=["change", "bugfix"], default="")
    review.add_argument("--approver", default="")
    review.add_argument("--diff-from", default="", help="用 git diff 计算变更文件（pre-push 自检）")
    review.add_argument("--fail-on-blocking", action="store_true",
                        help="有 blocking finding 时返回 1，供 hook 使用")
    review.add_argument("--dry-run", action="store_true")
    review.set_defaults(func=cmd_review)

    stats = sub.add_parser("stats", help="聚合 job record 生成指标")
    stats.add_argument("--dir", default="reports/jobs")
    stats.add_argument("--out", default="reports/metrics")
    stats.set_defaults(func=cmd_stats)

    incidents = sub.add_parser("incidents", help="合并后归因（revert / 红 CI）")
    incident_sub = incidents.add_subparsers(dest="incident_command", required=True)
    sync = incident_sub.add_parser("sync", help="检查某个合并 commit 是否引发事故")
    sync.add_argument("--repo", required=True)
    sync.add_argument("--sha", required=True)
    sync.add_argument("--hours", type=int, default=0, help="0 = 使用 POST_MERGE_WATCH_HOURS")
    sync.add_argument("--out", default="reports/incidents")
    sync.add_argument("--fixture", action="store_true", help="同时生成 eval 夹具骨架")
    sync.set_defaults(func=cmd_incidents)

    serve = sub.add_parser("serve", help="启动 FastAPI 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8002)
    serve.set_defaults(func=cmd_serve)

    health = sub.add_parser("health", help="检查配置与 Docker")
    health.set_defaults(func=cmd_health)

    mcp_parser = sub.add_parser("mcp", help="MCP Server 配置列表、健康探测与 Agent 工具预览")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command", required=True)

    mcp_list = mcp_sub.add_parser("list", help="列出当前策略下的 MCP Server 配置")
    mcp_list.add_argument("--policy", default="")
    mcp_list.set_defaults(func=cmd_mcp)

    mcp_probe = mcp_sub.add_parser("probe", help="真实拉起并探测所有启用的 MCP Server")
    mcp_probe.add_argument("--policy", default="")
    mcp_probe.set_defaults(func=cmd_mcp)

    mcp_tools = mcp_sub.add_parser("tools", help="查看指定 Agent 角色可见的全部工具（含内置与 MCP）")
    mcp_tools.add_argument("--role", default="reviewer")
    mcp_tools.add_argument("--tier", default="T1", choices=["T0", "T1", "T2"])
    mcp_tools.add_argument("--root", default=".")
    mcp_tools.add_argument("--policy", default="")
    mcp_tools.set_defaults(func=cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
