"""确定性检测与 LLM 语义检测的合并策略。"""
from __future__ import annotations

from ..models import DeterministicReport, HybridScanReport, SemanticReport


def build_hybrid_report(
    deterministic: DeterministicReport,
    semantic: SemanticReport,
    *,
    tree_hash: str = "",
    semantic_block_threshold: float = 0.8,
) -> HybridScanReport:
    """确定性结论优先；语义 finding 只有在证据与置信度达标时才 blocking。"""
    return HybridScanReport.combine(
        deterministic,
        semantic,
        tree_hash=tree_hash,
        semantic_block_threshold=semantic_block_threshold,
    )


__all__ = ["build_hybrid_report"]
