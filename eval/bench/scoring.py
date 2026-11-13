"""三轨评分：外部主分（primary）→ 自建补充分（supplementary）→ 诊断（diagnostic）。

口径：
- 主分只允许 `role=primary` 且已核准的外部数据集进入，维度为 F1–F5；
  F1 是门禁维（不占权重），F6 单独输出 `S_integration`；
- 任意一个要求维度缺失时，输出其余已有 F 的分数，`total = None`，状态 `PARTIAL`，
  不再把整份报告判为 `INVALID`；
- 自建集只在 supplementary 轨道内归一化，永远不出现在主分中，也不与主分相加；
- 来源权重 `w = grade_weight × coverage`，小样本来源不进入分母（`insufficient`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .models import (
    CAPABILITIES,
    CAPABILITY_WEIGHTS,
    DIAGNOSTIC_CAPABILITIES,
    INTEGRATION,
    PRIMARY_REQUIRED,
    PRIMARY_WEIGHTED,
    SUPPLEMENTARY_WEIGHTED,
    ScoreRecord,
)
from .registry import Registry
from .thresholds import Thresholds, load_thresholds

PRIMARY = "primary"
SUPPLEMENTARY = "supplementary"
DIAGNOSTIC = "diagnostic"

STATUS_OK = "OK"
STATUS_PARTIAL = "PARTIAL"
STATUS_NO_EXTERNAL = "NO_EXTERNAL_EVIDENCE"
STATUS_SUPPLEMENTARY_ONLY = "SUPPLEMENTARY_ONLY"
STATUS_INTEGRATION_ONLY = "INTEGRATION_ONLY"
STATUS_NO_EVIDENCE = "NO_EVIDENCE"


@dataclass(frozen=True)
class SourceScore:
    dataset: str
    capability: str
    metric: str
    value: float
    score: float
    weight: float
    grade: str
    sample_size: int
    interval: tuple[float, float] | None = None
    role: str = SUPPLEMENTARY
    kind: str = ""

    def to_dict(self) -> dict:
        return {
            "dataset": self.dataset, "capability": self.capability, "metric": self.metric,
            "value": round(self.value, 6), "score": round(self.score, 4),
            "weight": round(self.weight, 6), "grade": self.grade,
            "sample_size": self.sample_size,
            "interval": list(self.interval) if self.interval else None,
            "role": self.role, "kind": self.kind,
        }


@dataclass(frozen=True)
class FunctionScore:
    capability: str
    score: float | None
    conservative: float | None
    sample_size: int
    sources: tuple[SourceScore, ...] = ()
    status: str = "missing"
    small_sample: bool = False
    raw_score: float | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "capability": self.capability,
            "score": None if self.score is None else round(self.score, 4),
            "conservative": None if self.conservative is None else round(self.conservative, 4),
            "raw_score": None if self.raw_score is None else round(self.raw_score, 4),
            "sample_size": self.sample_size,
            "status": self.status,
            "small_sample": self.small_sample,
            "reason": self.reason,
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class TrackResult:
    """一条轨道的独立结论；轨道之间不求和、不混算。"""

    track: str
    status: str
    functions: dict[str, FunctionScore]
    score: float | None
    total: float | None
    partial: float | None
    strict: float | None
    coverage: float
    weights: dict[str, float]
    missing: tuple[str, ...] = ()
    verdicts: dict[str, str] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    evidence: dict[str, int] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "track": self.track,
            "status": self.status,
            "score": None if self.score is None else round(self.score, 4),
            "total": None if self.total is None else round(self.total, 4),
            "partial": None if self.partial is None else round(self.partial, 4),
            "strict": None if self.strict is None else round(self.strict, 4),
            "coverage": round(self.coverage, 6),
            "weights": {cap: round(float(value), 6) for cap, value in self.weights.items()},
            "missing": list(self.missing),
            "verdicts": dict(self.verdicts),
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
            "note": self.note,
            "functions": {
                cap: item.to_dict() for cap, item in self.functions.items()
            },
        }


@dataclass
class Evaluation:
    functions: dict[str, FunctionScore]
    composite: dict
    status: str
    verdicts: dict[str, str]
    overall: str
    invalid_reasons: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    thresholds: Thresholds = field(default_factory=Thresholds)
    tracks: dict[str, TrackResult] = field(default_factory=dict)
    evidence_mix: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def track(self, name: str) -> TrackResult | None:
        return self.tracks.get(name)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "overall": self.overall,
            "functions": {cap: self.functions[cap].to_dict() for cap in CAPABILITIES},
            "composite": dict(self.composite),
            "verdicts": dict(self.verdicts),
            "invalid_reasons": list(self.invalid_reasons),
            "regressions": list(self.regressions),
            "thresholds": self.thresholds.to_dict(),
            "tracks": {name: result.to_dict() for name, result in self.tracks.items()},
            "evidence_mix": dict(self.evidence_mix),
            "warnings": list(self.warnings),
        }


def _role_accepted(entry, role: str | None) -> bool:
    if role is None:
        return entry.is_scorable
    if role == PRIMARY:
        return entry.is_primary
    if role == SUPPLEMENTARY:
        return entry.is_supplementary
    if role == DIAGNOSTIC:
        return entry.is_diagnostic and entry.is_scorable
    raise ValueError(f"未知轨道 {role!r}")


def source_scores(records: Iterable[ScoreRecord], registry: Registry,
                  *, role: str | None = None) -> list[SourceScore]:
    grouped: dict[tuple[str, str], list[ScoreRecord]] = {}
    for record in records:
        entry = registry.maybe_get(record.dataset)
        if entry is None or not _role_accepted(entry, role):
            continue
        grouped.setdefault((record.dataset, record.capability), []).append(record)

    sources: list[SourceScore] = []
    for (dataset, capability), rows in sorted(grouped.items()):
        entry = registry.get(dataset)
        primary = entry.primary_metric(capability)
        chosen = next((row for row in rows if row.metric == primary), None)
        if chosen is None:
            chosen = next((row for row in rows if row.metric), None)
        if chosen is None:
            continue
        weight = entry.grade_weight * entry.coverage(capability)
        if weight <= 0:
            continue
        sources.append(SourceScore(
            dataset=dataset, capability=capability, metric=chosen.metric,
            value=chosen.value, score=chosen.value * 100.0,
            weight=weight, grade=entry.quality_grade,
            sample_size=max(0, int(chosen.sample_size)),
            interval=chosen.interval, role=entry.role, kind=entry.kind,
        ))
    return sources


def function_scores(records: Iterable[ScoreRecord], registry: Registry,
                    thresholds: Thresholds | None = None, *, role: str | None = None,
                    capabilities: Iterable[str] | None = None) -> dict[str, FunctionScore]:
    limits = thresholds or load_thresholds()
    caps = tuple(capabilities or CAPABILITIES)
    grouped: dict[str, list[SourceScore]] = {}
    for source in source_scores(records, registry, role=role):
        if source.capability not in caps:
            continue
        grouped.setdefault(source.capability, []).append(source)

    result: dict[str, FunctionScore] = {}
    for capability in caps:
        items = grouped.get(capability, [])
        if not items:
            result[capability] = FunctionScore(
                capability=capability, score=None, conservative=None, sample_size=0,
                status="missing", reason="没有已核准的数据集提供该能力",
            )
            continue
        total_weight = sum(item.weight for item in items)
        if total_weight <= 0:
            result[capability] = FunctionScore(
                capability=capability, score=None, conservative=None, sample_size=0,
                status="missing", reason="来源权重为 0",
            )
            continue
        raw = sum(item.score * (item.weight / total_weight) for item in items)
        conservative = sum(
            (item.interval[0] * 100.0 if item.interval else item.score)
            * (item.weight / total_weight)
            for item in items
        )
        sample_size = sum(item.sample_size for item in items)
        small = sample_size < limits.recommended_samples
        if sample_size < limits.min_samples:
            result[capability] = FunctionScore(
                capability=capability, score=None, conservative=None,
                raw_score=raw, sample_size=sample_size, sources=tuple(items),
                status="insufficient", small_sample=True,
                reason=f"有效样本 {sample_size} < 最小样本 {limits.min_samples}，只作定性证据",
            )
            continue
        result[capability] = FunctionScore(
            capability=capability, score=raw, conservative=conservative,
            raw_score=raw, sample_size=sample_size, sources=tuple(items),
            status="scored", small_sample=small,
            reason="样本量低于建议值，结论波动可能较大" if small else "",
        )
    return result


def _verdict(score: float | None, status: str, floor: float, warn_margin: float) -> str:
    if score is None:
        return "INSUFFICIENT" if status == "insufficient" else "MISSING"
    if score >= floor:
        return "PASS"
    if score >= floor - warn_margin:
        return "WARN"
    return "FAIL"


def _primary_weights(limits: Thresholds) -> dict[str, float]:
    """把 F2/F3/F4/F5 的原始权重在轨道内归一化（F1 是门禁维，F6 不入主分）。"""
    raw = {cap: float(limits.weights.get(cap, 0.0)) for cap in PRIMARY_WEIGHTED}
    total = sum(raw.values())
    if total <= 0:
        return {cap: 0.0 for cap in PRIMARY_WEIGHTED}
    return {cap: value / total for cap, value in raw.items()}


def _weighted(functions: dict[str, FunctionScore], weights: dict[str, float],
              caps: Iterable[str]) -> tuple[float, list[str]]:
    available = [cap for cap in caps
                 if functions.get(cap) is not None and functions[cap].score is not None]
    weighted = sum(float(functions[cap].score) * weights.get(cap, 0.0) for cap in available)
    return weighted, available


def evaluate_primary(records: Iterable[ScoreRecord], registry: Registry,
                     thresholds: Thresholds | None = None) -> TrackResult:
    """外部主分：F1–F5 齐全且 F1 达标才输出 `S_primary`，否则只给已有维度。"""
    limits = thresholds or load_thresholds()
    weights = _primary_weights(limits)
    functions = function_scores(records, registry, limits, role=PRIMARY)
    weighted, available = _weighted(functions, weights, PRIMARY_WEIGHTED)
    coverage = sum(weights[cap] for cap in available)
    partial = round(weighted / coverage, 4) if coverage > 0 else None
    strict = round(weighted, 4) if coverage > 0 else None

    f1 = functions["F1"]
    f1_ok = f1.score is not None and float(f1.score) >= limits.floor("F1")
    complete = all(functions[cap].score is not None for cap in PRIMARY_REQUIRED)
    total = round(weighted / coverage, 4) if (complete and f1_ok and coverage > 0) else None
    has_evidence = any(functions[cap].status != "missing" for cap in PRIMARY_REQUIRED)
    missing = tuple(cap for cap in PRIMARY_REQUIRED if functions[cap].score is None)

    reasons: list[str] = []
    if not has_evidence:
        reasons.append(
            "F1–F5 均无外部主分证据；请先物化外部基准（fetch）并跑 --profile external"
        )
    else:
        if f1.score is None:
            reasons.append("缺少 F1 前置诊断（PR 理解），按规则不输出主分")
        elif not f1_ok:
            reasons.append(
                f"F1={f1.score:.1f} 低于合格线 {limits.floor('F1'):.1f}，按规则不输出主分"
            )
        for cap in missing:
            reasons.append(f"{cap} 缺少已核准的外部主分证据")

    if not has_evidence:
        status = STATUS_NO_EXTERNAL
    elif complete and f1_ok:
        status = STATUS_OK
    else:
        status = STATUS_PARTIAL

    verdicts = {
        cap: _verdict(functions[cap].score, functions[cap].status,
                      limits.floor(cap), limits.warn_margin)
        for cap in PRIMARY_REQUIRED
    }
    return TrackResult(
        track=PRIMARY, status=status, functions=functions, score=total, total=total,
        partial=partial, strict=strict, coverage=coverage, weights=weights,
        missing=missing, verdicts=verdicts, reasons=tuple(reasons),
        evidence=_track_evidence(functions),
        note="仅统计 role=primary 的已核准外部数据集；F1 是门禁维，F6 不入主分。",
    )


def evaluate_supplementary(records: Iterable[ScoreRecord], registry: Registry,
                           thresholds: Thresholds | None = None) -> TrackResult:
    """自建补充分：只在 supplementary 轨道内归一化，标 SELF_BUILT_ONLY。"""
    limits = thresholds or load_thresholds()
    functions = function_scores(records, registry, limits, role=SUPPLEMENTARY)
    weights = {cap: float(limits.weights.get(cap, 0.0)) for cap in SUPPLEMENTARY_WEIGHTED}
    weighted, available = _weighted(functions, weights, SUPPLEMENTARY_WEIGHTED)
    coverage = sum(weights[cap] for cap in available)
    score = round(weighted / coverage, 4) if coverage > 0 else None
    status = STATUS_SUPPLEMENTARY_ONLY if score is not None else STATUS_NO_EVIDENCE

    f1 = functions["F1"]
    verdicts = {
        cap: _verdict(item.score, item.status, limits.floor(cap), limits.warn_margin)
        for cap, item in functions.items()
    }
    reasons = tuple(
        f"{cap} 缺少自建证据" for cap in SUPPLEMENTARY_WEIGHTED
        if functions[cap].score is None
    )
    return TrackResult(
        track=SUPPLEMENTARY, status=status, functions=functions, score=score, total=None,
        partial=score, strict=score, coverage=coverage, weights=weights,
        missing=tuple(cap for cap in CAPABILITIES if functions[cap].score is None),
        verdicts=verdicts, reasons=reasons, evidence=_track_evidence(functions),
        note=(
            "该分数不作为项目综合能力，只用于回归与边界；"
            f"F1 仅作诊断（{_verdict(f1.score, f1.status, limits.floor('F1'), limits.warn_margin)}）。"
        ),
    )


def evaluate_integration(records: Iterable[ScoreRecord], registry: Registry,
                         thresholds: Thresholds | None = None) -> TrackResult:
    """F6 工作流单列：公开基准不覆盖，单独输出 `S_integration`。"""
    limits = thresholds or load_thresholds()
    functions = function_scores(records, registry, limits, capabilities=INTEGRATION)
    f6 = functions["F6"]
    score = f6.score
    verdicts = {"F6": _verdict(f6.score, f6.status, limits.floor("F6"), limits.warn_margin)}
    return TrackResult(
        track="integration", status=STATUS_INTEGRATION_ONLY if score is not None
        else STATUS_NO_EVIDENCE,
        functions=functions, score=score, total=None, partial=score, strict=score,
        coverage=1.0 if score is not None else 0.0, weights={"F6": 1.0},
        missing=() if score is not None else ("F6",), verdicts=verdicts,
        reasons=() if score is not None else ("F6 没有工作流证据",),
        evidence=_track_evidence(functions),
        note="F6 无公开基准，保留自建集成测试，不进入外部主分。",
    )


def evaluate_diagnostic(records: Iterable[ScoreRecord], registry: Registry,
                        thresholds: Thresholds | None = None) -> TrackResult:
    """诊断轨：F1 探针（外部 + 自建标记），不参与主分计算。"""
    limits = thresholds or load_thresholds()
    functions = function_scores(records, registry, limits,
                                capabilities=DIAGNOSTIC_CAPABILITIES)
    f1 = functions["F1"]
    verdicts = {"F1": _verdict(f1.score, f1.status, limits.floor("F1"), limits.warn_margin)}
    evidence = _track_evidence(functions)
    return TrackResult(
        track=DIAGNOSTIC, status=STATUS_OK if f1.score is not None else STATUS_NO_EVIDENCE,
        functions=functions, score=f1.score, total=None, partial=f1.score, strict=f1.score,
        coverage=1.0 if f1.score is not None else 0.0, weights={"F1": 0.0},
        missing=() if f1.score is not None else ("F1",), verdicts=verdicts,
        reasons=() if f1.score is not None else ("没有 F1 探针证据",),
        evidence=evidence, note="F1 只作门禁与输入解析诊断，不占主分权重。",
    )


def _track_evidence(functions: dict[str, FunctionScore]) -> dict[str, int]:
    evidence = {"external": 0, "self_built": 0}
    for item in functions.values():
        for source in item.sources:
            bucket = "external" if source.kind == "external" else "self_built"
            evidence[bucket] += int(source.sample_size)
    return evidence


def evidence_mix(records: Iterable[ScoreRecord], registry: Registry) -> dict:
    """外部 / 自建样本数与权重占比；只看可评分来源。"""
    buckets = {
        "external": {"samples": 0, "weight": 0.0, "sources": 0},
        "self_built": {"samples": 0, "weight": 0.0, "sources": 0},
    }
    for source in source_scores(records, registry):
        entry = registry.maybe_get(source.dataset)
        key = "external" if entry is not None and entry.kind == "external" else "self_built"
        buckets[key]["samples"] += int(source.sample_size)
        buckets[key]["weight"] += float(source.weight)
        buckets[key]["sources"] += 1
    total = sum(item["weight"] for item in buckets.values())
    for item in buckets.values():
        item["weight"] = round(item["weight"], 6)
        item["share"] = round(item["weight"] / total, 6) if total > 0 else 0.0
    return {**buckets, "total_weight": round(total, 6)}


def _merged_functions(primary: TrackResult, supplementary: TrackResult,
                      integration: TrackResult) -> dict[str, FunctionScore]:
    merged: dict[str, FunctionScore] = {}
    for capability in CAPABILITIES:
        if capability in INTEGRATION:
            chosen = integration.functions.get(capability)
        else:
            primary_item = primary.functions.get(capability)
            supplementary_item = supplementary.functions.get(capability)
            if primary_item is not None and primary_item.status != "missing":
                chosen = primary_item
            else:
                chosen = supplementary_item or primary_item
        if chosen is None:
            chosen = FunctionScore(
                capability=capability, score=None, conservative=None, sample_size=0,
                status="missing", reason="没有已核准的数据集提供该能力",
            )
        merged[capability] = chosen
    return merged


def _overall_status(primary: TrackResult, supplementary: TrackResult,
                    integration: TrackResult) -> str:
    if primary.status in (STATUS_OK, STATUS_PARTIAL):
        return primary.status
    has_self_built = any(
        source.capability in SUPPLEMENTARY_WEIGHTED
        for item in supplementary.functions.values() for source in item.sources
    )
    if integration.score is not None and not has_self_built:
        return STATUS_INTEGRATION_ONLY
    if supplementary.score is not None:
        return STATUS_SUPPLEMENTARY_ONLY
    return STATUS_NO_EXTERNAL


def _overall(status: str, verdicts: dict[str, str]) -> str:
    has_fail = any(value == "FAIL" for value in verdicts.values())
    has_warn = any(value == "WARN" for value in verdicts.values())
    if status == STATUS_OK:
        return "FAIL" if has_fail else ("WARN" if has_warn else "PASS")
    if status == STATUS_PARTIAL:
        return "FAIL" if has_fail else STATUS_PARTIAL
    if status == STATUS_SUPPLEMENTARY_ONLY:
        return "SELF_BUILT_ONLY"
    if status == STATUS_INTEGRATION_ONLY:
        return STATUS_INTEGRATION_ONLY
    return STATUS_NO_EXTERNAL


def _warnings(tracks: dict[str, TrackResult], registry: Registry,
              limits: Thresholds) -> list[str]:
    warnings: list[str] = []
    primary = tracks[PRIMARY]
    if primary.status == STATUS_NO_EXTERNAL:
        warnings.append("F1–F5 均无外部 primary 证据，主分不可输出")
    else:
        for cap in PRIMARY_REQUIRED:
            if primary.functions[cap].status == "missing":
                warnings.append(f"{cap} 缺少外部 primary 证据，主分不可输出")
    for name, result in tracks.items():
        for cap, item in result.functions.items():
            if item.small_sample and item.score is not None:
                warnings.append(
                    f"{name}/{cap} 样本 {item.sample_size} < 建议 "
                    f"{limits.recommended_samples}（small_sample）"
                )
    candidates = sorted(
        entry.name for entry in registry.entries
        if entry.role == PRIMARY and not entry.is_primary
    )
    if candidates:
        warnings.append("候选数据集未核验，不进入主分：" + "、".join(candidates))
    warnings.append("外部与自建证据不合并计算综合分，禁止相加。")
    return warnings


def evaluate(records: Iterable[ScoreRecord], registry: Registry,
             thresholds: Thresholds | None = None,
             baseline: dict | None = None) -> Evaluation:
    limits = thresholds or load_thresholds()
    rows = list(records)
    primary = evaluate_primary(rows, registry, limits)
    supplementary = evaluate_supplementary(rows, registry, limits)
    integration = evaluate_integration(rows, registry, limits)
    diagnostic = evaluate_diagnostic(rows, registry, limits)
    tracks = {
        PRIMARY: primary, SUPPLEMENTARY: supplementary,
        "integration": integration, DIAGNOSTIC: diagnostic,
    }
    status = _overall_status(primary, supplementary, integration)
    functions = _merged_functions(primary, supplementary, integration)

    composite = {
        "track": PRIMARY,
        "total": primary.total,
        "partial": primary.partial,
        "strict": primary.strict,
        "coverage": round(primary.coverage, 6),
        "weights": {
            cap: round(primary.weights[cap], 6)
            for cap in PRIMARY_WEIGHTED if primary.functions[cap].score is not None
        },
        "missing": list(primary.missing),
        "diagnostic": list(DIAGNOSTIC_CAPABILITIES),
        "supplementary": supplementary.score,
        "integration": integration.score,
    }

    verdicts = {
        cap: _verdict(functions[cap].score, functions[cap].status,
                      limits.floor(cap), limits.warn_margin)
        for cap in CAPABILITIES
    }

    regressions: list[str] = []
    if baseline:
        base_composite = baseline.get("composite") or {}
        base_total = base_composite.get("total")
        if base_total is None:
            base_total = base_composite.get("partial")
        current_total = primary.total if primary.total is not None else primary.partial
        if base_total is not None and current_total is not None:
            delta = float(current_total) - float(base_total)
            if delta <= -limits.regression_composite:
                regressions.append(
                    f"主分退化 {delta:+.2f}（阈值 -{limits.regression_composite:.2f}）"
                )
        base_functions = baseline.get("functions") or {}
        for cap in PRIMARY_WEIGHTED:
            base_score = (base_functions.get(cap) or {}).get("score")
            current = functions[cap].score
            if base_score is None or current is None:
                continue
            delta = float(current) - float(base_score)
            if delta <= -limits.regression_dimension:
                regressions.append(
                    f"{cap} 退化 {delta:+.2f}（阈值 -{limits.regression_dimension:.2f}）"
                )
                verdicts[cap] = "FAIL"

    overall = _overall(status, verdicts)

    return Evaluation(
        functions=functions, composite=composite, status=status, verdicts=verdicts,
        overall=overall, invalid_reasons=list(primary.reasons), regressions=regressions,
        thresholds=limits, tracks=tracks,
        evidence_mix=evidence_mix(rows, registry),
        warnings=_warnings(tracks, registry, limits),
    )


def compare(current: dict, baseline: dict) -> dict:
    """两份 `Evaluation.to_dict()` 的 delta；报告层与 CLI 共用。"""
    result: dict[str, dict] = {}
    for cap in CAPABILITIES:
        cur = ((current.get("functions") or {}).get(cap) or {}).get("score")
        old = ((baseline.get("functions") or {}).get(cap) or {}).get("score")
        if cur is None or old is None:
            continue
        result[cap] = {"baseline": old, "current": cur, "delta": round(cur - old, 4)}
    cur_total = (current.get("composite") or {}).get("total")
    old_total = (baseline.get("composite") or {}).get("total")
    if cur_total is None or old_total is None:
        cur_total = (current.get("composite") or {}).get("partial")
        old_total = (baseline.get("composite") or {}).get("partial")
    if cur_total is not None and old_total is not None:
        result["composite"] = {
            "baseline": old_total, "current": cur_total,
            "delta": round(cur_total - old_total, 4),
        }
    return result


__all__ = [
    "CAPABILITY_WEIGHTS",
    "PRIMARY", "SUPPLEMENTARY", "DIAGNOSTIC",
    "STATUS_OK", "STATUS_PARTIAL", "STATUS_NO_EXTERNAL",
    "STATUS_SUPPLEMENTARY_ONLY", "STATUS_INTEGRATION_ONLY", "STATUS_NO_EVIDENCE",
    "SourceScore", "FunctionScore", "TrackResult", "Evaluation",
    "source_scores", "function_scores",
    "evaluate_primary", "evaluate_supplementary", "evaluate_integration",
    "evaluate_diagnostic", "evidence_mix", "evaluate", "compare",
]
