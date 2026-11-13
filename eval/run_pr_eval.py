"""确定性 PR 审查评测：不调用 LLM、不依赖 Docker。

夹具定义在 `eval/fixtures/cases.json`（`python -m eval.run_pr_eval`）。
一次漏检/误拦应当沉淀成一条 fixture，而不是改这个脚本。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeguard.engine.api_contract import check_api_contract
from codeguard.engine.policy import Policy
from codeguard.engine.rules import RuleEngine
from codeguard.engine.static_scan import scan_tree
from codeguard.engine.tests_guard import guard_tests
from eval.loader import load_cases
from eval.pseudo_pr import from_case


def _evaluate_case(case: dict, tmp: Path) -> dict:
    materialized = from_case(case).materialize(tmp)
    base, head = materialized.base_dir, materialized.head_dir
    changed = list(materialized.changed)
    policy = Policy()
    engine = RuleEngine(policy)
    det = scan_tree(head, changed, base_root=base)
    rules = engine.evaluate({}, [{"path": p} for p in changed], "", base_root=base, head_root=head)
    guard = guard_tests(base, head, changed)
    api = check_api_contract(base, head, changed)
    findings = list(det.findings) + rules + guard + api
    blocked = any(item.blocking for item in findings)
    ids = sorted({item.rule_id for item in findings if item.blocking})
    expected = case["expected"]
    passed = blocked == expected["block"] and all(rule in ids for rule in expected["rules"])
    return {
        "scenario": case["name"],
        "source": case["source"],
        "profile": case["profile"],
        "expected_block": expected["block"],
        "blocked": blocked,
        "blocking_rules": ids,
        "expected_rules": expected["rules"],
        "pass": passed,
    }


def run(out_dir: Path, cases_path: str | None = None) -> dict:
    cases = load_cases(cases_path)
    rows = []
    for case in cases:
        with tempfile.TemporaryDirectory(prefix="cg_eval_") as tmp_str:
            rows.append(_evaluate_case(case, Path(tmp_str)))
    report = {
        "total": len(rows),
        "passed": sum(1 for row in rows if row["pass"]),
        "false_merge": sum(1 for row in rows if row["expected_block"] and not row["blocked"]),
        "false_block": sum(1 for row in rows if not row["expected_block"] and row["blocked"]),
        "rows": rows,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pr_eval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# PR 审查确定性评测", "", f"- 场景数: {report['total']}",
             f"- 通过: {report['passed']}", f"- 错误放行: {report['false_merge']}",
             f"- 错误拦截: {report['false_block']}", "",
             "| 场景 | 来源 | 期望拦截 | 实际拦截 | 规则 | 通过 |",
             "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['scenario']} | {row['source']} | {row['expected_block']} | {row['blocked']} | "
            f"{', '.join(row['blocking_rules']) or '-'} | {row['pass']} |"
        )
    (out_dir / "pr_eval_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="reports/pr_eval")
    parser.add_argument("--cases", default="")
    args = parser.parse_args()
    report = run(Path(args.out), args.cases or None)
    print(json.dumps({k: report[k] for k in ("total", "passed", "false_merge", "false_block")},
                     ensure_ascii=False))
    return 0 if report["false_merge"] == 0 and report["false_block"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
