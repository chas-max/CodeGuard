"""Micro-SWE-Bench 评测 CLI。

用法：
    python -m eval.run_benchmark --suite 50 --out reports/
    python -m eval.run_benchmark --suite 11 --out reports/ --quick   # 单轮模板快速验证
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.micro_swe_bench import build_synthetic_suite, run_ablation_benchmark  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="CodeGuard Micro-SWE-Bench 评测")
    parser.add_argument("--suite", type=int, default=50, help="题目数量（默认 50）")
    parser.add_argument("--out", default="reports", help="报告输出目录")
    parser.add_argument("--seed", type=int, default=20260101)
    parser.add_argument("--workspace", default="", help="评测工作区（默认临时目录）")
    parser.add_argument("--keep", action="store_true", help="保留评测工作区（调试用）")
    args = parser.parse_args()

    if args.workspace:
        workspace = Path(args.workspace)
    elif args.keep:
        workspace = Path(tempfile.mkdtemp(prefix="codeguard_bench_"))
        print(f"[workspace] {workspace}（--keep 已保留）")
    else:
        workspace = Path(tempfile.mkdtemp(prefix="codeguard_bench_"))

    report = run_ablation_benchmark(
        workspace, Path(args.out), size=args.suite, seed=args.seed
    )

    print(json.dumps(
        {k: v for k, v in report.items() if k != "groups"},
        ensure_ascii=False, indent=2,
    ))
    for name, g in report["groups"].items():
        print(f"\n[{name}]")
        print(f"  Recall@3={g['localization_recall_at_3']:.1%}  "
              f"Pass@1={g['one_shot_pass_at_1']:.1%}  "
              f"Resolved={g['resolved_rate']:.1%}  "
              f"P2P回归={g['p2p_regression_rate']:.1%}")
    print(f"\n报告已落盘: {Path(args.out).resolve()}/benchmark_report.{{json,md}}")
    if not args.workspace and not args.keep:
        import shutil
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
