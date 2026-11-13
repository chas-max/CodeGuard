"""伪 PR 全链路模拟：跑完整张图，但唯一被「模仿」的东西是 PR 本身。

LLM 与沙箱走真实配置（`make_llm` / Docker backend），GitHub 换成
`LocalGitHub` 记录器——不 clone、不 push、不评论、不合并，也就不需要 token。

用法：

    python -m eval.simulate_pr --case clean_helper
    python -m eval.simulate_pr --case secret --cases eval/fixtures/cases.json

判定链路（`ingest → … → merge_gate → merge → finalize`）与线上完全一致，
所以 `merge_called=True` 的含义就是「门控放行并调用了合并」。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeguard.config import Settings, load_settings
from codeguard.errors import ConfigError
from codeguard.graph import make_checkpointer
from codeguard.nodes import ReviewNodes
from codeguard.runtime import CodeGuardRuntime

from .local_github import LocalGitHub
from .pseudo_pr import MaterializedPR, PseudoPR

MAX_APPROVALS = 5


def simulate(
    pseudo: PseudoPR,
    *,
    cfg: Settings,
    root: Path,
    llm_provider: Optional[Callable] = None,
    sandbox: Any = None,
    github: Any = None,
    job_id: str = "",
    approve: bool = True,
    checkpoint_path: Optional[Path] = None,
) -> dict:
    """把伪 PR 跑一遍完整流程，返回可断言的判定结果。

    - `llm_provider` 为 None → 用 `make_llm` + 真实 API key；
    - `sandbox` 为 None → 按 `cfg.sandbox_backend` 装配（默认 docker）；
    - `github` 为 None → 用不触网的 `LocalGitHub` 记录器。
    """
    job_id = job_id or f"sim-{pseudo.name}"
    materialized: MaterializedPR = pseudo.materialize(Path(root))
    github = github if github is not None else LocalGitHub()
    nodes = ReviewNodes(cfg, llm_provider=llm_provider, github=github, sandbox=sandbox)
    ckpt = Path(checkpoint_path) if checkpoint_path else (
        Path(cfg.data_dir) / f"ckpt_{job_id}.db"
    )
    runtime = CodeGuardRuntime(cfg, nodes=nodes, checkpointer=make_checkpointer(ckpt))

    snap = runtime.start(materialized.pr, job_id, **materialized.overrides())
    approvals = 0
    while not snap.get("done") and approvals < MAX_APPROVALS:
        if not approve:
            break
        approvals += 1
        snap = runtime.resume(job_id, {
            "approved": True, "approver": f"sim-approver-{approvals}",
            "feedback": "", "finding_outcomes": {},
        })

    values = snap.get("values", {})
    blocking = list(values.get("blocking_findings") or [])
    merge_decision = values.get("merge_decision") or {}
    findings = list(values.get("findings") or []) + list(
        (values.get("review_decision") or {}).get("findings") or []
    )
    return {
        "scenario": pseudo.name,
        "source": pseudo.source,
        "profile": values.get("profile") or pseudo.profile,
        "tier": str((values.get("risk_tier") or {}).get("tier") or ""),
        "tier_reasons": list((values.get("risk_tier") or {}).get("reasons") or []),
        "outcome": values.get("outcome") or "",
        "merge_called": bool(getattr(github, "merged", [])),
        "merge_reason": str(merge_decision.get("reason") or ""),
        "pushed": len(getattr(github, "pushed", []) or []),
        "comments": len(getattr(github, "comments", []) or []),
        "blocking": len(blocking),
        "blocking_rules": sorted({str(f.get("rule_id") or "") for f in blocking if f.get("rule_id")}),
        "semantic_blocking": sum(
            1 for f in blocking if str(f.get("source") or "") == "semantic"
        ),
        "findings_total": len(findings),
        "verdict": str((values.get("review_decision") or {}).get("verdict") or ""),
        "validation": str((values.get("validation_report") or {}).get("status") or ""),
        "validation_error": str((values.get("validation_report") or {}).get("error") or ""),
        "approvals": approvals,
        "review_rounds": int(values.get("review_round", 0) or 0),
        "fix_rounds": int(values.get("fix_round", 0) or 0),
        "llm_calls": int(values.get("llm_calls", 0) or 0),
        "tool_calls": int(values.get("tool_calls", 0) or 0),
        "cost_cny": round(sum(
            float(item.get("cost_cny", 0.0)) for item in (values.get("budget_events") or [])
        ), 4),
        "error": str(values.get("error") or ""),
        "done": bool(snap.get("done")),
    }


def matches(pseudo: PseudoPR, result: dict) -> bool:
    """判定是否符合夹具期望：该拦的拦住、该放行的走到合并调用。"""
    if not result.get("done"):
        return False
    if pseudo.expected_block:
        if result.get("merge_called"):
            return False
        rules = set(result.get("blocking_rules") or [])
        return all(rule in rules for rule in pseudo.expected_rules)
    return result.get("outcome") == "MERGED" and bool(result.get("merge_called"))


def _pick_case(name: str, cases_path: str | None) -> PseudoPR:
    from .loader import load_cases

    for case in load_cases(cases_path or None):
        if case["name"] == name:
            from .pseudo_pr import from_case

            return from_case(case)
    raise SystemExit(f"找不到用例: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.simulate_pr",
        description="用真实 LLM/沙箱模拟一次 PR 审查（不关联 GitHub）",
    )
    parser.add_argument("--case", required=True, help="用例名（见 eval/fixtures/cases.json）")
    parser.add_argument("--cases", default="", help="用例文件路径")
    parser.add_argument("--profile", default="", help="覆盖 profile：change/bugfix")
    parser.add_argument("--root", default="", help="工作目录，默认 data/simulated/<case>")
    parser.add_argument("--out", default="reports/simulated_pr")
    parser.add_argument("--no-approve", action="store_true", help="不自动通过人工审批")
    args = parser.parse_args(argv)

    cfg = load_settings()
    try:
        # 真实配置校验：沙箱要能起 docker，LLM 要有 key；GitHub 不参与。
        cfg.validate(require_docker=True, require_github=False)
    except ConfigError as exc:
        print(f"配置不可用，未执行模拟：{exc}", file=sys.stderr)
        return 2

    pseudo = _pick_case(args.case, args.cases or None)
    if args.profile:
        pseudo = dataclasses.replace(pseudo, profile=args.profile)
    root = Path(args.root) if args.root else Path(cfg.data_dir) / "simulated" / pseudo.name

    result = simulate(
        pseudo, cfg=cfg, root=root, approve=not args.no_approve,
    )
    result["pass"] = matches(pseudo, result)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{pseudo.name}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


__all__ = ["MAX_APPROVALS", "simulate", "matches", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
