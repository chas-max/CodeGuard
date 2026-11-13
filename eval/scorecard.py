"""五维评分卡：检测、语义、门控、安全、端到端 + 成本效率。

输入只有 `CaseResult`（`results.jsonl`），不感知执行细节；输出 `scorecard.json/md`。
跳过（skipped）的用例不计入分母，也不计通过，避免"没跑就是满分"。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import median
from typing import Iterable

DIMENSIONS = ("det", "semantic", "gate", "security", "e2e")
WEIGHTS = {"det": 0.25, "gate": 0.25, "semantic": 0.20, "security": 0.15, "e2e": 0.15}
Z = 1.96


def wilson(successes: int, total: int, z: float = Z) -> tuple[float, float]:
    """二项比例的 Wilson 95% 区间；total=0 返回 (0,0)。"""
    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    denom = 1 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denom
    return (round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4))


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return float(ordered[index])


def load_results(path: Path | str) -> list[dict]:
    target = Path(path)
    if target.is_dir():
        target = target / "results.jsonl"
    if not target.is_file():
        raise SystemExit(f"找不到结果文件: {target}")
    return [
        json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _rate(hits: int, total: int) -> dict:
    low, high = wilson(hits, total)
    return {
        "success": hits, "total": total,
        "rate": round(hits / total, 4) if total else 0.0,
        "wilson_95": [low, high],
    }


def _det_metrics(rows: list[dict]) -> dict:
    live = [row for row in rows if row["layer"] == "det" and not row.get("skipped")]
    false_block = [row for row in live if not row.get("expected_block") and row.get("blocked")]
    false_merge = [row for row in live if row.get("expected_block") and not row.get("blocked")]
    per_rule: dict[str, dict] = {}
    for row in live:
        expected = set(row.get("expected_rules") or [])
        actual = set(row.get("all_rules") or [])
        for rule in expected | actual:
            stats = per_rule.setdefault(rule, {"tp": 0, "fp": 0, "fn": 0})
            if rule in expected and rule in actual:
                stats["tp"] += 1
            elif rule in expected:
                stats["fn"] += 1
            elif rule in actual:
                stats["fp"] += 1
    for rule, stats in per_rule.items():
        tp, fp, fn = stats["tp"], stats["fp"], stats["fn"]
        stats["precision"] = round(tp / (tp + fp), 4) if tp + fp else 0.0
        stats["recall"] = round(tp / (tp + fn), 4) if tp + fn else 0.0
        p, r = stats["precision"], stats["recall"]
        stats["f1"] = round(2 * p * r / (p + r), 4) if p + r else 0.0
    correct = len(live) - len(false_block) - len(false_merge)
    return {
        "cases": len(live),
        "accuracy": _rate(correct, len(live)),
        "false_block": len(false_block),
        "false_merge": len(false_merge),
        "per_rule": dict(sorted(per_rule.items())),
        "score": round(correct / len(live), 4) if live else None,
    }


def _semantic_metrics(rows: list[dict]) -> dict:
    layer = [row for row in rows if row["layer"] == "semantic"]
    live = [row for row in layer if not row.get("skipped")]
    positives = [row for row in live if row.get("expected_block")]
    negatives = [row for row in live if not row.get("expected_block")]
    recall = _rate(sum(1 for row in positives if row.get("semantic_blocked")), len(positives))
    fp = _rate(sum(1 for row in negatives if row.get("semantic_blocked")), len(negatives))
    return {
        "cases": len(layer), "skipped": len(layer) - len(live),
        "recall": recall, "false_positive_rate": fp,
        "findings_per_case": round(
            sum(int(row.get("findings_total", 0)) for row in live) / len(live), 3
        ) if live else 0.0,
        "score": recall["rate"] if live else None,
        "skip_reason": next((row.get("skip_reason") for row in layer if row.get("skipped")), ""),
    }


def _gate_metrics(rows: list[dict]) -> dict:
    live = [row for row in rows if row["layer"] == "gate" and not row.get("skipped")]
    tiers = [row for row in live if row.get("probe") == "tier"]
    approvals = [row for row in live if row.get("probe") == "approvals"]
    merge = [row for row in live if row.get("probe") == "merge"]
    tier_ok = sum(
        1 for row in tiers
        if row.get("tier") == row.get("expected_tier")
        and (row.get("expected_approvals") is None
             or int(row.get("approvals", 0)) == int(row.get("expected_approvals")))
    )
    approval_ok = sum(
        1 for row in approvals
        if int(row.get("approvals", 0)) == int(row.get("expected_approvals") or 0)
    )
    merge_ok = sum(1 for row in merge if bool(row.get("blocked")) == bool(row.get("expected_block")))
    total = len(live)
    correct = tier_ok + approval_ok + merge_ok
    return {
        "cases": total,
        "tier_accuracy": _rate(tier_ok, len(tiers)),
        "approval_accuracy": _rate(approval_ok, len(approvals)),
        "merge_gate_accuracy": _rate(merge_ok, len(merge)),
        "score": round(correct / total, 4) if total else None,
    }


def _security_metrics(rows: list[dict]) -> dict:
    live = [row for row in rows if row["layer"] == "security" and not row.get("skipped")]
    passed = [row for row in live if row.get("passed")]
    failures = [
        {"case_id": row["case_id"], "detail": row.get("detail", ""), "tags": row.get("tags") or []}
        for row in live if not row.get("passed")
    ]
    return {
        "cases": len(live),
        "invariants": _rate(len(passed), len(live)),
        "score": round(len(passed) / len(live), 4) if live else None,
        "failures": failures,
    }


def _e2e_metrics(rows: list[dict]) -> dict:
    live = [row for row in rows if row["layer"] == "e2e" and not row.get("skipped")]
    false_merge = [
        row for row in live
        if row.get("expected_block") and row.get("merge_called")
    ]
    false_block = [
        row for row in live
        if not row.get("expected_block") and not row.get("merge_called")
    ]
    passed = [row for row in live if row.get("passed")]
    costs = [float(row.get("cost_cny", 0.0) or 0.0) for row in live]
    walls = [float(row.get("wall_ms", 0) or 0) for row in live]
    rounds = [int(row.get("review_rounds", 0) or 0) for row in live]
    return {
        "cases": len(live),
        "passed": _rate(len(passed), len(live)),
        "false_merge": len(false_merge),
        "false_block": len(false_block),
        "cost_cny": {"total": round(sum(costs), 4),
                     "median": round(median(costs), 4) if costs else 0.0},
        "wall_ms": {"median": round(median(walls), 1) if walls else 0.0,
                    "p90": percentile(walls, 90)},
        "review_rounds": {"median": median(rounds) if rounds else 0,
                          "max": max(rounds) if rounds else 0},
        "score": round(len(passed) / len(live), 4) if live else None,
    }


def _efficiency(rows: list[dict]) -> dict:
    live = [row for row in rows if not row.get("skipped")]
    costs = [float(row.get("cost_cny", 0.0) or 0.0) for row in live]
    walls = [float(row.get("wall_ms", 0) or 0) for row in live]
    llm = [int(row.get("llm_calls", 0) or 0) for row in live]
    tools = [int(row.get("tool_calls", 0) or 0) for row in live]
    return {
        "cases": len(live),
        "wall_ms": {"median": round(median(walls), 1) if walls else 0.0,
                    "p90": percentile(walls, 90)},
        "cost_cny": {"total": round(sum(costs), 4),
                     "median": round(median(costs), 4) if costs else 0.0},
        "llm_calls": {"total": sum(llm), "median": median(llm) if llm else 0},
        "tool_calls": {"total": sum(tools), "median": median(tools) if tools else 0},
    }


def scorecard(rows: Iterable[dict], *, run_meta: dict | None = None) -> dict:
    items = list(rows)
    dims = {
        "det": _det_metrics(items),
        "semantic": _semantic_metrics(items),
        "gate": _gate_metrics(items),
        "security": _security_metrics(items),
        "e2e": _e2e_metrics(items),
    }
    available = {name: dims[name]["score"] for name in DIMENSIONS if dims[name]["score"] is not None}
    weight_sum = sum(WEIGHTS[name] for name in available)
    composite = (
        round(sum(available[name] * WEIGHTS[name] for name in available) / weight_sum, 4)
        if available else None
    )
    failures = [
        {
            "case_id": row["case_id"], "layer": row["layer"],
            "tags": row.get("tags") or [], "weight": row.get("weight", 1.0),
            "expected_block": row.get("expected_block"),
            "actual_blocked": row.get("blocked"),
            "expected_rules": row.get("expected_rules") or [],
            "actual_rules": row.get("all_rules") or [],
            "expected_tier": row.get("expected_tier", ""),
            "actual_tier": row.get("tier", ""),
            "detail": row.get("detail", ""), "error": row.get("error", ""),
        }
        for row in items if not row.get("passed") and not row.get("skipped")
    ]
    return {
        "schema_version": 1,
        "run": run_meta or {},
        "dimensions": dims,
        "composite": {
            "score": composite,
            "weights": {name: WEIGHTS[name] for name in available},
            "excluded": sorted(set(DIMENSIONS) - set(available)),
        },
        "efficiency": _efficiency(items),
        "gap_cases": [row["case_id"] for row in items if "gap" in (row.get("tags") or [])],
        "failures": failures,
    }


def compare(current: dict, baseline: dict) -> dict:
    delta: dict[str, dict] = {}
    for name in DIMENSIONS:
        cur = (current.get("dimensions", {}).get(name) or {}).get("score")
        old = (baseline.get("dimensions", {}).get(name) or {}).get("score")
        if cur is None or old is None:
            continue
        delta[name] = {"baseline": old, "current": cur, "delta": round(cur - old, 4)}
    cur_total = (current.get("composite") or {}).get("score")
    old_total = (baseline.get("composite") or {}).get("score")
    if cur_total is not None and old_total is not None:
        delta["composite"] = {
            "baseline": old_total, "current": cur_total, "delta": round(cur_total - old_total, 4),
        }
    return delta


def render_markdown(card: dict) -> str:
    dims = card["dimensions"]
    lines = [
        "# CodeGuard 基线评分卡",
        "",
        f"- 综合分：**{card['composite']['score']}**",
        f"- 参与维度：{', '.join(card['composite']['weights']) or '-'}",
        f"- 排除维度：{', '.join(card['composite']['excluded']) or '-'}",
        f"- 用例总数：{card['efficiency']['cases']}",
        "",
        "| 维度 | 分数 | 用例 | 关键指标 |",
        "|---|---|---|---|",
    ]
    det = dims["det"]
    lines.append(
        f"| 检测 | {det['score']} | {det['cases']} | "
        f"错误放行 {det['false_merge']} / 错误拦截 {det['false_block']} / "
        f"准确率 {det['accuracy']['rate']} |"
    )
    sem = dims["semantic"]
    sem_score = "—" if sem["score"] is None else sem["score"]
    lines.append(
        f"| 语义 | {sem_score} | {sem['cases']}（跳过 {sem['skipped']}） | "
        f"recall {sem['recall']['rate']} / FPR {sem['false_positive_rate']['rate']} |"
    )
    gate = dims["gate"]
    lines.append(
        f"| 门控 | {gate['score']} | {gate['cases']} | "
        f"tier {gate['tier_accuracy']['rate']} / 审批 {gate['approval_accuracy']['rate']} / "
        f"merge {gate['merge_gate_accuracy']['rate']} |"
    )
    sec = dims["security"]
    lines.append(
        f"| 安全 | {sec['score']} | {sec['cases']} | "
        f"不变量通过 {sec['invariants']['rate']} |"
    )
    e2e = dims["e2e"]
    lines.append(
        f"| 全链路 | {e2e['score']} | {e2e['cases']} | "
        f"错误放行 {e2e['false_merge']} / 错误拦截 {e2e['false_block']} / "
        f"成本 {e2e['cost_cny']['total']} CNY |"
    )
    lines += ["", "## 每规则 P/R", "", "| rule | precision | recall | F1 | tp/fp/fn |", "|---|---|---|---|---|"]
    for rule, stats in det["per_rule"].items():
        lines.append(
            f"| {rule} | {stats['precision']} | {stats['recall']} | {stats['f1']} | "
            f"{stats['tp']}/{stats['fp']}/{stats['fn']} |"
        )
    lines += ["", "## 失败用例", ""]
    if not card["failures"]:
        lines.append("- 无")
    for item in card["failures"]:
        lines.append(
            f"- `{item['case_id']}`（{item['layer']}，{', '.join(item['tags']) or '-'}）："
            f"{item['detail'] or item['error'] or 'no detail'}"
        )
    lines += ["", "## 成本与时延", ""]
    eff = card["efficiency"]
    lines.append(
        f"- 墙钟：median {eff['wall_ms']['median']} ms / p90 {eff['wall_ms']['p90']} ms"
    )
    lines.append(f"- 成本：total {eff['cost_cny']['total']} CNY / median {eff['cost_cny']['median']} CNY")
    lines.append(f"- LLM 调用：{eff['llm_calls']['total']}；工具调用：{eff['tool_calls']['total']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.scorecard")
    parser.add_argument("--input", default="reports/eval_baseline/results.jsonl")
    parser.add_argument("--out", default="reports/eval_baseline")
    parser.add_argument("--compare", default="", help="上一版 scorecard.json，用于回归对比")
    args = parser.parse_args(argv)

    rows = load_results(args.input)
    run_meta = {}
    summary_path = Path(args.input).parent / "run_summary.json"
    if summary_path.is_file():
        run_meta = json.loads(summary_path.read_text(encoding="utf-8"))
    card = scorecard(rows, run_meta=run_meta)
    if args.compare:
        baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        card["comparison"] = compare(card, baseline)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "scorecard.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (out / "scorecard.md").write_text(render_markdown(card), encoding="utf-8")
    print(json.dumps({
        "composite": card["composite"]["score"],
        "dimensions": {name: card["dimensions"][name]["score"] for name in DIMENSIONS},
        "failures": len(card["failures"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
