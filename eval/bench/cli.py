"""`python -m eval.bench` 命令面：run / score / report / fetch。

退出码约定：质量门槛只写进报告，不改变退出码；只有文件缺失、参数错误或
内部异常才返回非 0。`--live` 才允许真实 LLM / Docker。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .dedup import DedupResult
from .models import Observation, RunMeta, TaskSpec
from .paths import BENCH_ROOT
from .registry import RegistryError, load_registry
from .report import build_report, render_markdown, write_report
from .runner import (
    BenchRunner,
    RunError,
    aggregate_records,
    load_tasks,
    task_evidence_mix,
)
from .scoring import evaluate
from .thresholds import load_thresholds

PROFILES = ("external", "supplement", "full")
DEFAULT_PROFILE = "external"


def datasets_for_profile(registry, profile: str = DEFAULT_PROFILE) -> list[str]:
    """`external` 只跑主分数据集；`supplement` 只跑自建；`full` 两边都跑但分栏报告。"""
    if profile == "external":
        return [entry.name for entry in registry.entries if entry.is_primary]
    if profile == "supplement":
        return [
            entry.name for entry in registry.entries
            if entry.is_supplementary or entry.is_diagnostic
        ]
    return [
        entry.name for entry in registry.entries
        if entry.is_primary or entry.is_supplementary or entry.is_diagnostic
    ]


def _read_json(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _baseline(path: str) -> dict | None:
    if not path:
        return None
    data = _read_json(Path(path), None)
    if data is None:
        raise ValueError(f"找不到基线文件: {path}")
    if "summary" in data and "functions" in data:
        return {
            "functions": data.get("functions") or {},
            "composite": {
                "total": (data.get("summary") or {}).get("total"),
                "partial": (data.get("summary") or {}).get("partial"),
            },
        }
    return data


def _print_summary(payload: dict, quiet: bool) -> None:
    if not quiet:
        print(json.dumps(payload, ensure_ascii=False))


def cmd_run(args: argparse.Namespace) -> int:
    registry = load_registry(args.registry or None)
    thresholds = load_thresholds(args.thresholds or None)
    profile = str(args.profile or DEFAULT_PROFILE)
    if args.dataset:
        datasets = list(args.dataset)
        # 显式 --dataset 覆盖 profile；core 自动视为 supplementary。
        roles = {
            entry.role for entry in (registry.maybe_get(name) for name in datasets)
            if entry is not None
        }
        if roles == {"supplementary"}:
            profile = "supplement"
        elif roles == {"primary"}:
            profile = "external"
    else:
        datasets = datasets_for_profile(registry, profile)
    if not datasets:
        print("bench: 当前 profile 没有可用数据集，请检查 registry 的 role", file=sys.stderr)
        return 0
    fixtures_dir = BENCH_ROOT / "fixtures" if args.fixtures else None
    try:
        tasks = load_tasks(
            registry, datasets, limit=args.limit, fixtures_dir=fixtures_dir,
        )
    except RegistryError as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 0
    if not tasks:
        print(
            "bench: 没有可执行任务（外部数据集可能需要先物化快照）；仍输出空报告",
            file=sys.stderr,
        )

    provider = None
    if args.live and (args.patch_source == "system" or any(
        registry.get(name).adapter == "reviewbench" for name in datasets
    )):
        try:
            from codeguard.config import load_settings
            from codeguard.llm import make_llm

            settings = load_settings()
            provider = lambda role: make_llm(role, settings)  # noqa: E731
        except Exception as exc:  # noqa: BLE001 - live 配置缺失时降级为 skipped
            print(f"bench: LLM provider 不可用，外部任务将跳过: {exc}", file=sys.stderr)

    runner = BenchRunner(
        registry=registry, out_dir=Path(args.out), offline=not args.live,
        repeat=args.repeat, parallel=args.parallel,
        budget_cny=args.budget_cny, provider=provider,
        patch_source=args.patch_source,
        snapshot_root=args.snapshot_root or None,
        keep_work=args.keep_work,
        profile=profile,
    )
    result = runner.run(tasks, resume=args.resume)
    baseline = _baseline(args.baseline)
    evaluation = evaluate(result.records, registry, thresholds, baseline)
    out = Path(args.out)
    _write_json(out / "evaluation.json", evaluation.to_dict())
    if args.no_report:
        _print_summary({**result.to_dict(), "evaluation": evaluation.to_dict()}, args.quiet)
        return 0
    report = build_report(
        evaluation=evaluation, records=result.records, registry=registry,
        run_meta=result.run_meta, dedup=result.dedup, thresholds=thresholds,
        observations=result.observations, baseline=baseline,
        task_mix=result.evidence_mix,
    )
    write_report(out, report)
    _print_summary({
        **result.to_dict(),
        "profile": profile,
        "status": evaluation.status,
        "overall": evaluation.overall,
        "total": evaluation.composite["total"],
        "coverage": evaluation.composite["coverage"],
        "tracks": {
            name: {"status": item.status, "score": item.to_dict()["score"]}
            for name, item in evaluation.tracks.items()
        },
    }, args.quiet)
    return 0


def _load_run_dir(target: Path) -> tuple[list[TaskSpec], list[Observation], RunMeta, DedupResult]:
    tasks = [TaskSpec.from_dict(row) for row in _read_jsonl(target / "tasks.jsonl")]
    observations = [Observation.from_dict(row) for row in _read_jsonl(target / "observations.jsonl")]
    meta = _read_json(target / "run_meta.json", {}) or {}
    dedup_raw = _read_json(target / "dedup.json", {}) or {}
    dedup = DedupResult(
        kept=list(tasks),
        duplicates=[],
        index=dict(dedup_raw.get("index") or {}),
        overlap=dict(dedup_raw.get("overlap") or {}),
        duplicate_count=int(dedup_raw.get("duplicate_count") or 0),
    )
    run_meta = RunMeta(**{
        key: meta[key] for key in
        ("run_id", "started_at", "git_sha", "registry_sha256", "models",
         "repeat", "offline", "budget_cny", "notes", "profile", "patch_source")
        if key in meta
    })
    return tasks, observations, run_meta, dedup


def cmd_score(args: argparse.Namespace) -> int:
    target = Path(args.input)
    if not (target / "tasks.jsonl").is_file() or not (target / "observations.jsonl").is_file():
        print(f"bench: {target} 缺少 tasks.jsonl/observations.jsonl，请先运行 run", file=sys.stderr)
        return 1
    registry = load_registry(args.registry or None)
    thresholds = load_thresholds(args.thresholds or None)
    tasks, observations, run_meta, dedup = _load_run_dir(target)
    records = aggregate_records(registry, tasks, observations)
    baseline = _baseline(args.baseline)
    evaluation = evaluate(records, registry, thresholds, baseline)
    out = Path(args.out or target)
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "evaluation.json", evaluation.to_dict())
    _write_json(out / "scores.json", {
        "schema_version": 1,
        "records": [record.to_dict() for record in records],
    })
    report = build_report(
        evaluation=evaluation, records=records, registry=registry,
        run_meta=run_meta, dedup=dedup, thresholds=thresholds,
        observations=observations, baseline=baseline,
        task_mix=task_evidence_mix(registry, tasks),
    )
    write_report(out, report)
    _print_summary({
        "status": evaluation.status,
        "overall": evaluation.overall,
        "total": evaluation.composite["total"],
        "coverage": evaluation.composite["coverage"],
        "records": len(records),
        "tracks": {
            name: {"status": item.status, "score": item.to_dict()["score"]}
            for name, item in evaluation.tracks.items()
        },
    }, args.quiet)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    target = Path(args.input)
    report = _read_json(target / "report.json", None)
    if report is None:
        print(f"bench: {target} 缺少 report.json", file=sys.stderr)
        return 1
    (target / "report.md").write_text(render_markdown(report), encoding="utf-8")
    _print_summary({"written": str(target / "report.md")}, args.quiet)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.bench",
        description="CodeGuard 评测平台：数据集登记、两级功能评分与报告",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行任务、评分并生成报告")
    run.add_argument("--profile", choices=PROFILES, default=DEFAULT_PROFILE,
                     help="external=只跑外部主分数据集（默认）；supplement=只跑自建；full=两边都跑但分栏")
    run.add_argument("--dataset", action="append", default=[],
                     help="数据集名，可重复；显式指定时覆盖 --profile")
    run.add_argument("--limit", type=int, default=0, help="每个数据集最多取多少条")
    run.add_argument("--fixtures", action="store_true", help="使用内置小样本（不读物化数据）")
    run.add_argument("--out", default="reports/bench/latest")
    run.add_argument("--registry", default="")
    run.add_argument("--thresholds", default="")
    run.add_argument("--baseline", default="", help="上一版 report.json 或 evaluation.json")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--parallel", type=int, default=1)
    run.add_argument("--budget-cny", type=float, default=10.0)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--live", action="store_true", help="允许真实 LLM/Docker 执行外部基准")
    run.add_argument("--patch-source", choices=("gold", "system"), default="gold",
                     help="外部修复任务的补丁来源；gold 用于自证 harness")
    run.add_argument("--snapshot-root", default="", help="快照根目录，默认 eval/datasets/bench/snapshots")
    run.add_argument("--keep-work", action="store_true",
                     help="保留外部任务工作区，便于排查；默认成功后清理")
    run.add_argument("--no-report", action="store_true")
    run.add_argument("--quiet", action="store_true")
    run.set_defaults(func=cmd_run)

    score = sub.add_parser("score", help="从已有观测重算功能分与报告")
    score.add_argument("--input", required=True)
    score.add_argument("--out", default="")
    score.add_argument("--registry", default="")
    score.add_argument("--thresholds", default="")
    score.add_argument("--baseline", default="")
    score.add_argument("--quiet", action="store_true")
    score.set_defaults(func=cmd_score)

    report = sub.add_parser("report", help="从 report.json 重新渲染 Markdown")
    report.add_argument("--input", required=True)
    report.add_argument("--quiet", action="store_true")
    report.set_defaults(func=cmd_report)

    # 只用于 --help 展示；实际调用在 main 里直接转交 fetch.main。
    fetch = sub.add_parser("fetch", help="数据集下载/切片/快照/校验")
    fetch.set_defaults(func=lambda args: 0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args_list = list(argv if argv is not None else sys.argv[1:])
    # fetch 的参数面独立于 run/score/report，直接转交，避免 REMAINDER 吞掉 --flag。
    if args_list and args_list[0] == "fetch":
        from . import fetch

        try:
            return fetch.main(args_list[1:])
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"bench: {exc}", file=sys.stderr)
            return 1
    parser = build_parser()
    args = parser.parse_args(args_list)
    try:
        return args.func(args)
    except (RegistryError, ValueError, OSError, RunError) as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一报错，不打印栈
        print(f"bench: 未预期错误 {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


__all__ = ["main", "build_parser"]
