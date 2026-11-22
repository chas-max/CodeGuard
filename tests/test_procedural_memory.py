from __future__ import annotations

import json
import math
from pathlib import Path

from codeguard.memory import LocalHashEmbedder, ProceduralMemoryStore
from codeguard.models import (
    PRBrief,
    ProceduralMemoryRecord,
    QACheckReport,
    ReviewSummary,
)
from codeguard.tools.memory_tools import SearchProceduralMemoryArgs, search_procedural_memory


def test_local_hash_embedder_deterministic_and_normalized():
    emb = LocalHashEmbedder(dim=128)
    v1 = emb.embed("ZeroDivisionError in divide_safe calc.py")
    v2 = emb.embed("ZeroDivisionError in divide_safe calc.py")
    v3 = emb.embed("SQL injection in auth/login.py")
    assert len(v1) == 128
    assert v1 == v2
    assert v1 != v3
    norm = math.sqrt(sum(x * x for x in v1))
    assert abs(norm - 1.0) < 1e-4


def test_store_upsert_idempotent_and_search_vec_and_numpy_fallback(tmp_path: Path):
    for fallback in (False, True):
        db_path = tmp_path / f"mem_{'np' if fallback else 'vec'}.db"
        store = ProceduralMemoryStore(
            db_path,
            embedder=LocalHashEmbedder(dim=128),
            force_numpy_fallback=fallback,
        )
        try:
            if not fallback:
                assert store.vec_enabled is True
            else:
                assert store.vec_enabled is False

            rec_ok = ProceduralMemoryRecord(
                repo="acme/calc",
                pr_number=12,
                importance_level="medium",
                reviewer_roles=["review_logic", "review_tests"],
                categories=["correctness", "tests"],
                file_patterns=["calc.py"],
                problem_pattern="divide_safe 未检查除数 b == 0 导致 ZeroDivisionError",
                fix_strategy="在除法前增加 if b == 0 返回 0.0 并补充单元测试",
                patch_summary="+ if b == 0: return 0.0",
                qa_verdict="pass",
                ci_passed=True,
                human_approved=True,
                final_outcome="MERGED",
                memory_type="fix_pattern",
            )
            rec_bad = ProceduralMemoryRecord(
                repo="acme/calc",
                pr_number=13,
                importance_level="critical",
                reviewer_roles=["review_security"],
                categories=["security"],
                file_patterns=["auth/login.py"],
                problem_pattern="SQL 字符串拼接注入漏洞",
                fix_strategy="仅做引号转义未参数化查询（被 QA 拦截）",
                patch_summary="escape quote",
                qa_verdict="revise",
                ci_passed=False,
                human_approved=False,
                final_outcome="CHANGES_REQUESTED",
                memory_type="anti_pattern",
            )
            m1 = store.upsert(rec_ok)
            m1_dup = store.upsert(rec_ok)
            m2 = store.upsert(rec_bad)
            assert m1 == m1_dup
            assert m1 != m2
            assert store.count() == 2

            fetched = store.get(m1)
            assert fetched is not None
            assert fetched["repo"] == "acme/calc"
            assert fetched["ci_passed"] is True
            assert fetched["human_approved"] is True
            assert "review_logic" in fetched["reviewer_roles"]

            # 默认 verified_only=True 不返回失败的反面教材
            hits = store.search("divide_safe ZeroDivisionError", current_repo="acme/calc", top_k=3)
            assert len(hits) == 1
            assert hits[0]["memory_id"] == m1
            assert hits[0]["memory_type"] == "fix_pattern"

            # verified_only=False + category=security 可查出反面教训
            sec_hits = store.search(
                "SQL 注入 auth/login.py",
                category="security",
                verified_only=False,
                top_k=3,
            )
            assert len(sec_hits) == 1
            assert sec_hits[0]["memory_id"] == m2
            assert sec_hits[0]["memory_type"] == "anti_pattern"
        finally:
            store.close()


def test_search_procedural_memory_tool(tmp_path: Path):
    store = ProceduralMemoryStore(tmp_path / "rag.db", embedder=LocalHashEmbedder(dim=64))
    try:
        store.upsert(ProceduralMemoryRecord(
            repo="acme/app",
            pr_number=5,
            categories=["correctness"],
            file_patterns=["src/parser.py"],
            problem_pattern="空列表索引越界 IndexError",
            fix_strategy="增加 if not items: return [] 守卫",
            patch_summary="+ if not items: return []",
            qa_verdict="pass",
            ci_passed=True,
            human_approved=True,
            final_outcome="MERGED",
            memory_type="fix_pattern",
        ))
        res = search_procedural_memory(
            store,
            SearchProceduralMemoryArgs(query="IndexError 空列表", top_k=2),
            current_repo="acme/app",
        )
        assert res.ok
        parsed = json.loads(res.content)
        assert len(parsed) == 1
        assert parsed[0]["repo"] == "acme/app"
        assert res.meta["hits"] == 1
    finally:
        store.close()


def test_v2_models_monotonic_invariants():
    # PRBrief 不允许 LLM 将 critical/medium 降级到 low
    brief = PRBrief(
        problem_summary="修改鉴权逻辑",
        deterministic_level="critical",
        llm_suggested_level="low",
    )
    assert brief.final_level == "critical"

    # QACheckReport 有范围漂移时不允许维持 pass
    qa = QACheckReport(verdict="pass", is_aligned=True, has_scope_drift=True)
    assert qa.verdict == "revise"

    # ReviewSummary 继承等级只升不降
    summary = ReviewSummary(
        headline="更新 PR",
        inherited_level="medium",
        effective_level="low",
    )
    assert summary.effective_level == "medium"
    assert summary.level_escalated is False
