"""执行层：统一调度核心集与外部基准，产出 Observation 与 ScoreRecord。

核心集复用 `eval.run_matrix.Runner.run_case`，不重写判定；外部基准在快照未物化、
离线模式或缺少 live 配置时一律 `skipped`，绝不伪造结果。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .adapters import get_adapter
from .adapters.base import load_jsonl
from .dedup import DedupResult, dedup_tasks
from .models import Observation, RunMeta, ScoreRecord, TaskSpec
from .paths import BENCH_ROOT, PROJECT_ROOT
from .registry import Registry

_SRC = PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class RunError(RuntimeError):
    pass


def git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def load_tasks(registry: Registry, datasets: list[str] | None = None, *,
               limit: int = 0, fixtures_dir: Path | str | None = None) -> list[TaskSpec]:
    names = list(datasets or ["core"])
    tasks: list[TaskSpec] = []
    for name in names:
        entry = registry.get(name)
        adapter = get_adapter(entry.adapter)
        instances: list[dict]
        if fixtures_dir is not None and entry.fixtures:
            fixture = BENCH_ROOT / entry.fixtures
            if entry.adapter == "core":
                instances = adapter.load_instances(entry, root=fixtures_dir, limit=limit)
            else:
                raw = load_jsonl(fixture)
                normalized = [adapter.normalize_instance(item) for item in raw]
                instances = normalized[:limit] if limit and limit > 0 else normalized
        else:
            instances = adapter.load_instances(entry, limit=limit)
        tasks.extend(adapter.to_tasks(entry, instances))
    return tasks


def aggregate_records(registry: Registry, tasks: list[TaskSpec],
                      observations: list[Observation]) -> list[ScoreRecord]:
    by_dataset: dict[str, list[TaskSpec]] = {}
    for task in tasks:
        by_dataset.setdefault(task.dataset, []).append(task)
    obs_by_dataset: dict[str, list[Observation]] = {}
    for obs in observations:
        obs_by_dataset.setdefault(obs.dataset, []).append(obs)

    records: list[ScoreRecord] = []
    for entry in registry.entries:
        dataset_tasks = by_dataset.get(entry.name)
        if not dataset_tasks:
            continue
        adapter = get_adapter(entry.adapter)
        try:
            records.extend(adapter.aggregate(
                entry, dataset_tasks, obs_by_dataset.get(entry.name, []),
            ))
        except Exception as exc:  # noqa: BLE001 - 数据集级失败要带上下文
            raise RunError(f"{entry.name}: 聚合失败 {type(exc).__name__}: {exc}") from exc
    return records


def task_evidence_mix(registry: Registry, tasks: list[TaskSpec]) -> dict:
    """按数据集性质拆分外部 / 自建任务数，供报告说明“证据构成”。"""
    buckets = {"external": 0, "self_built": 0}
    for task in tasks:
        entry = registry.maybe_get(task.dataset)
        key = "external" if entry is not None and entry.kind == "external" else "self_built"
        buckets[key] += 1
    total = sum(buckets.values())
    return {
        "tasks": dict(buckets),
        "share": {
            key: round(value / total, 6) if total else 0.0
            for key, value in buckets.items()
        },
        "total": total,
    }


@dataclass
class RunResult:
    tasks: list[TaskSpec]
    observations: list[Observation]
    records: list[ScoreRecord]
    dedup: DedupResult
    run_meta: RunMeta
    out_dir: Path
    evidence_mix: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run": self.run_meta.to_dict(),
            "tasks": len(self.tasks),
            "kept": len(self.dedup.kept),
            "duplicates": len(self.dedup.duplicates),
            "observations": len(self.observations),
            "ok": sum(1 for obs in self.observations if obs.status == "ok"),
            "skipped": sum(1 for obs in self.observations if obs.status == "skipped"),
            "error": sum(1 for obs in self.observations if obs.status == "error"),
            "records": len(self.records),
            "cost_cny": round(sum(obs.cost_cny for obs in self.observations), 4),
            "evidence_mix": dict(self.evidence_mix),
        }


class BenchRunner:
    def __init__(
        self,
        *,
        registry: Registry,
        out_dir: Path | str,
        offline: bool = True,
        repeat: int = 1,
        parallel: int = 1,
        budget_cny: float = 10.0,
        provider=None,
        sandbox=None,
        patch_source: str = "gold",
        snapshot_root: Path | str | None = None,
        keep_work: bool = False,
        profile: str = "",
    ):
        self.registry = registry
        self.profile = str(profile or "")
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.repeat = max(1, int(repeat))
        self.parallel = max(1, int(parallel))
        self.budget_cny = float(budget_cny)
        self.provider = provider
        self.sandbox = sandbox
        self.patch_source = str(patch_source or "gold")
        self.snapshot_root = Path(snapshot_root) if snapshot_root else None
        self.keep_work = bool(keep_work)
        self.spent = 0.0
        self._lock = threading.Lock()
        self._matrix = None
        self._reviewer_cache: list[dict] | None = None

    # ── 调度 ──

    def run(self, tasks: list[TaskSpec], *, resume: bool = False) -> RunResult:
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        dedup = dedup_tasks(tasks)
        observations: list[Observation] = []
        done: set[tuple[str, int]] = set()
        if resume and (self.out_dir / "observations.jsonl").is_file():
            for row in load_jsonl(self.out_dir / "observations.jsonl"):
                try:
                    existing = Observation.from_dict(row)
                except (TypeError, ValueError):
                    continue
                observations.append(existing)
                done.add((existing.task_id, int(existing.payload.get("_repeat", 0))))

        jobs = [
            (task, repeat_index)
            for task in dedup.kept for repeat_index in range(self.repeat)
            if (task.task_id, repeat_index) not in done
        ]
        if self.parallel == 1:
            for task, repeat_index in jobs:
                if self.spent >= self.budget_cny:
                    observations.append(self._budget_observation(task, repeat_index))
                    continue
                observation = self._run_task(task, repeat_index)
                self.spent += observation.cost_cny
                observations.append(observation)
        else:
            with ThreadPoolExecutor(max_workers=self.parallel) as pool:
                futures = {}
                for task, repeat_index in jobs:
                    if self.spent >= self.budget_cny:
                        observations.append(self._budget_observation(task, repeat_index))
                        continue
                    futures[pool.submit(self._run_task, task, repeat_index)] = (task, repeat_index)
                for future in as_completed(futures):
                    observation = future.result()
                    with self._lock:
                        self.spent += observation.cost_cny
                    observations.append(observation)

        records = aggregate_records(self.registry, dedup.kept, observations)
        run_meta = RunMeta(
            run_id=self.out_dir.name,
            started_at=started_at,
            git_sha=git_sha(),
            registry_sha256=self.registry.sha256,
            models={"provider": type(self.provider).__name__} if self.provider else {},
            repeat=self.repeat,
            offline=self.offline,
            budget_cny=self.budget_cny,
            notes="datasets=" + ",".join(sorted({task.dataset for task in dedup.kept})),
            profile=self.profile,
            patch_source=self.patch_source,
        )
        result = RunResult(
            tasks=list(dedup.kept), observations=observations, records=records,
            dedup=dedup, run_meta=run_meta, out_dir=self.out_dir,
            evidence_mix=task_evidence_mix(self.registry, list(dedup.kept)),
        )
        self._persist(result)
        return result

    # ── 单任务 ──

    def _run_task(self, task: TaskSpec, repeat_index: int) -> Observation:
        started = time.monotonic()
        try:
            if task.source_layer == "probe":
                observation = self._run_probe(task)
            elif task.source_layer == "reviewer":
                observation = self._run_reviewer(task)
            elif self.registry.get(task.dataset).kind == "external":
                observation = self._run_external(task)
            else:
                observation = self._run_core(task, repeat_index)
        except Exception as exc:  # noqa: BLE001 - 单任务失败不拖垮整批
            observation = Observation(
                task_id=task.task_id, dataset=task.dataset, status="error",
                payload={"error_type": type(exc).__name__},
                capabilities=task.capabilities, sample_uid=task.sample_uid,
                error=f"{type(exc).__name__}: {exc}",
            )
        wall_ms = int((time.monotonic() - started) * 1000)
        payload = dict(observation.payload)
        payload["_repeat"] = repeat_index
        return replace(
            observation, payload=payload,
            wall_ms=observation.wall_ms or wall_ms,
            capabilities=task.capabilities, sample_uid=task.sample_uid,
        )

    def _run_core(self, task: TaskSpec, repeat_index: int) -> Observation:
        from eval.loader import validate_case

        raw = task.input.get("case") or {}
        case = validate_case(raw)
        result = self._matrix_runner().run_case(case, repeat_index)
        if result.skipped:
            status = "skipped"
        elif result.error:
            status = "error"
        else:
            status = "ok"
        return Observation(
            task_id=task.task_id, dataset=task.dataset, status=status,
            payload=result.to_dict(), capabilities=task.capabilities,
            cost_cny=result.cost_cny, wall_ms=result.wall_ms,
            sample_uid=task.sample_uid,
            skip_reason=result.skip_reason, error=result.error,
        )

    def _run_probe(self, task: TaskSpec) -> Observation:
        from codeguard.engine.diff import patch_paths

        diff = str(task.input.get("diff") or "")
        expected = [str(item) for item in (task.expected.get("paths") or [])]
        try:
            actual = patch_paths(diff)
            error = ""
        except Exception as exc:  # noqa: BLE001 - 非法 diff 是探针结论，不是崩溃
            actual = []
            error = f"{type(exc).__name__}: {exc}"
        passed = sorted(set(actual)) == sorted(set(expected))
        return Observation(
            task_id=task.task_id, dataset=task.dataset,
            status="ok" if not error else "error",
            payload={
                "probe": str(task.input.get("probe") or ""),
                "expected": expected, "actual": actual, "passed": passed,
            },
            capabilities=task.capabilities, sample_uid=task.sample_uid,
            error=error,
        )

    def _run_reviewer(self, task: TaskSpec) -> Observation:
        reviewer = task.input.get("reviewer") or {}
        case_name = str(reviewer.get("case_name") or task.task_id.rsplit(".", 1)[-1])
        rows = self._reviewer_rows()
        row = next((item for item in rows if str(item.get("case")) == case_name), None)
        if row is None:
            return Observation(
                task_id=task.task_id, dataset=task.dataset, status="error",
                payload={}, capabilities=task.capabilities, sample_uid=task.sample_uid,
                error=f"reviewer cassette 缺少 case: {case_name}",
            )
        return Observation(
            task_id=task.task_id, dataset=task.dataset, status="ok",
            payload={
                "expected": list(row.get("expected") or []),
                "actual": list(row.get("actual") or []),
                "passed": bool(row.get("pass")),
                "verdict": str(row.get("verdict") or ""),
            },
            capabilities=task.capabilities, sample_uid=task.sample_uid,
        )

    def _run_external(self, task: TaskSpec) -> Observation:
        entry = self.registry.get(task.dataset)
        work_root = self.out_dir / "external_work"

        if task.source_layer == "security":
            from .harness.security_task import run_security_task

            status, payload = run_security_task(task, work_root=work_root)
        elif task.source_layer == "fix":
            if self.offline:
                return self._skip_external(
                    task, "离线模式不执行外部基准；使用 --live 并先物化快照",
                )
            if self.patch_source == "system" and self.provider is None:
                return self._skip_external(
                    task, "patch_source=system 需要 LLM provider（--live 且配置 key）",
                )
            sandbox = self.sandbox
            if sandbox is None:
                try:
                    from codeguard.sandbox.docker_backend import DockerSandboxBackend

                    sandbox = DockerSandboxBackend()
                except Exception as exc:  # noqa: BLE001 - 环境问题应显式报错
                    return Observation(
                        task_id=task.task_id, dataset=task.dataset, status="error",
                        payload={"stage": "sandbox"}, capabilities=task.capabilities,
                        sample_uid=task.sample_uid,
                        error=f"Docker 不可用: {type(exc).__name__}: {exc}",
                    )
            from .harness.fix_task import run_fix_task

            snapshot_root = self.snapshot_root or (BENCH_ROOT / entry.snapshot_root)
            status, payload = run_fix_task(
                task, snapshot_root=snapshot_root, sandbox=sandbox,
                work_root=work_root, patch_source=self.patch_source,
                provider=self.provider, keep_work=self.keep_work,
            )
        elif task.source_layer == "review":
            if self.offline or self.provider is None:
                return self._skip_external(
                    task, "review 任务需要 --live 与 LLM provider",
                )
            from .harness.review_task import run_review_task

            status, payload = run_review_task(
                task, provider=self.provider, work_root=work_root / "review",
            )
        else:
            return self._skip_external(task, f"未知外部任务类型: {task.source_layer!r}")

        return Observation(
            task_id=task.task_id, dataset=task.dataset, status=status,
            payload=payload, capabilities=task.capabilities,
            cost_cny=float(payload.get("cost_cny", 0.0) or 0.0),
            sample_uid=task.sample_uid,
            skip_reason=str(payload.get("error") or "") if status == "skipped" else "",
            error=str(payload.get("error") or "") if status == "error" else "",
        )

    def _skip_external(self, task: TaskSpec, reason: str) -> Observation:
        return Observation(
            task_id=task.task_id, dataset=task.dataset, status="skipped",
            payload={}, capabilities=task.capabilities, sample_uid=task.sample_uid,
            skip_reason=reason,
        )

    def _budget_observation(self, task: TaskSpec, repeat_index: int) -> Observation:
        return Observation(
            task_id=task.task_id, dataset=task.dataset, status="skipped",
            payload={"_repeat": repeat_index, "budget_exceeded": True},
            capabilities=task.capabilities, sample_uid=task.sample_uid,
            skip_reason=f"预算耗尽（{self.budget_cny} CNY）",
        )

    # ── 依赖缓存 ──

    def _matrix_runner(self):
        if self._matrix is None:
            from eval.run_matrix import Runner

            self._matrix = Runner(
                out_dir=self.out_dir / "core_runner",
                offline=self.offline, provider=self.provider,
            )
        return self._matrix

    def _reviewer_rows(self) -> list[dict]:
        with self._lock:
            if self._reviewer_cache is None:
                from eval.run_reviewer_eval import run as run_reviewer

                report = run_reviewer()
                self._reviewer_cache = list(report.get("rows") or [])
            return list(self._reviewer_cache)

    # ── 落盘 ──

    def _persist(self, result: RunResult) -> None:
        self._write_jsonl(result.out_dir / "tasks.jsonl",
                          [task.to_dict() for task in result.tasks])
        self._write_jsonl(result.out_dir / "observations.jsonl",
                          [obs.to_dict() for obs in result.observations])
        self._write_json(result.out_dir / "scores.json", {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "records": [record.to_dict() for record in result.records],
        })
        self._write_json(result.out_dir / "dedup.json", result.dedup.to_dict())
        self._write_json(result.out_dir / "run_meta.json", result.run_meta.to_dict())

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        BenchRunner._atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict]) -> None:
        text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        BenchRunner._atomic_write(path, text)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)


__all__ = [
    "RunError", "RunResult", "BenchRunner", "load_tasks", "aggregate_records",
    "git_sha", "task_evidence_mix",
]
