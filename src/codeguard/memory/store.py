"""本地向量程序化记忆库：基于 sqlite-vec + SQLite 元数据表（支持 numpy 降级）。"""
from __future__ import annotations

import json
import sqlite3
import struct
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ..models import ProceduralMemoryRecord
from .embedder import Embedder, LocalHashEmbedder


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def _unpack_vector(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProceduralMemoryStore:
    """持久化程序化记忆库，支持向量相似度检索与多维元数据过滤。"""

    def __init__(
        self,
        db_path: Path | str,
        *,
        embedder: Embedder | None = None,
        force_numpy_fallback: bool = False,
    ):
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder: Embedder = embedder or LocalHashEmbedder(dim=256)
        self.dim = int(self.embedder.dim)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._vec_enabled = False
        self._init_schema(force_numpy_fallback=force_numpy_fallback)

    def _init_schema(self, *, force_numpy_fallback: bool) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS procedural_memories (
                    memory_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL UNIQUE,
                    repo TEXT NOT NULL,
                    pr_number INTEGER NOT NULL DEFAULT 0,
                    importance_level TEXT NOT NULL DEFAULT 'medium',
                    reviewer_roles TEXT NOT NULL DEFAULT '[]',
                    categories TEXT NOT NULL DEFAULT '[]',
                    file_patterns TEXT NOT NULL DEFAULT '[]',
                    problem_pattern TEXT NOT NULL DEFAULT '',
                    fix_strategy TEXT NOT NULL DEFAULT '',
                    patch_summary TEXT NOT NULL DEFAULT '',
                    qa_verdict TEXT NOT NULL DEFAULT '',
                    ci_passed INTEGER NOT NULL DEFAULT 0,
                    human_approved INTEGER,
                    final_outcome TEXT NOT NULL DEFAULT '',
                    memory_type TEXT NOT NULL DEFAULT 'review_lesson',
                    content_text TEXT NOT NULL DEFAULT '',
                    embedding_model TEXT NOT NULL DEFAULT '',
                    embedding_dim INTEGER NOT NULL DEFAULT 256,
                    embedding_blob BLOB NOT NULL,
                    created_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pm_repo ON procedural_memories(repo)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pm_type_ci ON procedural_memories(memory_type, ci_passed)"
            )
            if not force_numpy_fallback:
                try:
                    import sqlite_vec

                    self._conn.enable_load_extension(True)
                    sqlite_vec.load(self._conn)
                    self._conn.execute(
                        f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS vec_procedural_memories USING vec0(
                            memory_id TEXT PRIMARY KEY,
                            embedding float[{self.dim}] distance_metric=cosine
                        )
                        """
                    )
                    self._vec_enabled = True
                except Exception:
                    self._vec_enabled = False
            self._conn.commit()

    @property
    def vec_enabled(self) -> bool:
        return self._vec_enabled

    def upsert(self, record: ProceduralMemoryRecord) -> str:
        """幂等写入程序化记忆及其向量表示；按 content_hash 去重更新。"""
        created_at = record.created_at or _now_iso()
        vec = self.embedder.embed(record.content_text)
        if len(vec) != self.dim:
            raise ValueError(f"向量维度不匹配: expected={self.dim}, got={len(vec)}")
        blob = _pack_vector(vec)
        human_int = None if record.human_approved is None else (1 if record.human_approved else 0)

        with self._lock:
            existing = self._conn.execute(
                "SELECT memory_id FROM procedural_memories WHERE content_hash = ?",
                (record.content_hash,),
            ).fetchone()
            memory_id = str(existing["memory_id"]) if existing else record.memory_id

            self._conn.execute(
                """
                INSERT INTO procedural_memories (
                    memory_id, content_hash, repo, pr_number, importance_level,
                    reviewer_roles, categories, file_patterns, problem_pattern,
                    fix_strategy, patch_summary, qa_verdict, ci_passed,
                    human_approved, final_outcome, memory_type, content_text,
                    embedding_model, embedding_dim, embedding_blob, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    repo = excluded.repo,
                    pr_number = excluded.pr_number,
                    importance_level = excluded.importance_level,
                    reviewer_roles = excluded.reviewer_roles,
                    categories = excluded.categories,
                    file_patterns = excluded.file_patterns,
                    problem_pattern = excluded.problem_pattern,
                    fix_strategy = excluded.fix_strategy,
                    patch_summary = excluded.patch_summary,
                    qa_verdict = excluded.qa_verdict,
                    ci_passed = excluded.ci_passed,
                    human_approved = excluded.human_approved,
                    final_outcome = excluded.final_outcome,
                    memory_type = excluded.memory_type,
                    content_text = excluded.content_text,
                    embedding_model = excluded.embedding_model,
                    embedding_dim = excluded.embedding_dim,
                    embedding_blob = excluded.embedding_blob
                """,
                (
                    memory_id,
                    record.content_hash,
                    record.repo,
                    int(record.pr_number),
                    record.importance_level,
                    json.dumps(record.reviewer_roles, ensure_ascii=False),
                    json.dumps(record.categories, ensure_ascii=False),
                    json.dumps(record.file_patterns, ensure_ascii=False),
                    record.problem_pattern,
                    record.fix_strategy,
                    record.patch_summary,
                    record.qa_verdict,
                    1 if record.ci_passed else 0,
                    human_int,
                    record.final_outcome,
                    record.memory_type,
                    record.content_text,
                    self.embedder.model_name,
                    self.dim,
                    blob,
                    created_at,
                ),
            )
            if self._vec_enabled:
                self._conn.execute(
                    "DELETE FROM vec_procedural_memories WHERE memory_id = ?",
                    (memory_id,),
                )
                self._conn.execute(
                    "INSERT INTO vec_procedural_memories(memory_id, embedding) VALUES (?, ?)",
                    (memory_id, blob),
                )
            self._conn.commit()
        return memory_id

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        human_raw = row["human_approved"]
        return {
            "memory_id": row["memory_id"],
            "content_hash": row["content_hash"],
            "repo": row["repo"],
            "pr_number": int(row["pr_number"]),
            "importance_level": row["importance_level"],
            "reviewer_roles": json.loads(row["reviewer_roles"] or "[]"),
            "categories": json.loads(row["categories"] or "[]"),
            "file_patterns": json.loads(row["file_patterns"] or "[]"),
            "problem_pattern": row["problem_pattern"],
            "fix_strategy": row["fix_strategy"],
            "patch_summary": row["patch_summary"],
            "qa_verdict": row["qa_verdict"],
            "ci_passed": bool(row["ci_passed"]),
            "human_approved": None if human_raw is None else bool(human_raw),
            "final_outcome": row["final_outcome"],
            "memory_type": row["memory_type"],
            "content_text": row["content_text"],
            "embedding_model": row["embedding_model"],
            "embedding_dim": int(row["embedding_dim"]),
            "created_at": row["created_at"],
        }

    def get(self, memory_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM procedural_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS cnt FROM procedural_memories").fetchone()
            return int(row["cnt"]) if row else 0

    def _matches_filters(
        self,
        item: dict[str, Any],
        *,
        repo: str,
        category: str,
        memory_types: tuple[str, ...] | None,
        verified_only: bool,
    ) -> bool:
        if repo and item["repo"] != repo:
            return False
        if category:
            cats = {str(c).lower() for c in item["categories"]}
            if category.lower() not in cats:
                return False
        if memory_types and item["memory_type"] not in memory_types:
            return False
        if verified_only:
            if not item["ci_passed"]:
                return False
            if item["human_approved"] is False:
                return False
        return True

    def search(
        self,
        query: str,
        *,
        repo: str = "",
        current_repo: str = "",
        category: str = "",
        memory_types: tuple[str, ...] | None = None,
        verified_only: bool = True,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """检索相似程序化记忆，结合向量相似度与结构化元数据重排。"""
        text = (query or "").strip()
        if not text:
            return []
        k = max(1, min(20, int(top_k)))
        q_vec = self.embedder.embed(text)
        q_blob = _pack_vector(q_vec)
        q_np = np.asarray(q_vec, dtype=np.float32)
        q_norm = float(np.linalg.norm(q_np)) or 1.0

        candidates: list[tuple[dict[str, Any], float]] = []
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS cnt FROM procedural_memories").fetchone()
            total_count = int(total["cnt"]) if total else 0
            if total_count == 0:
                return []

            if self._vec_enabled:
                knn_limit = min(total_count, max(k * 8, 32))
                vec_rows = self._conn.execute(
                    "SELECT memory_id, distance FROM vec_procedural_memories "
                    "WHERE embedding MATCH ? AND k = ?",
                    (q_blob, knn_limit),
                ).fetchall()
                id_list = [str(r["memory_id"]) for r in vec_rows]
                dist_map = {str(r["memory_id"]): float(r["distance"]) for r in vec_rows}
                if id_list:
                    placeholders = ",".join("?" for _ in id_list)
                    meta_rows = self._conn.execute(
                        f"SELECT * FROM procedural_memories WHERE memory_id IN ({placeholders})",
                        id_list,
                    ).fetchall()
                    for row in meta_rows:
                        item = self._row_to_dict(row)
                        similarity = max(0.0, 1.0 - dist_map.get(item["memory_id"], 1.0))
                        candidates.append((item, similarity))
            else:
                rows = self._conn.execute("SELECT * FROM procedural_memories").fetchall()
                for row in rows:
                    dim = int(row["embedding_dim"])
                    if dim != self.dim:
                        continue
                    v_np = _unpack_vector(row["embedding_blob"], dim)
                    denom = (float(np.linalg.norm(v_np)) or 1.0) * q_norm
                    similarity = max(0.0, float(np.dot(q_np, v_np) / denom))
                    candidates.append((self._row_to_dict(row), similarity))

        scored: list[dict[str, Any]] = []
        for item, sim in candidates:
            if not self._matches_filters(
                item,
                repo=repo,
                category=category,
                memory_types=memory_types,
                verified_only=verified_only,
            ):
                continue
            bonus = 0.0
            if current_repo and item["repo"] == current_repo:
                bonus += 0.08
            if item["ci_passed"]:
                bonus += 0.05
            if item["human_approved"] is True:
                bonus += 0.05
            if item["memory_type"] == "anti_pattern":
                bonus -= 0.02
            scored.append({
                "memory_id": item["memory_id"],
                "score": round(sim + bonus, 4),
                "vector_similarity": round(sim, 4),
                "memory_type": item["memory_type"],
                "repo": item["repo"],
                "pr_number": item["pr_number"],
                "importance_level": item["importance_level"],
                "categories": item["categories"],
                "file_patterns": item["file_patterns"],
                "problem_pattern": item["problem_pattern"],
                "fix_strategy": item["fix_strategy"],
                "patch_summary": item["patch_summary"],
                "ci_passed": item["ci_passed"],
                "human_approved": item["human_approved"],
                "final_outcome": item["final_outcome"],
                "created_at": item["created_at"],
            })

        scored.sort(key=lambda row: row["score"], reverse=True)
        return scored[:k]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["ProceduralMemoryStore"]
