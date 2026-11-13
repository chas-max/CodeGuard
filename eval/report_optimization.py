"""把基线失败收敛成可排序的优化项。

排序口径：`impact = 严重度权重 × 失败用例数 × 平均权重`。
严重度：错误放行/安全不变量 = 5，漏检/门控缺陷 = 4，错误拦截 = 3，噪声 = 2。
每条优化项都附证据 case、根因定位、验证命令与预期改善指标。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

SEVERITY = {"merge": 5, "security": 5, "detect": 4, "gate": 4, "noise": 3}


def _root_cause(case_id: str, failure: dict) -> str:
    if "typing_only" in case_id:
        return "api_precision"
    if "kwonly" in case_id:
        return "api_kwonly"
    if case_id.startswith("det.test001") and "API-001" in (failure.get("actual_rules") or []):
        return "api_precision"
    if "ssh_id_ed25519" in case_id:
        return "path_ssh"
    if "sec002_not_blocking" in case_id:
        return "sec002_severity"
    if "high_risk_approved_by_human" in case_id:
        return "gate_high_risk"
    if "bot_approval" in case_id:
        return "bot_approval"
    if "approval_without_binding" in case_id:
        return "unbound_approval"
    rules = failure.get("actual_rules") or failure.get("expected_rules") or []
    return str(rules[0]) if rules else f"{failure.get('layer')}:{failure.get('case_id')}"


CATALOG = {
    "api_precision": {
        "title": "API-001 误报：测试函数与类型注解被当成契约破坏",
        "severity": SEVERITY["noise"],
        "metric": "API-001 precision（当前 0.36）",
        "fix": "`engine/api_contract.py::extract_signatures` 跳过 `is_test_path()`，"
               "签名比较只取参数名/顺序/默认值，注解归一化后再比。",
        "gain": "测试重构与纯类型标注不再被拦截；TEST-001 仍是测试变更的唯一裁决者",
    },
    "api_kwonly": {
        "title": "签名提取忽略 keyword-only / 默认值，真实破坏被漏检",
        "severity": SEVERITY["detect"],
        "metric": "API-001 recall（当前 0.8）",
        "fix": "`extract_signatures` 纳入 `node.args.kwonlyargs`、`kw_defaults`、`defaults`，"
               "并对可变默认值单独标记。",
        "gain": "kw-only/默认值变更可被识别",
    },
    "path_ssh": {
        "title": "敏感路径规则漏掉嵌套的 SSH 私钥",
        "severity": SEVERITY["security"],
        "metric": "PATH-002 recall（当前 0.89）",
        "fix": "`FORBIDDEN_PATTERNS` 增加 `**/id_rsa`、`**/id_ed25519`、`**/*.pem`、`**/*.key`，"
               "并补对应回归用例。",
        "gain": "嵌套密钥路径命中；误报由路径规则白名单控制",
    },
    "sec002_severity": {
        "title": "SEC-002 危险调用是 major 却不阻塞合并",
        "severity": SEVERITY["security"],
        "metric": "危险调用拦截率（当前 0/21 阻塞）",
        "fix": "统一 `static_scan._finding` 与 `rules._f` 的阻塞语义：major 默认 blocking，"
               "或在 policy 增加 `severity_blocking` 映射并显式声明。",
        "gain": "eval/exec/os.system 等在门控层生效，而不是只出现在报告里",
    },
    "gate_high_risk": {
        "title": "高风险路径 blocker 无条件追加，审批够了也永远不放行",
        "severity": SEVERITY["gate"],
        "metric": "merge_gate 准确率（当前 0.975）",
        "fix": "`merge_blockers` 里改为 `if high_risk and len(valid) < 2:` 才追加；"
               "或在 required 里统一表达，不重复产出 blocker 文案。",
        "gain": "高风险路径在满足 2 名审批后可以合并，避免永久 BLOCKED_HUMAN",
    },
    "bot_approval": {
        "title": "机器人审批被计入人工审批",
        "severity": SEVERITY["security"],
        "metric": "审批合规率（当前 7/8）",
        "fix": "`valid_approvals` 过滤 `[bot]` 后缀与 `github-actions`/`codeguard` 固定账号；"
               "与 README「机器人永不冒充人工批准」对齐。",
        "gain": "消除机器人与人类审批混淆，T1/T2 门控不可被 bot 满足",
    },
    "unbound_approval": {
        "title": "未绑定 sha/tree_hash 的审批仍然生效",
        "severity": SEVERITY["security"],
        "metric": "审批绑定率（当前 7/8）",
        "fix": "`valid_approvals` 要求 sha 与 tree_hash 同时存在且匹配；"
               "对历史审批显式标记 legacy 并 fail closed。",
        "gain": "代码变更后旧审批必然失效，避免“审批漂移”",
    },
}


def _item(key: str, failures: list[dict]) -> dict:
    meta = CATALOG.get(key)
    rules = sorted({rule for item in failures for rule in (item.get("actual_rules") or [])
                    + (item.get("expected_rules") or [])})
    title = meta["title"] if meta else f"{key} 相关失败"
    severity = meta["severity"] if meta else 3
    weights = [float(item.get("weight", 1.0) or 1.0) for item in failures]
    impact = round(severity * len(failures) * (mean(weights) if weights else 1.0), 2)
    return {
        "id": key,
        "title": title,
        "severity": severity,
        "priority": f"P{max(0, 5 - severity)}",
        "impact": impact,
        "case_count": len(failures),
        "rule_ids": rules,
        "layer": failures[0].get("layer", "") if failures else "",
        "evidence_cases": [item["case_id"] for item in failures[:8]],
        "root_cause": meta["fix"] if meta else "需要人工定位失败用例的判定分支",
        "metric": meta["metric"] if meta else "失败用例数",
        "expected_gain": meta["gain"] if meta else "失败用例归零",
        "verify": "python -m eval.run_matrix --case <case_id> --out reports/eval_rerun",
    }


def _observations(card: dict) -> list[dict]:
    items: list[dict] = []
    semantic = card["dimensions"]["semantic"]
    if semantic["score"] is None:
        items.append({
            "id": "semantic.live_missing",
            "title": "语义层没有基线：离线跑批不伪造模型结果",
            "impact": None,
            "action": "配好 LLM key 后执行 `python -m eval.run_matrix --layer semantic --live "
                      "--out reports/eval_live`，再合并评分卡。",
            "reason": semantic.get("skip_reason", ""),
        })
    e2e = card["dimensions"]["e2e"]
    if e2e["cases"] and e2e["cost_cny"]["total"] == 0:
        items.append({
            "id": "e2e.cost_missing",
            "title": "全链路成本/时延仍是 fake 沙箱基线",
            "impact": None,
            "action": "用 `--live` 跑 e2e 采样（repeat>=3），记录 CNY、墙钟、轮次分布。",
            "reason": "offline 模式使用 fake LLM/fake 沙箱，成本恒为 0",
        })
    if not any(item["layer"] == "external" for item in card.get("failures", [])):
        items.append({
            "id": "external.not_materialized",
            "title": "外部有效性尚未验证",
            "impact": None,
            "action": "按 `python -m eval.fetch_external --plan swebench_lite` 固化快照，"
                      "再跑 external 层。",
            "reason": "manifest 中 snapshot_ready=false，外部层按约定跳过",
        })
    return items


def _enrich_with_rule_noise(items: list[dict], rows: list[dict]) -> None:
    """把"整体通过但夹带误报/漏报"的用例补进证据，否则 precision 问题会被低估。"""
    for item in items:
        # 只把"规则级误报/漏报"聚合到 api_precision；具体子问题（kw-only）保持单一证据集。
        if item["id"] != "api_precision":
            continue
        rules = set(item.get("rule_ids") or [])
        if not rules:
            continue
        noisy = []
        for row in rows:
            # 规则级 P/R 只在 det 层统计；e2e 的规则命中用于门控断言，不混入 precision。
            if row.get("layer") != "det":
                continue
            expected = set(row.get("expected_rules") or [])
            actual = set(row.get("all_rules") or [])
            if (expected & rules) != (actual & rules):
                noisy.append(row["case_id"])
        if not noisy:
            continue
        merged = list(dict.fromkeys(list(item["evidence_cases"]) + noisy))
        item["evidence_cases"] = merged[:8]
        item["case_count"] = max(item["case_count"], len(noisy))
        item["impact"] = round(item["severity"] * item["case_count"], 2)


def build(card: dict, rows: list[dict] | None = None) -> dict:
    grouped: dict[str, list[dict]] = {}
    for failure in card.get("failures", []):
        key = _root_cause(failure["case_id"], failure)
        grouped.setdefault(key, []).append(failure)
    def order(item: dict) -> tuple:
        return (-item["severity"], -item["impact"], item["id"])

    items = sorted((_item(key, group) for key, group in grouped.items()), key=order)
    if rows:
        _enrich_with_rule_noise(items, rows)
        items.sort(key=order)
    return {
        "schema_version": 1,
        "composite": (card.get("composite") or {}).get("score"),
        "failures_total": len(card.get("failures", [])),
        "items": items,
        "observations": _observations(card),
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# 优化方向（按影响排序）",
        "",
        f"- 综合分：{report['composite']}",
        f"- 失败用例：{report.get('failures_total', 0)}"
        f"（优化项覆盖 {sum(item['case_count'] for item in report['items'])} 条次，含同一用例的多规则贡献）",
        "",
        "| # | 优先级 | 优化项 | 严重度 | 用例数 | impact |",
        "|---|---|---|---|---|---|",
    ]
    for index, item in enumerate(report["items"], 1):
        lines.append(
            f"| {index} | {item['priority']} | {item['title']} | {item['severity']} | "
            f"{item['case_count']} | {item['impact']} |"
        )
    lines += ["", "## 明细", ""]
    for index, item in enumerate(report["items"], 1):
        lines += [
            f"### {index}. {item['title']}",
            "",
            f"- 根因：{item['root_cause']}",
            f"- 影响指标：{item['metric']}",
            f"- 预期改善：{item['expected_gain']}",
            f"- 证据用例：{', '.join(f'`{case}`' for case in item['evidence_cases'])}",
            f"- 复现：`{item['verify']}`",
            "",
        ]
    if report["observations"]:
        lines += ["## 待补测（不计入 impact）", ""]
        for item in report["observations"]:
            lines.append(f"- **{item['title']}**：{item['action']}（{item['reason']}）")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.report_optimization")
    parser.add_argument("--input", default="reports/eval_baseline/scorecard.json")
    parser.add_argument("--results", default="", help="默认读 scorecard 同目录的 results.jsonl")
    parser.add_argument("--out", default="reports/eval_baseline")
    parser.add_argument("--top", type=int, default=0)
    args = parser.parse_args(argv)
    card_path = Path(args.input)
    card = json.loads(card_path.read_text(encoding="utf-8"))
    results_path = Path(args.results) if args.results else card_path.parent / "results.jsonl"
    rows: list[dict] = []
    if results_path.is_file():
        rows = [
            json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    report = build(card, rows)
    if args.top:
        report["items"] = report["items"][: args.top]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "optimization.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (out / "optimization.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(
        {"items": [(item["id"], item["impact"]) for item in report["items"]],
         "observations": [item["id"] for item in report["observations"]]},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
