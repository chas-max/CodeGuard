"""跨文件语义检测 Agent：LLM 发现静态规则覆盖不到的逻辑/设计问题。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..models import (
    FindingCategory,
    SemanticFinding,
    SemanticReport,
    Severity,
)
from ..prompts.semantic_detector import semantic_detector_prompt
from ..prompts.tools import tool_protocol_block
from ..tools.spec import ToolBox, ToolLimits
from .base import call_json_with_tools

ASPECTS = ["cross_file_contract", "data_flow_and_concurrency", "architecture_and_completeness"]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    findings: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def _norm_path(raw: object) -> str:
    text = str(raw).replace("\\", "/")
    return text[2:] if text.startswith("./") else text


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def _to_finding(raw: dict, *, root: Path, repo_files: set[str], threshold: float) -> SemanticFinding | None:
    try:
        paths = [_norm_path(p) for p in (raw.get("paths") or [])]
        if not paths or any(p not in repo_files for p in paths):
            return None
        evidence_items = raw.get("evidence") or []
        evidence_texts: list[str] = []
        for ev in evidence_items:
            if isinstance(ev, str):
                evidence_texts.append(ev)
                continue
            if not isinstance(ev, dict):
                continue
            path = _norm_path(ev.get("path", ""))
            line = int(ev.get("line", 0) or 0)
            if path not in repo_files:
                return None
            max_line = _line_count(root / path)
            if line < 0 or (max_line and line > max_line):
                return None
            snippet = str(ev.get("snippet", ""))[:300]
            evidence_texts.append(f"{path}:{line} {snippet}".strip())
        if not evidence_texts:
            return None
        severity_raw = str(raw.get("severity", "minor")).lower()
        severity = Severity(severity_raw) if severity_raw in Severity._value2member_map_ else Severity.minor
        category_raw = str(raw.get("category", "other")).lower()
        category = FindingCategory(category_raw) if category_raw in FindingCategory._value2member_map_ else FindingCategory.other
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0) or 0.0)))
        blocking = confidence >= threshold and severity in (Severity.critical, Severity.major)
        return SemanticFinding(
            category=category,
            severity=severity,
            blocking=blocking,
            confidence=confidence,
            paths=paths,
            symbols=[str(s) for s in (raw.get("symbols") or [])][:20],
            call_chain=[str(s) for s in (raw.get("call_chain") or [])][:20],
            message=str(raw.get("explanation") or raw.get("message") or "")[:1000],
            evidence="；".join(evidence_texts)[:2000],
            suggestion=str(raw.get("recommendation") or raw.get("suggestion") or "")[:1000],
            recommendation=str(raw.get("recommendation") or "")[:1000],
            path=paths[0],
            line=int((evidence_items[0].get("line", 0) if isinstance(evidence_items[0], dict) else 0) or 0),
        )
    except Exception:  # noqa: BLE001 - 单条 finding 校验失败不应拖垮整次检测
        return None


def detect_semantics(
    ctx,
    *,
    root: Path,
    repo_map: dict,
    repo_context: str,
    diff_text: str,
    changed_files: list[str],
    deterministic_findings: list[str],
    aspects: list[str] | None = None,
    threshold: float = 0.8,
    tools: ToolBox | None = None,
    limits: ToolLimits | None = None,
) -> tuple[SemanticReport, list[dict], list[dict]]:
    """按 aspect 调用语义检测；每个 finding 做路径/行号 grounding。

    返回 `(report, costs, tool_events)`；未注入工具时 tool_events 为空，
    提示词与改造前逐字一致。
    """
    limits = limits or ToolLimits()
    repo_files = {item["path"] for item in repo_map.get("files", [])}
    chosen = aspects or (ASPECTS if len(changed_files) > 10 else [ASPECTS[0]])
    all_findings: list[SemanticFinding] = []
    limitations: list[str] = []
    costs: list[dict] = []
    tool_events: list[dict] = []
    tools_block = tool_protocol_block(tools.specs(), limits) if tools else ""
    for aspect in chosen:
        prompt = semantic_detector_prompt(
            aspect=aspect,
            repo_context=repo_context,
            diff_text=diff_text,
            changed_files=changed_files,
            deterministic_findings=deterministic_findings,
            tools_block=tools_block,
        )
        payload, c, events = call_json_with_tools(
            ctx, "semantic_detector", prompt, _Payload,
            tools=tools, limits=limits, temperature=0.2,
        )
        costs.extend(c)
        tool_events.extend(events)
        limitations.extend(payload.limitations)
        for raw in payload.findings:
            finding = _to_finding(raw, root=root, repo_files=repo_files, threshold=threshold)
            if finding is None:
                limitations.append(f"{aspect}: 丢弃无法 grounding 的 finding")
                continue
            all_findings.append(finding)
    # 去重
    by_id: dict[str, SemanticFinding] = {f.id: f for f in all_findings}
    confidence = (
        sum(f.confidence for f in by_id.values()) / len(by_id) if by_id else 0.0
    )
    return (
        SemanticReport(
            findings=sorted(by_id.values(), key=lambda f: f.id),
            scanned_scope=list(changed_files),
            limitations=limitations[:50],
            confidence=round(confidence, 3),
        ),
        costs,
        tool_events,
    )


__all__ = ["detect_semantics", "ASPECTS"]
