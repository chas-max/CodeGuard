"""reviewer 离线评测：用录制输出回放，零 API key、零网络。

每个 case 保存当时的 reviewer 原始 JSON 输出与期望的 blocking 集合，
用来衡量 reviewer 的 precision/recall，以及“style 是否被误判为 blocking”。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codeguard.agents.reviewer import review

DEFAULT_CASES = Path(__file__).resolve().parent / "cassettes" / "reviewer_cases.json"

DIFF = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,3 +1,5 @@
 def f(user):
+    name = user.name
     return name
"""


class _Response:
    def __init__(self, content: str):
        self.content = content
        self.usage_metadata = None


class _LLM:
    def __init__(self, content: str):
        self.content = content

    def invoke(self, messages):  # noqa: ARG002
        return _Response(self.content)


class _Ctx:
    def __init__(self, content: str):
        self._llm = _LLM(content)

    def llm(self, role: str):  # noqa: ARG002
        return self._llm


def _keys(findings: list[dict]) -> set[str]:
    return {
        f"{item.get('path')}:{item.get('line')}"
        for item in findings if item.get("blocking")
    }


def run(cases_path: Path | str | None = None) -> dict:
    target = Path(cases_path) if cases_path else DEFAULT_CASES
    cases = json.loads(target.read_text(encoding="utf-8"))
    rows = []
    tp = fp = fn = 0
    for case in cases:
        ctx = _Ctx(json.dumps(case["response"], ensure_ascii=False))
        expected_raw = case.get("expected_blocking") or []
        decision, _, _ = review(
            ctx,
            claim={}, hybrid_report={}, validation_report={"status": "passed"},
            diff_text=DIFF, changed_paths=["src/a.py"],
        )
        actual = _keys([item.model_dump(mode="json") for item in decision.findings])
        expected = set(expected_raw)
        tp += len(actual & expected)
        fp += len(actual - expected)
        fn += len(expected - actual)
        rows.append({
            "case": case["name"],
            "verdict": decision.verdict.value,
            "expected": sorted(expected),
            "actual": sorted(actual),
            "pass": actual == expected,
        })
    precision = round(tp / (tp + fp), 3) if tp + fp else 1.0
    recall = round(tp / (tp + fn), 3) if tp + fn else 1.0
    return {
        "total": len(rows),
        "passed": sum(1 for row in rows if row["pass"]),
        "precision": precision,
        "recall": recall,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="")
    parser.add_argument("--out", default="reports/reviewer_eval")
    args = parser.parse_args()
    report = run(args.cases or None)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reviewer_eval.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# reviewer 离线评测", "",
        f"- case 数: {report['total']}",
        f"- 通过: {report['passed']}",
        f"- precision: {report['precision']}",
        f"- recall: {report['recall']}", "",
        "| case | verdict | expected | actual | 通过 |", "|---|---|---|---|---|",
    ]
    for row in report["rows"]:
        lines.append(
            f"| {row['case']} | {row['verdict']} | {', '.join(row['expected']) or '-'} | "
            f"{', '.join(row['actual']) or '-'} | {row['pass']} |"
        )
    (out / "reviewer_eval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("total", "passed", "precision", "recall")},
                     ensure_ascii=False))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
