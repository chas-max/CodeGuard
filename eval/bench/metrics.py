"""指标库：P/R/F1、Wilson、Macro-F1、定位匹配、pytest 输出解析。

所有函数都是纯函数，输入 0..1 或计数，输出可序列化的 dict / tuple。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from statistics import median

Z = 1.96
_SEVERITIES = {
    "critical": "high", "blocker": "high", "high": "high", "major": "high",
    "medium": "medium", "minor": "low", "low": "low", "nit": "low", "info": "low",
}
_PATH_PREFIXES = ("a/", "b/", "./")
_PYTEST_STATUSES = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")


def ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def prf(tp: float, fp: float, fn: float) -> dict:
    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    f1 = ratio(2 * precision * recall, precision + recall) if precision + recall else 0.0
    return {
        "tp": float(tp), "fp": float(fp), "fn": float(fn),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "support": float(tp + fn),
    }


def wilson(successes: float, total: float, z: float = Z) -> tuple[float, float]:
    """二项比例 Wilson 95% 区间；total<=0 返回 (0, 0)。"""
    if total <= 0:
        return (0.0, 0.0)
    n = float(total)
    phat = float(successes) / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, center - margin), 6), round(min(1.0, center + margin), 6))


def normalize_severity(value: str) -> str:
    return _SEVERITIES.get(str(value or "").strip().lower(), "low")


def macro_f1(pairs: list[tuple[str, str]]) -> tuple[float, dict[str, dict], int]:
    """多分类 Macro-F1。`pairs` 是 (gold, predicted) 的已匹配对。"""
    if not pairs:
        return 0.0, {}, 0
    labels = sorted({gold for gold, _ in pairs} | {pred for _, pred in pairs})
    per_label: dict[str, dict] = {}
    for label in labels:
        tp = sum(1 for gold, pred in pairs if gold == label and pred == label)
        fp = sum(1 for gold, pred in pairs if gold != label and pred == label)
        fn = sum(1 for gold, pred in pairs if gold == label and pred != label)
        per_label[label] = prf(tp, fp, fn)
    macro = sum(stats["f1"] for stats in per_label.values()) / len(labels)
    return round(macro, 6), per_label, len(labels)


def _normalize_path(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip()
    for prefix in _PATH_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.lstrip("/")


def _line_of(item: dict) -> int:
    try:
        return int(item.get("line") or item.get("start_line") or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class MatchResult:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    pairs: list[tuple[dict, dict]] = field(default_factory=list)
    matched_gold: int = 0

    @property
    def total_gold(self) -> int:
        return self.tp + self.fn


def match_findings(predicted: list[dict], gold: list[dict], *, window: int = 3) -> MatchResult:
    """按 path + 行号窗口贪心配对；路径先统一成相对 posix 形式。"""
    result = MatchResult()
    remaining = list(predicted or [])
    for gold_item in gold or []:
        gold_path = _normalize_path(gold_item.get("path", ""))
        gold_line = _line_of(gold_item)
        chosen = -1
        for index, pred_item in enumerate(remaining):
            if _normalize_path(pred_item.get("path", "")) != gold_path:
                continue
            if abs(_line_of(pred_item) - gold_line) <= max(0, int(window)):
                chosen = index
                break
        if chosen >= 0:
            result.tp += 1
            result.matched_gold += 1
            result.pairs.append((gold_item, remaining.pop(chosen)))
        else:
            result.fn += 1
    result.fp = len(remaining)
    return result


def localization_accuracy(predicted: list[dict], gold: list[dict], *, window: int = 3) -> float:
    if not gold:
        return 0.0
    return ratio(match_findings(predicted, gold, window=window).matched_gold, len(gold))


def parse_pytest_rA(text: str) -> dict[str, str]:
    """解析 `pytest -rA` 的短摘要，兼容 "PASSED node" 与 "node PASSED" 两种行。"""
    statuses: dict[str, str] = {}
    mapping = {
        "passed": "passed", "failed": "failed", "error": "error",
        "skipped": "skipped", "xfail": "skipped", "xpass": "passed",
    }
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("=", "-", "_")):
            continue
        head, _, rest = line.partition(" ")
        if head.upper() in _PYTEST_STATUSES and rest:
            node = rest.split(" - ", 1)[0].strip()
            status = mapping[head.lower()]
        else:
            node, _, tail = line.rpartition(" ")
            if not node or tail.upper() not in _PYTEST_STATUSES:
                continue
            status = mapping[tail.lower()]
        if "::" not in node and "/" not in node:
            continue
        statuses[node.strip()] = status
    return statuses


def rate_record_counts(flags: list[bool]) -> tuple[float, float]:
    return float(sum(1 for flag in flags if flag)), float(len(flags))


def median_iqr(values: list[float]) -> dict:
    if not values:
        return {"median": 0.0, "iqr": 0.0, "n": 0}
    ordered = sorted(float(item) for item in values)
    mid = median(ordered)
    if len(ordered) == 1:
        return {"median": round(mid, 4), "iqr": 0.0, "n": 1}
    q1 = ordered[max(0, int(round(0.25 * (len(ordered) - 1))))]
    q3 = ordered[max(0, int(round(0.75 * (len(ordered) - 1))))]
    return {"median": round(mid, 4), "iqr": round(q3 - q1, 4), "n": len(ordered)}


def bootstrap_ci(values: list[float], *, samples: int = 1000, seed: int = 0) -> tuple[float, float]:
    """确定性 bootstrap（固定种子），样本不足时退回 (min, max)。"""
    if not values:
        return (0.0, 0.0)
    if len(values) < 5:
        return (round(min(values), 6), round(max(values), 6))
    rng = random.Random(seed)
    means = []
    for _ in range(max(1, int(samples))):
        means.append(sum(rng.choice(values) for _ in values) / len(values))
    means.sort()
    low = means[int(0.025 * (len(means) - 1))]
    high = means[int(0.975 * (len(means) - 1))]
    return (round(low, 6), round(high, 6))


__all__ = [
    "Z", "MatchResult", "ratio", "prf", "wilson", "normalize_severity", "macro_f1",
    "match_findings", "localization_accuracy", "parse_pytest_rA",
    "rate_record_counts", "median_iqr", "bootstrap_ci",
]
