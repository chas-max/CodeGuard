"""外部数据集切片构建器：只固化"切片 + 清单"，不在跑批时联网。

支持两类来源：
- `swebench_lite`：SWE-bench Lite（MIT），用于验证"合法修复不会被误拦"；
- `swe_care`：SWE-CARE / CodeFuse-CR-Bench（HF `inclusionAI/SWE-CARE`），用于语义召回。

用法：
    # 从本地导出文件切片（推荐，完全离线）
    python -m eval.fetch_external --dataset swebench_lite --source path/to/test.jsonl --limit 15
    # 校验已固化内容与 manifest 哈希
    python -m eval.fetch_external --offline
    # 打印快照物化命令（只打印，不执行）
    python -m eval.fetch_external --plan swebench_lite

约束：
- 不下载完整仓库、不执行 clone；仓库快照需人工按 `--plan` 输出固化；
- 许可证不明确的数据集（Qodo / Greptile / CR-Bench）默认拒绝写入，必须显式 `--allow-unlicensed`
  且只在本地使用；
- 外部切片不进入核心评分卡分母，单独统计。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .loader import CaseSpec, write_suite

ROOT = Path(__file__).resolve().parent / "datasets" / "external"
MANIFEST = ROOT / "manifest.json"

DATASET_META = {
    "swebench_lite": {
        "source": "princeton-nlp/SWE-bench_Lite",
        "license": "MIT",
        "purpose": "合法修复不应被误拦；validate 通过率",
        "snapshot": "git clone <repo> 后 checkout base_commit",
    },
    "swe_care": {
        "source": "inclusionAI/SWE-CARE (codefuse-ai/SWE-CARE)",
        "license": "Apache-2.0（以仓库 LICENSE 为准）",
        "purpose": "reviewer 语义召回与噪声比",
        "snapshot": "按 commit_sha checkout 仓库副本",
    },
}
UNLICENSED = {"qodo", "greptile", "cr_bench"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _read_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("instances") or data.get("rows") or []
    if not isinstance(data, list):
        raise SystemExit(f"无法识别的数据格式: {path}")
    return [item for item in data if isinstance(item, dict)]


def normalize_swebench(raw: dict) -> dict:
    instance_id = str(raw.get("instance_id") or raw.get("id") or "").strip()
    if not instance_id:
        raise ValueError("SWE-bench 记录缺少 instance_id")
    return {
        "id": instance_id,
        "repo": str(raw.get("repo") or ""),
        "base_commit": str(raw.get("base_commit") or ""),
        "patch": str(raw.get("patch") or ""),
        "test_patch": str(raw.get("test_patch") or ""),
        "problem_statement": str(raw.get("problem_statement") or "")[:2000],
        "fail_to_pass": raw.get("FAIL_TO_PASS") or [],
        "pass_to_pass": raw.get("PASS_TO_PASS") or [],
        "expect_block": False,
    }


def normalize_swe_care(raw: dict) -> dict:
    instance_id = str(raw.get("instance_id") or raw.get("id") or "").strip()
    if not instance_id:
        raise ValueError("SWE-CARE 记录缺少 instance_id")
    return {
        "id": instance_id,
        "repo": str(raw.get("repo") or ""),
        "pr_number": raw.get("pull_number") or raw.get("pr_number") or 0,
        "commit_sha": str(raw.get("commit_sha") or raw.get("base_commit") or ""),
        "patch": str(raw.get("patch") or raw.get("diff") or ""),
        "problem_statement": str(raw.get("problem_statement") or raw.get("title") or "")[:2000],
        "reference_comments": raw.get("reviews") or raw.get("reference_comments") or [],
        "expect_block": True,
    }


NORMALIZERS = {"swebench_lite": normalize_swebench, "swe_care": normalize_swe_care}


def build_slice(dataset: str, source: Path, *, limit: int = 20,
                allow_unlicensed: bool = False) -> dict:
    if dataset in UNLICENSED and not allow_unlicensed:
        raise SystemExit(
            f"{dataset} 未声明许可证，默认不写入仓库；如仅本地使用请加 --allow-unlicensed"
        )
    if dataset not in NORMALIZERS:
        raise SystemExit(f"未知数据集: {dataset}（可选: {sorted(NORMALIZERS)}）")
    meta = DATASET_META[dataset]
    rows = [NORMALIZERS[dataset](item) for item in _read_rows(source)]
    rows = rows[:limit]
    out_dir = ROOT / dataset
    out_dir.mkdir(parents=True, exist_ok=True)
    instances = out_dir / "instances.jsonl"
    instances.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    entry = {
        "name": dataset,
        "source": meta["source"],
        "license": meta["license"],
        "purpose": meta["purpose"],
        "snapshot": meta["snapshot"],
        "instances_file": instances.relative_to(ROOT).as_posix(),
        "count": len(rows),
        "sha256": _sha256(instances),
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_ready": False,
    }
    _upsert_manifest(entry)
    return entry


def _load_manifest() -> dict:
    if not MANIFEST.is_file():
        return {"schema_version": 1, "datasets": [], "notes": []}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _upsert_manifest(entry: dict) -> Path:
    manifest = _load_manifest()
    datasets = [item for item in manifest.get("datasets", []) if item.get("name") != entry["name"]]
    datasets.append(entry)
    manifest["datasets"] = sorted(datasets, key=lambda item: item["name"])
    manifest.setdefault("schema_version", 1)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return MANIFEST


def verify_offline(manifest_path: Path = MANIFEST) -> list[str]:
    """校验已固化切片的存在性与哈希；返回问题列表（空表示通过）。"""
    if not manifest_path.is_file():
        return [f"manifest 不存在: {manifest_path}"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for entry in manifest.get("datasets", []):
        path = ROOT / entry.get("instances_file", "")
        if not path.is_file():
            problems.append(f"{entry['name']}: 缺少切片文件 {path}")
            continue
        actual = _sha256(path)
        if entry.get("sha256") and actual != entry["sha256"]:
            problems.append(f"{entry['name']}: 哈希不匹配（manifest={entry['sha256'][:8]} actual={actual[:8]}）")
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if entry.get("count") is not None and len(lines) != int(entry["count"]):
            problems.append(f"{entry['name']}: 行数 {len(lines)} != manifest {entry['count']}")
    return problems


def external_cases(manifest_path: Path = MANIFEST) -> list[CaseSpec]:
    """把已固化切片转成 `external` 层用例；未固化仓库快照时执行层会跳过。"""
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases: list[CaseSpec] = []
    for entry in manifest.get("datasets", []):
        path = ROOT / entry.get("instances_file", "")
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            cases.append(CaseSpec(
                id=f"external.{entry['name']}.{row['id']}".lower().replace("/", "-"),
                layer="external",
                source="benchmark",
                dataset=entry["name"],
                expect_block=bool(row.get("expect_block", False)),
                tags=("external", entry["name"]),
                notes=str(row.get("problem_statement", ""))[:300],
                probe="external",
                probe_args={
                    "instance": row,
                    "snapshot_ready": bool(entry.get("snapshot_ready")),
                    "repo_dir": row.get("repo_dir", ""),
                },
            ))
    return cases


def plan(dataset: str) -> str:
    entry = DATASET_META.get(dataset)
    if entry is None:
        raise SystemExit(f"未知数据集: {dataset}")
    lines = [f"# {dataset}", f"# source: {entry['source']}  license: {entry['license']}", ""]
    path = ROOT / dataset / "instances.jsonl"
    if not path.is_file():
        return "\n".join(lines + ["# 先运行 build 生成切片"])
    for raw in path.read_text(encoding="utf-8").splitlines()[:10]:
        if not raw.strip():
            continue
        row = json.loads(raw)
        ref = row.get("base_commit") or row.get("commit_sha") or "<sha>"
        repo = row.get("repo") or "<owner/name>"
        lines.append(f"git clone https://github.com/{repo}.git snapshots/{repo.replace('/', '__')}")
        lines.append(
            f"git -C snapshots/{repo.replace('/', '__')} checkout {ref}  # {row['id']}"
        )
    lines.append("")
    lines.append("# 快照固化后，在 manifest 对应条目把 snapshot_ready 改为 true")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.fetch_external")
    parser.add_argument("--dataset", default="")
    parser.add_argument("--source", default="", help="本地导出的 JSON/JSONL")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--allow-unlicensed", action="store_true")
    parser.add_argument("--offline", action="store_true", help="只校验已固化切片")
    parser.add_argument("--plan", default="", help="打印快照物化命令")
    parser.add_argument("--out", default="", help="导出 external 用例 JSONL 的路径")
    args = parser.parse_args(argv)

    if args.offline:
        problems = verify_offline()
        print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False))
        return 0 if not problems else 1
    if args.plan:
        print(plan(args.plan))
        return 0
    if not args.dataset or not args.source:
        parser.error("需要 --dataset 与 --source，或使用 --offline/--plan")
    entry = build_slice(
        args.dataset, Path(args.source), limit=args.limit,
        allow_unlicensed=args.allow_unlicensed,
    )
    if args.out:
        write_suite(external_cases(), args.out)
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
