"""多视角审查结果规则综合引擎：Grounding 硬校验、跨视角融合去重、短期记忆指纹与 FixDirective 生成。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from ..models import (
    Finding,
    FindingCategory,
    FindingLifecycle,
    FindingSource,
    FixDirective,
    Severity,
    stable_content_hash,
    validate_repo_path,
)
from .findings import SEVERITY_RANK, apply_nit_cap, reconcile
from .repo_map import is_test_path

_SEVERITY_ALIASES: dict[str, str] = {
    "critical": "critical",
    "high": "critical",
    "blocker": "critical",
    "severe": "critical",
    "fatal": "critical",
    "error": "critical",
    "major": "major",
    "medium": "major",
    "moderate": "major",
    "warn": "major",
    "warning": "major",
    "important": "major",
    "minor": "minor",
    "low": "minor",
    "nit": "minor",
    "nitpick": "minor",
    "suggestion": "minor",
    "trivial": "minor",
    "info": "info",
    "note": "info",
    "hint": "info",
}

_CATEGORY_ALIASES: dict[str, str] = {
    "logic": "correctness",
    "bug": "correctness",
    "error_handling": "correctness",
    "boundary": "correctness",
    "edge_case": "correctness",
    "security": "security",
    "vuln": "security",
    "vulnerability": "security",
    "architecture": "architecture",
    "design": "design",
    "style": "style",
    "convention": "style",
    "test": "tests",
    "testing": "tests",
    "coverage": "tests",
    "api": "api_contract",
    "contract": "api_contract",
    "concurrency": "concurrency",
    "dataflow": "data_flow",
    "data_flow": "data_flow",
    "cross_file": "cross_file",
    "syntax": "syntax",
    "policy": "policy",
    "dependency": "dependency",
    "ci": "ci",
}
_VALID_CATEGORIES = {c.value for c in FindingCategory}
_BACKTICK_RE = re.compile(r"`([^`\n]{1,64})`")
_WORD_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}")
_PROTECTED_PREFIXES = (".github/workflows/", ".codeguard/")


def normalize_severity(raw: object) -> str:
    key = str(raw or "minor").strip().lower().replace("-", "_").replace(" ", "_")
    return _SEVERITY_ALIASES.get(key, "minor")


def normalize_category(raw: object) -> str:
    key = str(raw or "other").strip().lower().replace("-", "_").replace(" ", "_")
    if key in _VALID_CATEGORIES:
        return key
    return _CATEGORY_ALIASES.get(key, "other")


def normalize_finding_dict(raw: dict[str, Any], *, role: str = "") -> dict[str, Any]:
    """将单条审查输出字典规范化为符合 Finding 模型的标准结构。"""
    data = dict(raw)
    path_raw = str(data.get("path") or "").strip().replace("\\", "/")
    if path_raw.startswith("./"):
        path_raw = path_raw[2:]
    try:
        norm_path = validate_repo_path(path_raw) if path_raw else ""
    except ValueError:
        norm_path = ""
    severity = normalize_severity(data.get("severity"))
    category = normalize_category(data.get("category"))
    line_raw = data.get("line", 0)
    try:
        line = max(0, int(line_raw or 0))
    except (TypeError, ValueError):
        line = 0
    message = str(data.get("message") or data.get("explanation") or "").strip()[:1000]
    evidence = str(data.get("evidence") or "").strip()[:2000]
    suggestion = str(data.get("suggestion") or data.get("recommendation") or "").strip()[:1000]
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.8) or 0.8)))
    except (TypeError, ValueError):
        confidence = 0.8

    blocking_raw = data.get("blocking")
    if blocking_raw is None:
        blocking = severity in ("critical", "major") and category != "style"
    else:
        blocking = bool(blocking_raw) and severity in ("critical", "major")
    demoted_by = str(data.get("demoted_by") or "")
    if category == "style" and blocking:
        blocking = False
        demoted_by = demoted_by or "style"

    reported_by = list(dict.fromkeys(
        [str(r) for r in (data.get("reported_by") or []) if r]
        + ([role] if role else [])
    ))
    auto_fixable = data.get("auto_fixable")
    if auto_fixable is None:
        auto_fixable = not any(norm_path.startswith(p) for p in _PROTECTED_PREFIXES)

    source_raw = str(data.get("source") or FindingSource.semantic.value)
    source = (
        FindingSource.deterministic.value
        if source_raw == FindingSource.deterministic.value
        else FindingSource.semantic.value
    )
    fid = str(data.get("id") or "")
    if not fid:
        fid = hashlib.sha1(
            f"{source}|{data.get('rule_id', '')}|{norm_path}|{line}|{message}".encode("utf-8")
        ).hexdigest()[:16]

    return {
        "id": fid,
        "source": source,
        "rule_id": str(data.get("rule_id") or ""),
        "category": category,
        "severity": severity,
        "blocking": blocking,
        "auto_fixable": bool(auto_fixable),
        "path": norm_path,
        "line": line,
        "message": message,
        "evidence": evidence,
        "suggestion": suggestion,
        "lifecycle": str(data.get("lifecycle") or FindingLifecycle.open.value),
        "introduced_by_pr": bool(data.get("introduced_by_pr", False)),
        "demoted_by": demoted_by,
        "group": str(data.get("group") or role or ""),
        "confidence": round(confidence, 3),
        "reported_by": reported_by,
    }


def finding_fingerprint(finding: dict[str, Any]) -> str:
    """生成稳定的语义指纹，写入各视角 Reviewer 的短期记忆，防止跨轮次重复报错。"""
    path = str(finding.get("path") or "").strip()
    cat = str(finding.get("category") or "other")
    line = int(finding.get("line") or 0)
    line_bucket = line // 5 if line > 0 else 0
    msg = str(finding.get("message") or "").strip().lower()
    tokens = sorted(_WORD_RE.findall(msg))[:6]
    return f"{path}:{cat}:b{line_bucket}:{'_'.join(tokens)}"


def _file_line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def ground_finding(
    finding: dict[str, Any],
    *,
    root: Path | None,
    repo_files: set[str],
) -> tuple[bool, str]:
    """硬校验 LLM 产出是否立足真实仓库事实：路径、行号与证据缺一不可。"""
    if not str(finding.get("message") or "").strip():
        return False, "缺少问题说明 message"
    path = str(finding.get("path") or "").strip()
    if path and repo_files and path not in repo_files:
        # 若刚创建在磁盘上则放行，否则判为路径幻觉
        if root is None or not (root / path).is_file():
            return False, f"引用了不存在的仓库路径: {path}"
    line = int(finding.get("line") or 0)
    if line < 0:
        return False, f"非法行号: {line}"
    if path and line > 0 and root is not None and (root / path).is_file():
        max_line = _file_line_count(root / path)
        if max_line and line > max_line:
            return False, f"行号越界: {path}:{line} > {max_line}"
    if finding.get("blocking") and not str(finding.get("evidence") or "").strip():
        return False, "blocking finding 缺少可核验证据 evidence"
    return True, ""


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for chunk in _WORD_RE.findall((text or "").lower()):
        if chunk.isascii():
            out.add(chunk)
        elif len(chunk) <= 2:
            out.add(chunk)
        else:
            out.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return out


def _symbols(text: str) -> set[str]:
    return {m.strip().lower() for m in _BACKTICK_RE.findall(text or "") if m.strip()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _same_issue(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """判断两条 finding 是否指向同位置同根因（跨视角去重与确定性保留）。"""
    pa, pb = str(a.get("path") or ""), str(b.get("path") or "")
    if pa != pb:
        return False
    la, lb = int(a.get("line") or 0), int(b.get("line") or 0)
    if la > 0 and lb > 0 and abs(la - lb) > 6:
        return False
    ma = str(a.get("message") or "").strip().lower()
    mb = str(b.get("message") or "").strip().lower()
    if ma and ma == mb:
        return True
    ta, tb = _tokens(ma), _tokens(mb)
    tok_sim = _jaccard(ta, tb)
    overlap_coef = (len(ta & tb) / min(len(ta), len(tb))) if ta and tb else 0.0
    syms_a = _symbols(f"{ma} {a.get('evidence', '')}")
    syms_b = _symbols(f"{mb} {b.get('evidence', '')}")
    shared_syms = bool(syms_a and syms_b and (syms_a & syms_b))
    if la > 0 and la == lb:
        if shared_syms and (len(ta & tb) >= 2 or tok_sim >= 0.15 or a.get("category") == b.get("category")):
            return True
        if overlap_coef >= 0.50:
            return True
    if shared_syms and tok_sim >= 0.30:
        return True
    return tok_sim >= 0.50 or overlap_coef >= 0.65


def _merge_two(primary: dict[str, Any], secondary: dict[str, Any], *, keep_primary_core: bool) -> dict[str, Any]:
    winner = dict(primary if keep_primary_core else (
        primary if SEVERITY_RANK.get(primary["severity"], 0) >= SEVERITY_RANK.get(secondary["severity"], 0)
        else secondary
    ))
    other = secondary if winner is primary or keep_primary_core else primary
    reporters = list(dict.fromkeys(
        list(primary.get("reported_by") or []) + list(secondary.get("reported_by") or [])
    ))
    best_sev = (
        primary["severity"]
        if SEVERITY_RANK.get(primary["severity"], 0) >= SEVERITY_RANK.get(secondary["severity"], 0)
        else secondary["severity"]
    )
    if keep_primary_core and primary.get("source") == FindingSource.deterministic.value:
        winner["blocking"] = bool(primary.get("blocking"))
        winner["auto_fixable"] = bool(primary.get("auto_fixable"))
    else:
        winner["severity"] = best_sev
        winner["blocking"] = bool(primary.get("blocking") or secondary.get("blocking"))
        if winner.get("category") == "style":
            winner["blocking"] = False
    ev_parts = [p for p in dict.fromkeys([primary.get("evidence", ""), secondary.get("evidence", "")]) if p]
    sug_parts = [p for p in dict.fromkeys([primary.get("suggestion", ""), secondary.get("suggestion", "")]) if p]
    winner["evidence"] = " | ".join(ev_parts)[:2000]
    winner["suggestion"] = "；".join(sug_parts)[:1000]
    winner["reported_by"] = reporters
    base_conf = max(float(primary.get("confidence", 0.8)), float(secondary.get("confidence", 0.8)))
    winner["confidence"] = round(min(1.0, base_conf + 0.08 * max(0, len(reporters) - 1)), 3)
    if not winner.get("line") and other.get("line"):
        winner["line"] = other["line"]
    return winner


def synthesize_reviewer_outputs(
    *,
    reviewer_outputs: dict[str, dict[str, Any]],
    deterministic_findings: list[dict[str, Any]],
    previous_findings: list[dict[str, Any]],
    root: Path | None,
    repo_files: set[str],
    max_nits: int = 5,
    block_threshold: float = 0.8,
    round_no: int = 1,
    allowed_source_files: list[str],
    candidate_test_files: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], FixDirective, list[str]]:
    """对 4 个并行 Reviewer 输出执行 Grounding、跨视角去重、确定性规则合并并生成 FixDirective。"""
    notes: list[str] = []
    det_items = [normalize_finding_dict(item) for item in deterministic_findings]
    sem_items: list[dict[str, Any]] = []

    for role in sorted(reviewer_outputs):
        out = reviewer_outputs.get(role) or {}
        for raw in out.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            item = normalize_finding_dict(raw, role=role)
            ok, reason = ground_finding(item, root=root, repo_files=repo_files)
            if not ok:
                notes.append(f"{role} 丢弃未 grounding 问题: {reason}")
                continue
            if item["confidence"] < block_threshold and item["blocking"]:
                item["blocking"] = False
                item["demoted_by"] = "low_confidence"
            if item["severity"] in ("minor", "info") and item["blocking"]:
                item["blocking"] = False
                item["demoted_by"] = "severity"
            # 若阻塞但缺少 evidence，validate Finding 前兜底检查已拦下；此处再转 Model 确认 schema 合规
            try:
                validated = Finding.model_validate(item).model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{role} finding schema 校验失败已跳过: {exc}")
                continue
            sem_items.append(validated)

    # 1) 保留全部确定性问题作为权威锚点，LLM 同根因重复项合并进去但不改写确定性门控属性
    merged_det = [dict(d) for d in det_items]
    unique_sem: list[dict[str, Any]] = []
    for item in sem_items:
        matched_det = False
        for idx, det in enumerate(merged_det):
            if _same_issue(det, item):
                merged_det[idx] = _merge_two(det, item, keep_primary_core=True)
                matched_det = True
                break
        if matched_det:
            continue
        merged_sem = False
        for idx, existing in enumerate(unique_sem):
            if _same_issue(existing, item):
                unique_sem[idx] = _merge_two(existing, item, keep_primary_core=False)
                merged_sem = True
                break
        if not merged_sem:
            unique_sem.append(item)

    combined = merged_det + unique_sem
    capped = apply_nit_cap(combined, max_nits)
    reconciled, newly_resolved = reconcile(previous_findings, capped)

    open_blockers = [
        item for item in reconciled
        if item.get("lifecycle", "open") == "open" and item.get("blocking")
    ]
    fixable: list[dict[str, Any]] = []
    non_fixable: list[dict[str, Any]] = []
    test_addition_requested = False

    for idx, item in enumerate(open_blockers, start=1):
        tagged = {**item, "directive_item_id": f"F-{idx:03d}"}
        path = str(item.get("path") or "")
        reporters = set(item.get("reported_by") or [])
        is_protected = any(path.startswith(prefix) for prefix in _PROTECTED_PREFIXES)
        if not item.get("auto_fixable", True) or is_protected:
            non_fixable.append(tagged)
            continue
        if (
            "review_tests" in reporters
            or item.get("category") == "tests"
            or (path and is_test_path(path))
        ):
            test_addition_requested = True
        fixable.append(tagged)

    # 只有 review_tests 或测试类问题需要补测时，才授权 Coder 向指定测试文件追加用例
    allowed_tests: list[str] = []
    if test_addition_requested:
        for item in fixable:
            p = str(item.get("path") or "")
            if p and is_test_path(p) and p not in allowed_tests:
                allowed_tests.append(p)
        for p in candidate_test_files or []:
            if p and is_test_path(p) and p not in allowed_tests:
                allowed_tests.append(p)

    clean_sources = [p for p in dict.fromkeys(allowed_source_files) if p and not is_test_path(p)]
    for item in fixable:
        p = str(item.get("path") or "")
        if p and not is_test_path(p) and p not in clean_sources:
            clean_sources.append(p)

    needs_mod = bool(fixable)
    acceptance: list[str] = [
        f"解决 {item['directive_item_id']} ({item.get('path') or 'global'}:{item.get('line', 0)}): "
        f"{str(item.get('message') or '')[:160]}"
        for item in fixable
    ]
    if allowed_tests:
        acceptance.append("允许向授权测试文件新增用例，但严禁删除或削弱任何已有测试函数与断言。")

    directive_id = stable_content_hash(
        round_no,
        "|".join(str(i.get("id") or "") for i in fixable),
    )[:12]
    directive = FixDirective(
        directive_id=f"dir_{directive_id}",
        round_no=round_no,
        needs_modification=needs_mod,
        target_findings=fixable,
        non_fixable_blockers=non_fixable,
        allowed_source_files=clean_sources,
        allowed_test_addition_files=allowed_tests[:10],
        acceptance_criteria=acceptance,
        summary=(
            f"第 {round_no} 轮综合结论：待自动修复阻塞项 {len(fixable)} 条，"
            f"不可自动修复项 {len(non_fixable)} 条。"
        ),
    )
    return reconciled, newly_resolved, directive, notes


__all__ = [
    "normalize_severity",
    "normalize_category",
    "normalize_finding_dict",
    "finding_fingerprint",
    "ground_finding",
    "synthesize_reviewer_outputs",
]
