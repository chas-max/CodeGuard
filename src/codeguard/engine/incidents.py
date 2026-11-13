"""合并后归因：把 revert / 红 CI 变成 incident 记录与 eval 夹具骨架。

这是文章「any incident is now an eval」在内部场景的落地形态：
CodeGuard 能看到主干 CI 与后续 revert PR，所以可以把一次事故固化成可重放的 case。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

BAD_CI_STATES = ("failure", "error")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def find_revert_pr(pulls: list[dict], merged_sha: str) -> dict | None:
    needle = (merged_sha or "")[:7].lower()
    if not needle:
        return None
    for pull in pulls or []:
        title = str(pull.get("title", "")).lower()
        body = str(pull.get("body") or "").lower()
        if title.startswith("revert") and (needle in title or needle in body):
            return {
                "number": pull.get("number"),
                "title": pull.get("title", ""),
                "html_url": pull.get("html_url", ""),
            }
    return None


def collect_incident(
    github,
    *,
    repo: str,
    merged_sha: str,
    hours: int = 72,
) -> dict:
    """检查一次合并之后是否出现红 CI 或被 revert。"""
    try:
        status = github.get_combined_status(repo, merged_sha) or {}
    except Exception as exc:  # noqa: BLE001 - 取证失败不应拖垮调用方
        status = {"state": "unknown", "error": str(exc)}
    ci_state = str(status.get("state", "unknown"))
    revert_pr = None
    if hasattr(github, "list_pulls"):
        try:
            revert_pr = find_revert_pr(
                github.list_pulls(repo, state="closed", per_page=50), merged_sha,
            )
        except Exception:  # noqa: BLE001
            revert_pr = None
    incident = ci_state in BAD_CI_STATES or revert_pr is not None
    return {
        "repo": repo,
        "merged_sha": merged_sha,
        "window_hours": hours,
        "ci_state": ci_state,
        "revert_pr": revert_pr,
        "incident": incident,
        "checked_at": _now(),
    }


def write_incident(directory: Path | str, incident: dict) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    sha = str(incident.get("merged_sha", ""))[:8] or "unknown"
    path = target / f"incident-{sha}.json"
    path.write_text(json.dumps(incident, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_fixture_stub(incident: dict) -> dict:
    """生成 eval 夹具骨架；head/base 需要人工补上最小复现改动。"""
    sha = str(incident.get("merged_sha", ""))[:8] or "unknown"
    return {
        "name": f"incident-{sha}",
        "source": "incident",
        "profile": "change",
        "base": {},
        "head": {},
        "expected": {"block": True, "rules": []},
    }


__all__ = [
    "BAD_CI_STATES", "find_revert_pr", "collect_incident", "write_incident",
    "build_fixture_stub",
]
