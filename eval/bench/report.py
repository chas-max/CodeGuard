"""报告层：JSON 与 Markdown 同源，固定排序，敏感信息脱敏。

报告分栏呈现三轨证据：外部主分（primary）、自建补充分（supplementary）、
诊断（diagnostic）。任意维度缺失时仍输出已有 F 的分数，`total` 留空；
禁止输出“外部 + 自建”的单一综合分。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .dedup import DedupResult
from .models import (
    CAPABILITIES,
    CAPABILITY_NAMES,
    COVERAGE_TARGETS,
    INTEGRATION,
    PRIMARY_REQUIRED,
    SUPPLEMENTARY_WEIGHTED,
    Observation,
    RunMeta,
)
from .registry import Registry
from .scoring import Evaluation, compare
from .thresholds import Thresholds

_SECRET_PATTERNS = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._-]{20,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)\b(token|secret|password|api[_-]?key)\s*[:=]\s*[^\s]+"),
     r"\1=[REDACTED]"),
)
_MAX_DETAIL = 2000


def mask_secrets(text: str) -> str:
    result = str(text or "")
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _truncate(text: str, limit: int = _MAX_DETAIL) -> str:
    masked = mask_secrets(text)
    return masked if len(masked) <= limit else masked[:limit] + "…"


def _dataset_rows(registry: Registry, observations: list[Observation],
                  active: set[str] | None = None) -> list[dict]:
    counts = Counter(obs.dataset for obs in observations)
    ok = Counter(obs.dataset for obs in observations if obs.status == "ok")
    skipped = Counter(obs.dataset for obs in observations if obs.status == "skipped")
    errors = Counter(obs.dataset for obs in observations if obs.status == "error")
    rows: list[dict] = []
    for entry in registry.entries:
        if active is not None and entry.name not in active:
            continue
        rows.append({
            "name": entry.name,
            "adapter": entry.adapter,
            "kind": entry.kind,
            "role": entry.role,
            "primary": entry.is_primary,
            "license": entry.license,
            "status": entry.status,
            "grade": entry.quality_grade,
            "scorable": entry.is_scorable,
            "capabilities": dict(entry.capabilities),
            "verification": entry.verification,
            "observations": counts.get(entry.name, 0),
            "ok": ok.get(entry.name, 0),
            "skipped": skipped.get(entry.name, 0),
            "errors": errors.get(entry.name, 0),
        })
    return rows


def _coverage_matrix(evaluation: Evaluation, registry: Registry) -> list[dict]:
    """每个 F 维的来源、样本数、状态与许可证；缺失维度也要出现。"""
    rows: list[dict] = []
    for capability in CAPABILITIES:
        merged: dict[tuple[str, str], dict] = {}
        for track_name, result in evaluation.tracks.items():
            item = result.functions.get(capability)
            if item is None:
                continue
            for source in item.sources:
                key = (source.dataset, source.metric)
                row = merged.get(key)
                if row is None:
                    entry = registry.maybe_get(source.dataset)
                    merged[key] = {
                        "track": track_name,
                        "tracks": [track_name],
                        "dataset": source.dataset,
                        "metric": source.metric,
                        "score": round(source.score, 4),
                        "weight": round(source.weight, 6),
                        "samples": source.sample_size,
                        "role": entry.role if entry else "",
                        "license": entry.license if entry else "",
                    }
                elif track_name not in row["tracks"]:
                    row["tracks"].append(track_name)
        sources = list(merged.values())
        item = evaluation.functions[capability]
        rows.append({
            "capability": capability,
            "name": CAPABILITY_NAMES[capability],
            "track": "primary" if capability in PRIMARY_REQUIRED else "integration",
            "status": item.status,
            "score": None if item.score is None else round(item.score, 4),
            "samples": item.sample_size,
            "target": COVERAGE_TARGETS.get(capability, 0),
            "small_sample": item.small_sample,
            "reason": item.reason,
            "sources": sources,
            "licenses": sorted({src["license"] for src in sources if src["license"]}),
        })
    return rows


def _evidence_mix(evaluation: Evaluation, registry: Registry,
                  observations: list[Observation], task_mix: dict | None = None) -> dict:
    mix = dict(evaluation.evidence_mix)
    counts = {"external": 0, "self_built": 0}
    for obs in observations:
        entry = registry.maybe_get(obs.dataset)
        key = "external" if entry is not None and entry.kind == "external" else "self_built"
        counts[key] += 1
    mix["observations"] = counts
    tasks = dict((task_mix or {}).get("tasks") or counts)
    total = sum(tasks.values())
    mix["tasks"] = tasks
    mix["task_share"] = dict(
        (task_mix or {}).get("share")
        or {key: (round(value / total, 6) if total else 0.0) for key, value in tasks.items()}
    )
    mix["env_error"] = sum(
        1 for obs in observations if (obs.payload or {}).get("env_error")
    )
    return mix


def build_report(
    *,
    evaluation: Evaluation,
    records: list,
    registry: Registry,
    run_meta: RunMeta,
    dedup: DedupResult,
    thresholds: Thresholds,
    observations: list[Observation] | None = None,
    baseline: dict | None = None,
    task_mix: dict | None = None,
) -> dict:
    items = list(observations or [])
    skipped = [
        {
            "task_id": obs.task_id, "dataset": obs.dataset,
            "reason": _truncate(obs.skip_reason or "未说明"),
        }
        for obs in items if obs.status == "skipped"
    ]
    errors = [
        {
            "task_id": obs.task_id, "dataset": obs.dataset,
            "error": _truncate(obs.error or "unknown"),
        }
        for obs in items if obs.status == "error"
    ]
    sources: list[dict] = []
    for track_name, result in evaluation.tracks.items():
        for capability in CAPABILITIES:
            item = result.functions.get(capability)
            if item is None:
                continue
            for source in item.sources:
                payload = source.to_dict()
                payload["track"] = track_name
                sources.append(payload)

    tracks = {name: result.to_dict() for name, result in evaluation.tracks.items()}
    primary = evaluation.tracks["primary"]
    supplementary = evaluation.tracks["supplementary"]
    integration = evaluation.tracks["integration"]

    warnings = list(evaluation.warnings)
    env_errors = sum(1 for obs in items if (obs.payload or {}).get("env_error"))
    if env_errors:
        warnings.append(
            f"{env_errors} 条任务测试环境无法启动（env_error），不计入分母"
        )
    if primary.total is None and primary.status == "PARTIAL":
        missing = "、".join(primary.missing) or "—"
        warnings.append(f"主分缺少 {missing}，按规则不输出总结果")
    if run_meta.patch_source == "gold" and primary.status in ("OK", "PARTIAL"):
        warnings.append(
            "主分证据来自 gold patch，只证明 harness 正确，不代表 CodeGuard 的修复能力"
        )
    warnings = list(dict.fromkeys(warnings))

    report = {
        "schema_version": 1,
        "run": run_meta.to_dict(),
        "registry": {"sha256": registry.sha256, "path": registry.path},
        "summary": {
            "status": evaluation.status,
            "overall": evaluation.overall,
            "total": evaluation.composite["total"],
            "partial": evaluation.composite["partial"],
            "strict": evaluation.composite["strict"],
            "coverage": evaluation.composite["coverage"],
            "verdicts": dict(evaluation.verdicts),
            "invalid_reasons": list(evaluation.invalid_reasons),
            "regressions": list(evaluation.regressions),
            "enforce": thresholds.enforce,
            "primary_status": primary.status,
            "supplementary_status": supplementary.status,
            "integration_status": integration.status,
            "primary_score": primary.score,
            "supplementary_score": supplementary.score,
            "integration_score": integration.score,
        },
        "tracks": tracks,
        "coverage": {
            "value": evaluation.composite["coverage"],
            "weights": dict(evaluation.composite["weights"]),
            "missing": list(evaluation.composite["missing"]),
            "diagnostic": list(evaluation.composite["diagnostic"]),
            "targets": dict(COVERAGE_TARGETS),
        },
        "functions": {
            capability: evaluation.functions[capability].to_dict()
            for capability in CAPABILITIES
        },
        "sources": sources,
        "coverage_matrix": _coverage_matrix(evaluation, registry),
        "evidence_mix": _evidence_mix(evaluation, registry, items, task_mix),
        "datasets": _dataset_rows(
            registry, items,
            active={obs.dataset for obs in items} | {source["dataset"] for source in sources},
        ),
        "dedup": dedup.to_dict(),
        "observations": {
            "total": len(items),
            "ok": sum(1 for obs in items if obs.status == "ok"),
            "skipped": len(skipped),
            "error": len(errors),
        },
        "skipped": skipped,
        "errors": errors,
        "warnings": warnings,
        "thresholds": thresholds.to_dict(),
    }
    if baseline:
        report["comparison"] = compare(evaluation.to_dict(), baseline)
    return report


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _function_table(functions: dict, caps, floors: dict, verdicts: dict) -> list[str]:
    lines = [
        "| 能力 | 名称 | 分数 | 保守分 | 样本 | 门槛 | 判定 |",
        "|---|---|---|---|---|---|---|",
    ]
    for capability in caps:
        item = functions.get(capability) or {}
        lines.append(
            f"| {capability} | {CAPABILITY_NAMES[capability]} | {_fmt(item.get('score'))} | "
            f"{_fmt(item.get('conservative'))} | {item.get('sample_size', 0)} | "
            f"{_fmt(floors.get(capability), 0)} | {verdicts.get(capability, '—')} |"
        )
    return lines


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    tracks = report.get("tracks") or {}
    primary = tracks.get("primary") or {}
    supplementary = tracks.get("supplementary") or {}
    diagnostic = tracks.get("diagnostic") or {}
    integration = tracks.get("integration") or {}
    floors = report["thresholds"].get("floors") or {}

    lines = [
        "# CodeGuard 评测平台报告",
        "",
        f"- 运行：`{report['run'].get('run_id', '')}`（{report['run'].get('started_at', '')}）",
        f"- profile：`{report['run'].get('profile') or '—'}`",
        f"- 状态：**{summary['status']}**；结论：**{summary['overall']}**",
        f"- 外部主分：**{_fmt(summary['total'])}**"
        f"（覆盖度 {summary['coverage']:.2f}；F1–F5 齐全且 F1 达标才输出）",
        f"- 自建补充分：{_fmt(summary.get('supplementary_score'))}（SELF_BUILT_ONLY）",
        f"- 集成分：{_fmt(summary.get('integration_score'))}（S_integration，仅 F6）",
        f"- 门槛 enforcement：{'on' if summary['enforce'] else 'off（只报告，不阻断）'}",
        f"- 数据集：{len(report['datasets'])} 个；观测：{report['observations']['total']} 条"
        f"（ok {report['observations']['ok']} / skipped {report['observations']['skipped']}"
        f" / error {report['observations']['error']}）",
        "",
        "## 外部主分（Primary）",
        "",
        f"状态：**{primary.get('status', '—')}**",
        "",
    ]
    primary_caps = list(PRIMARY_REQUIRED)
    lines += _function_table(
        primary.get("functions") or {}, primary_caps, floors,
        primary.get("verdicts") or {},
    )
    if primary.get("total") is None:
        missing = "、".join(primary.get("missing") or []) or "—"
        lines += ["", f"总结果：—（缺少 {missing}，按规则不输出）"]
    else:
        lines += ["", f"总结果：**{primary['total']:.2f}**"]
    for reason in primary.get("reasons") or []:
        lines.append(f"- {reason}")
    if primary.get("note"):
        lines += ["", f"说明：{primary['note']}"]

    lines += [
        "",
        "## 自建补充分（Supplementary）",
        "",
        f"状态：**{supplementary.get('status', '—')}**；"
        f"补充分：{_fmt(supplementary.get('score'))}",
        "",
    ]
    lines += _function_table(
        supplementary.get("functions") or {},
        list(SUPPLEMENTARY_WEIGHTED) + list(INTEGRATION), floors,
        supplementary.get("verdicts") or {},
    )
    lines += ["", "说明：仅用于回归与边界，不作为项目综合能力，且不得与主分相加。"]

    lines += [
        "",
        "## 诊断（Diagnostic）",
        "",
        "| 项 | 状态 | 分数 | 样本 | 说明 |",
        "|---|---|---|---|---|",
    ]
    f1_item = (diagnostic.get("functions") or {}).get("F1") or {}
    evidence = diagnostic.get("evidence") or {}
    lines.append(
        f"| F1 探针（外部/自建） | {diagnostic.get('status', '—')} | "
        f"{_fmt(f1_item.get('score'))} | {f1_item.get('sample_size', 0)} | "
        f"外部样本 {evidence.get('external', 0)} / 自建 {evidence.get('self_built', 0)} |"
    )
    f6_item = (integration.get("functions") or {}).get("F6") or {}
    lines.append(
        f"| F6 工作流（S_integration） | {integration.get('status', '—')} | "
        f"{_fmt(integration.get('score'))} | {f6_item.get('sample_size', 0)} | "
        "无公开基准，不进入主分 |"
    )

    lines += ["", "## 覆盖矩阵", "",
              "| 能力 | 轨道 | 来源 | 指标 | 样本 | 目标 | 状态 | 许可证 |",
              "|---|---|---|---|---|---|---|---|"]
    matrix = report.get("coverage_matrix") or []
    for row in matrix:
        if not row.get("sources"):
            lines.append(
                f"| {row['capability']} | {row['track']} | — | — | 0 | "
                f"{row['target']} | {row['status']} | — |"
            )
            continue
        for source in row["sources"]:
            tracks = "/".join(source.get("tracks") or [source.get("track", "—")])
            lines.append(
                f"| {row['capability']} | {tracks} | {source['dataset']} | "
                f"{source['metric']} | {source['samples']} | {row['target']} | "
                f"{row['status']} | {source['license'] or '—'} |"
            )

    mix = report.get("evidence_mix") or {}
    lines += ["", "## 证据构成（Evidence mix）", "",
              "| 来源 | 样本数 | 权重 | 权重占比 | 任务数 | 观测数 |",
              "|---|---|---|---|---|---|"]
    for key, label in (("external", "外部"), ("self_built", "自建")):
        item = mix.get(key) or {}
        lines.append(
            f"| {label} | {item.get('samples', 0)} | {item.get('weight', 0.0)} | "
            f"{item.get('share', 0.0)} | {(mix.get('tasks') or {}).get(key, 0)} | "
            f"{(mix.get('observations') or {}).get(key, 0)} |"
        )

    warnings = report.get("warnings") or []
    lines += ["", "## 警告", ""]
    lines += [f"- {item}" for item in warnings] if warnings else ["- 无"]

    lines += ["", "## 来源贡献", "", "| 轨道 | 数据集 | 能力 | 指标 | 分数 | 权重 | 样本 |",
              "|---|---|---|---|---|---|---|"]
    for source in report["sources"]:
        lines.append(
            f"| {source.get('track', '—')} | {source['dataset']} | {source['capability']} | "
            f"{source['metric']} | {_fmt(source['score'])} | {source['weight']:.3f} | "
            f"{source['sample_size']} |"
        )
    if not report["sources"]:
        lines.append("| — | — | — | — | — | — | — |")

    lines += ["", "## 覆盖度缺口", ""]
    missing = report["coverage"]["missing"]
    if missing:
        for capability in missing:
            item = report["functions"][capability]
            lines.append(
                f"- **{capability}**（{CAPABILITY_NAMES[capability]}）："
                f"{item.get('reason') or '未覆盖'}"
            )
    else:
        lines.append("- 无，F1–F5 外部主分维度均已覆盖")

    if summary["invalid_reasons"]:
        lines += ["", "## 未输出主分的原因", ""]
        lines += [f"- {reason}" for reason in summary["invalid_reasons"]]
    if summary["regressions"]:
        lines += ["", "## 退化告警", ""]
        lines += [f"- {reason}" for reason in summary["regressions"]]

    duplicate_count = report["dedup"].get(
        "duplicate_count", len(report["dedup"].get("duplicates") or [])
    )
    lines += ["", "## 样本去重", "",
              f"- 保留 {report['dedup']['kept']} 条，重复 {duplicate_count} 条"]
    overlap = report["dedup"].get("overlap") or {}
    if overlap:
        lines += ["", "| 数据集对 | 重叠样本数 |", "|---|---|"]
        lines += [f"| {key} | {value} |" for key, value in sorted(overlap.items())]
    else:
        lines.append("- 未发现跨数据集重叠样本")

    lines += ["", "## 跳过与失败", ""]
    if report["skipped"]:
        for item in report["skipped"]:
            lines.append(f"- 跳过 `{item['task_id']}`（{item['dataset']}）：{item['reason']}")
    if report["errors"]:
        for item in report["errors"]:
            lines.append(f"- 错误 `{item['task_id']}`（{item['dataset']}）：{item['error']}")
    if not report["skipped"] and not report["errors"]:
        lines.append("- 无")

    if report.get("comparison"):
        lines += ["", "## 与基线对比", "", "| 项 | 基线 | 当前 | delta |",
                  "|---|---|---|---|"]
        for key, item in report["comparison"].items():
            lines.append(
                f"| {key} | {_fmt(item['baseline'])} | {_fmt(item['current'])} | "
                f"{item['delta']:+.2f} |"
            )

    lines += ["", "## 可复现信息", "",
              f"- git SHA：`{report['run'].get('git_sha') or 'unknown'}`",
              f"- registry sha256：`{report['registry']['sha256'][:16]}`",
              f"- 离线：{report['run'].get('offline')}；repeat：{report['run'].get('repeat')}",
              f"- 预算：{report['run'].get('budget_cny')} CNY",
              ""]
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_report(out_dir: Path | str, report: dict) -> Path:
    import json

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        target / "report.json",
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write(target / "report.md", render_markdown(report))
    return target


__all__ = ["mask_secrets", "build_report", "render_markdown", "write_report"]
