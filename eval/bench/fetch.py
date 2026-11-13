"""数据集物化：下载/切片、git 快照、哈希校验与状态。

约束：
- 数据本体不提交仓库，默认写到 `eval/datasets/bench/<dataset>/`；
- 许可证不明确的数据集默认拒绝，必须显式 `--allow-unlicensed`；
- 跑批本身不联网，联网只发生在 `fetch`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .adapters import get_adapter
from .adapters.base import load_jsonl
from .dedup import dedup_tasks
from .models import DatasetEntry, license_is_clear
from .paths import BENCH_ROOT
from .registry import Registry, load_registry


class FetchError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def resolve_source(source: str, *, revision: str = "main") -> str:
    """把 `hf://org/name[@rev]/path` 解析成 HuggingFace 直链；其它原样返回。"""
    text = str(source or "").strip()
    if not text.startswith("hf://"):
        return text
    body = text[len("hf://"):]
    if "/" not in body:
        raise FetchError(f"非法 HF 源: {source}")
    org, remainder = body.split("/", 1)
    name, _, path = remainder.partition("/")
    if not name or not path:
        raise FetchError(f"HF 源必须形如 hf://org/name/path/to/file.jsonl: {source}")
    rev = revision
    if "@" in name:
        name, rev = name.split("@", 1)
    return f"https://huggingface.co/datasets/{org}/{name}/resolve/{rev}/{path}"


def _download_http(url: str, dest: Path) -> tuple[Path, str]:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha256()
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
                    digest.update(chunk)
        tmp.replace(dest)
    except Exception as exc:  # noqa: BLE001 - 下载失败要带 URL 上下文
        tmp.unlink(missing_ok=True)
        raise FetchError(f"下载失败 {url}: {type(exc).__name__}: {exc}") from exc
    return dest, digest.hexdigest()


def _download(source: str, dest: Path, *, revision: str = "main") -> tuple[Path, str]:
    resolved = resolve_source(source, revision=revision)
    if resolved.startswith(("http://", "https://")):
        return _download_http(resolved, dest)
    local = Path(resolved[7:] if resolved.startswith("file://") else resolved)
    if not local.is_file():
        raise FetchError(f"本地源不存在: {local}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(local, dest)
    return dest, _sha256_file(dest)


def _read_rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return load_jsonl(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FetchError(f"{path} 不是合法 JSON: {exc}") from exc
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("instances") or payload.get("rows") or payload.get("data") or []
    else:
        rows = []
    if not isinstance(rows, list):
        raise FetchError(f"{path} 没有可识别的记录数组")
    return [row for row in rows if isinstance(row, dict)]


def _manifest_path(root: Path, entry: DatasetEntry) -> Path:
    if entry.manifest_file:
        return root / entry.manifest_file
    return root / entry.name / "manifest.json"


def _read_manifest(root: Path, entry: DatasetEntry) -> dict | None:
    path = _manifest_path(root, entry)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def slice_dataset(
    registry: Registry,
    name: str,
    source: str,
    *,
    limit: int = 0,
    allow_unlicensed: bool = False,
    out_root: Path | str | None = None,
    revision: str = "main",
) -> dict:
    entry = registry.get(name)
    if entry.kind == "core":
        raise FetchError(f"{name}: core 数据集是内置用例，不需要物化")
    if not license_is_clear(entry.license) and not allow_unlicensed:
        raise FetchError(
            f"{name}: 许可证不明确（{entry.license or '未声明'}），默认拒绝写入；"
            "如仅本地使用请加 --allow-unlicensed"
        )
    root = Path(out_root or BENCH_ROOT)
    dataset_dir = root / entry.name
    raw_dir = dataset_dir / "raw"
    resolved = resolve_source(source, revision=revision)
    filename = Path(urlparse(resolved).path).name or "source.jsonl"
    downloaded, source_sha = _download(source, raw_dir / filename, revision=revision)

    rows = _read_rows(downloaded)
    adapter = get_adapter(entry.adapter)
    normalized: list[dict] = []
    for index, row in enumerate(rows, 1):
        try:
            normalized.append(adapter.normalize_instance(row))
        except Exception as exc:  # noqa: BLE001 - 带行号，便于定位数据问题
            raise FetchError(f"{name}: 第 {index} 条归一化失败: {exc}") from exc
    if limit and limit > 0:
        normalized = normalized[:limit]
    if not normalized:
        raise FetchError(f"{name}: 切片为空（来源 {resolved}）")

    instances_path = dataset_dir / "instances.jsonl"
    _write_jsonl(instances_path, normalized)
    previous = _read_manifest(root, entry) or {}
    instances_sha = _sha256_file(instances_path)
    same_instances = bool(previous.get("sha256")) and previous.get("sha256") == instances_sha
    manifest = {
        "schema_version": 1,
        "dataset": name,
        "adapter": entry.adapter,
        "license": entry.license,
        "license_override": bool(
            allow_unlicensed and not license_is_clear(entry.license)
        ),
        "source": resolved,
        "source_sha256": source_sha,
        "retrieved_at": _now(),
        "count": len(normalized),
        "sha256": instances_sha,
        "instances_file": instances_path.relative_to(root).as_posix(),
        "snapshot_ready": bool(previous.get("snapshot_ready")) and same_instances,
        "snapshots": list(previous.get("snapshots") or []) if same_instances else [],
        "registry_sha256": registry.sha256,
    }
    _write_json(_manifest_path(root, entry), manifest)
    return manifest


def _git(args: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def _git_head(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    proc = _git(["-C", str(path), "rev-parse", "HEAD"], timeout=60)
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def snapshot_one(repo_url: str, commit: str, target: Path | str) -> dict:
    """把 repo@commit checkout 到 target；已存在且 HEAD 一致时直接复用。"""
    target_path = Path(target)
    record = {"repo": repo_url, "commit": commit, "path": target_path.as_posix()}
    if target_path.exists():
        if _git_head(target_path) == commit:
            return {**record, "ok": True, "reused": True}
        fetched = _git(["-C", str(target_path), "fetch", "--all", "--quiet"])
        checked = _git(["-C", str(target_path), "checkout", "--quiet", commit], timeout=300)
        if fetched.returncode == 0 and checked.returncode == 0:
            return {**record, "ok": True, "reused": True}
        return {
            **record, "ok": False, "reused": True,
            "error": (checked.stderr or fetched.stderr or "checkout 失败").strip()[:500],
        }

    target_path.parent.mkdir(parents=True, exist_ok=True)
    cloned = _git(["clone", "--quiet", "--no-checkout", repo_url, str(target_path)])
    if cloned.returncode != 0:
        shutil.rmtree(target_path, ignore_errors=True)
        return {
            **record, "ok": False, "reused": False,
            "error": (cloned.stderr or "clone 失败").strip()[:500],
        }
    checked = _git(["-C", str(target_path), "checkout", "--quiet", commit], timeout=300)
    if checked.returncode != 0:
        shutil.rmtree(target_path, ignore_errors=True)
        return {
            **record, "ok": False, "reused": False,
            "error": (checked.stderr or "checkout 失败").strip()[:500],
        }
    return {**record, "ok": True, "reused": False}


def snapshot_dataset(
    registry: Registry,
    name: str,
    *,
    limit: int = 0,
    snapshot_root: str = "snapshots",
    out_root: Path | str | None = None,
) -> dict:
    root = Path(out_root or BENCH_ROOT)
    entry = registry.get(name)
    manifest = _read_manifest(root, entry)
    if manifest is None:
        raise FetchError(f"{name}: 还没有物化切片，请先执行 fetch")
    instances_path = root / manifest["instances_file"]
    rows = load_jsonl(instances_path)
    if limit and limit > 0:
        rows = rows[:limit]
    base = Path(snapshot_root)
    snapshots: list[dict] = []
    for row in rows:
        repo = str(row.get("repo") or "").strip()
        commit = str(row.get("base_commit") or row.get("commit") or "").strip()
        if not repo or not commit:
            snapshots.append({
                "repo": repo, "commit": commit, "ok": False,
                "error": "缺少 repo/base_commit",
            })
            continue
        target = (base if base.is_absolute() else root / base) / (
            f"{repo.replace('/', '__')}@{commit[:12]}"
        )
        url = entry.repo_url_template.format(repo=repo)
        result = snapshot_one(url, commit, target)
        try:
            result["path"] = target.relative_to(root).as_posix()
        except ValueError:
            result["path"] = target.as_posix()
        snapshots.append(result)
    manifest["snapshots"] = snapshots
    manifest["snapshot_ready"] = bool(snapshots) and all(item.get("ok") for item in snapshots)
    manifest["snapshot_checked_at"] = _now()
    _write_json(_manifest_path(root, entry), manifest)
    return manifest


def verify(registry: Registry | None = None, *, root: Path | str | None = None) -> list[str]:
    registry = registry or load_registry()
    base = Path(root or BENCH_ROOT)
    problems: list[str] = []
    for entry in registry.entries:
        manifest = _read_manifest(base, entry)
        if manifest is None:
            continue
        instances = base / str(manifest.get("instances_file") or "")
        if not instances.is_file():
            problems.append(f"{entry.name}: 缺少切片文件 {instances}")
        else:
            actual = _sha256_file(instances)
            if manifest.get("sha256") and actual != manifest["sha256"]:
                problems.append(
                    f"{entry.name}: 哈希不匹配（manifest={manifest['sha256'][:8]} actual={actual[:8]}）"
                )
            lines = [line for line in instances.read_text(encoding="utf-8").splitlines() if line.strip()]
            if int(manifest.get("count") or 0) != len(lines):
                problems.append(
                    f"{entry.name}: 行数 {len(lines)} != manifest {manifest.get('count')}"
                )
        snapshots = manifest.get("snapshots") or []
        if manifest.get("snapshot_ready") and not snapshots:
            problems.append(f"{entry.name}: snapshot_ready=true 但没有快照记录")
        for snapshot in snapshots:
            path = Path(str(snapshot.get("path") or ""))
            target = path if path.is_absolute() else base / path
            if not target.exists():
                problems.append(f"{entry.name}: 快照不存在 {target}")
            elif snapshot.get("commit") and _git_head(target) != snapshot["commit"]:
                problems.append(f"{entry.name}: 快照 HEAD 与 commit 不一致 {target}")
    return problems


def status(registry: Registry | None = None, *, root: Path | str | None = None) -> list[dict]:
    registry = registry or load_registry()
    base = Path(root or BENCH_ROOT)
    rows: list[dict] = []
    for entry in registry.entries:
        manifest = _read_manifest(base, entry)
        rows.append({
            "name": entry.name,
            "adapter": entry.adapter,
            "kind": entry.kind,
            "role": entry.role,
            "primary": entry.is_primary,
            "license": entry.license,
            "status": entry.status,
            "grade": entry.quality_grade,
            "verification": entry.verification,
            "materialized": manifest is not None,
            "count": int((manifest or {}).get("count") or 0),
            "snapshot_ready": bool((manifest or {}).get("snapshot_ready")),
            "snapshots": len((manifest or {}).get("snapshots") or []),
            "instances_file": str((manifest or {}).get("instances_file") or ""),
        })
    return rows


def build_dedup_index(registry: Registry | None = None, *, root: Path | str | None = None,
                      datasets: list[str] | None = None) -> dict:
    registry = registry or load_registry()
    base = Path(root or BENCH_ROOT)
    names = datasets or [
        entry.name for entry in registry.entries if entry.kind == "external"
    ]
    tasks = []
    for name in names:
        entry = registry.get(name)
        manifest = _read_manifest(base, entry)
        if manifest is None:
            continue
        adapter = get_adapter(entry.adapter)
        instances = adapter.load_instances(entry, root=base / entry.name)
        tasks.extend(adapter.to_tasks(entry, instances))
    result = dedup_tasks(tasks)
    payload = result.to_dict()
    _write_json(base / "dedup.json", payload)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.bench fetch",
        description="数据集下载/切片/快照/校验（联网只发生在这里）",
    )
    parser.add_argument("--list", action="store_true", help="列出注册表")
    parser.add_argument("--status", action="store_true", help="查看物化状态")
    parser.add_argument("--verify", action="store_true", help="校验哈希、行数与快照")
    parser.add_argument("--dedup", action="store_true", help="生成跨数据集去重索引")
    parser.add_argument("--dataset", default="")
    parser.add_argument("--source", default="", help="本地路径 / https URL / hf://org/name/path")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--snapshot", action="store_true", help="checkout 仓库快照")
    parser.add_argument("--snapshot-root", default="snapshots")
    parser.add_argument("--allow-unlicensed", action="store_true")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--root", default="", help="物化根目录，默认 eval/datasets/bench")
    parser.add_argument("--registry", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv or []))
    registry = load_registry(args.registry or None)
    root = Path(args.root) if args.root else None
    if args.list:
        print(json.dumps([
            {
                "name": entry.name, "adapter": entry.adapter, "kind": entry.kind,
                "role": entry.role, "primary": entry.is_primary,
                "license": entry.license, "status": entry.status,
                "grade": entry.quality_grade, "verification": entry.verification,
                "capabilities": entry.capabilities,
            }
            for entry in registry.entries
        ], ensure_ascii=False, indent=2))
        return 0
    if args.status:
        print(json.dumps(status(registry, root=root), ensure_ascii=False, indent=2))
        return 0
    if args.verify:
        problems = verify(registry, root=root)
        print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False))
        return 0 if not problems else 1
    if args.dedup:
        print(json.dumps(
            build_dedup_index(registry, root=root), ensure_ascii=False, indent=2,
        ))
        return 0
    if not args.dataset:
        _build_parser().print_help()
        return 2
    if args.snapshot:
        print(json.dumps(
            snapshot_dataset(
                registry, args.dataset, limit=args.limit,
                snapshot_root=args.snapshot_root, out_root=root,
            ),
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not args.source:
        raise FetchError("需要 --source（本地路径 / https / hf://），或使用 --snapshot")
    print(json.dumps(
        slice_dataset(
            registry, args.dataset, args.source, limit=args.limit,
            allow_unlicensed=args.allow_unlicensed, out_root=root,
            revision=args.revision,
        ),
        ensure_ascii=False, indent=2,
    ))
    return 0


__all__ = [
    "FetchError", "resolve_source", "slice_dataset", "snapshot_one",
    "snapshot_dataset", "verify", "status", "build_dedup_index", "main",
]
