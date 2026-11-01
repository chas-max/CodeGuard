"""隔离沙箱 pytest 执行引擎：F2P（复现测试）+ P2P（回归测试）双门控 + 分支覆盖率。

安全红线：
- 所有补丁应用与 pytest 执行必须在 tempfile.TemporaryDirectory 隔离副本中进行，
  严禁污染宿主工作区（snapshot_tree_hash 前后校验）。
- 强制 subprocess timeout 防止死循环卡死。
- 失败输出经 _trim_traceback 精准裁剪，只保留最后异常帧附近信息驱动自愈。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .models import SandboxTestResult

IGNORE_PATTERNS = None  # copytree(ignore=...) 在 _clone 中构造
IGNORE_DIR_NAMES = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}


def _clone_repo(repo_dir: Path, dest: Path) -> None:
    """克隆仓库到隔离目录（跳过缓存/虚拟环境目录，保留隐藏文件以外的运行时产物可再生成）。"""
    import shutil

    def _ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n in IGNORE_DIR_NAMES]

    shutil.copytree(repo_dir, dest, ignore=_ignore, dirs_exist_ok=True)


def snapshot_tree_hash(repo_dir: Path) -> str:
    """对仓库全部文件内容计算稳定哈希，用于沙箱执行前后宿主未被污染的校验。"""
    digest = hashlib.sha256()
    for p in sorted(repo_dir.rglob("*")):
        if not p.is_file():
            continue
        if any(part in IGNORE_DIR_NAMES for part in p.parts):
            continue
        rel = p.relative_to(repo_dir).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(hashlib.sha256(p.read_bytes()).digest())
    return digest.hexdigest()


def _trim_traceback(out: str, max_chars: int = 1500) -> str:
    """裁剪 pytest 输出：优先保留最后一个 Traceback 块及其断言上下文，尾部截断。"""
    if not out:
        return ""
    marker = "Traceback (most recent call last)"
    idx = out.rfind(marker)
    if idx > 0:
        start = max(0, idx - 400)
        trimmed = out[start:]
    else:
        # 无显式 Traceback（断言失败等）：保留最后 50 行
        lines = out.strip().splitlines()
        trimmed = "\n".join(lines[-50:])
    if len(trimmed) > max_chars:
        trimmed = trimmed[:max_chars] + "\n...（输出已截断）"
    return trimmed.strip()


def _run_pytest_targets(
    workdir: Path,
    targets: list[str],
    timeout_sec: int = 30,
    extra_args: list[str] | None = None,
) -> tuple[bool, str]:
    """在沙箱目录内运行指定 pytest 节点集，返回 (是否全过, 裁剪后输出)。"""
    if not targets:
        return True, ""
    env = os.environ.copy()
    src_sub = workdir / "src"
    env["PYTHONPATH"] = f"{src_sub}{os.pathsep}{workdir}" if src_sub.exists() else str(workdir)

    cmd = [
        sys.executable, "-m", "pytest",
        "-q", "--tb=short", "--no-header", "-p", "no:cacheprovider",
        *targets,
        *(extra_args or []),
    ]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return proc.returncode == 0, _trim_traceback(out)
    except subprocess.TimeoutExpired:
        return False, f"Sandbox pytest timeout after {timeout_sec}s（疑似死循环，进程已被强制终止）"
    except OSError as err:
        return False, f"Sandbox pytest 启动失败: {err}"


def _run_with_coverage(
    workdir: Path,
    targets: list[str],
    cov_target: str,
    timeout_sec: int,
) -> float:
    """运行全量目标并解析分支覆盖率（pytest-cov）。失败或未安装返回 0.0。"""
    cov_json = workdir / "_cover.json"
    extra = [
        f"--cov={cov_target}",
        "--cov-branch",
        f"--cov-report=json:{cov_json.name}",
    ]
    ok, _ = _run_pytest_targets(workdir, targets, timeout_sec=timeout_sec, extra_args=extra)
    if not cov_json.is_file():
        return 0.0
    import json

    try:
        data = json.loads(cov_json.read_text(encoding="utf-8"))
        return float(data.get("totals", {}).get("percent_covered", 0.0))
    except (OSError, ValueError, KeyError):
        return 0.0


def run_sandbox_verification(
    repo_dir: Path,
    patches: dict[str, str],
    f2p_tests: list[str],
    p2p_tests: list[str],
    timeout_sec: int = 30,
    cov_target: str | None = None,
) -> SandboxTestResult:
    """在临时隔离目录中克隆目标仓库、写入补丁并运行 F2P 与 P2P 测试集。

    Args:
        repo_dir: 宿主仓库目录（只读，绝不被修改）。
        patches: {相对路径: 修复后完整文件内容}，写入沙箱副本。
        f2p_tests: Fail-to-Pass 复现测试节点（pytest nodeid）。
        p2p_tests: Pass-to-Pass 回归测试节点。
        timeout_sec: 每轮 pytest 强制超时。
        cov_target: 覆盖率统计目标（包名或 "."）；None 表示跳过覆盖率。

    Returns:
        SandboxTestResult（resolved = f2p_passed and p2p_passed）。
    """
    host_hash_before = snapshot_tree_hash(repo_dir)
    with tempfile.TemporaryDirectory(prefix="codeguard_sbx_") as tmp_str:
        tmp_repo = Path(tmp_str) / "repo"
        _clone_repo(repo_dir, tmp_repo)

        # 写入待测补丁文件
        for rel_path, content in patches.items():
            target_file = tmp_repo / rel_path
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(content, encoding="utf-8")

        f2p_ok, f2p_out = _run_pytest_targets(tmp_repo, f2p_tests, timeout_sec=timeout_sec)
        p2p_ok, p2p_out = _run_pytest_targets(tmp_repo, p2p_tests, timeout_sec=timeout_sec)

        coverage = 0.0
        if cov_target is not None and f2p_ok and p2p_ok:
            coverage = _run_with_coverage(
                tmp_repo, sorted(set(f2p_tests) | set(p2p_tests)), cov_target, timeout_sec
            )

        errors = []
        if not f2p_ok:
            errors.append(f"[F2P 复现测试未通过]\n{f2p_out}")
        if not p2p_ok:
            errors.append(f"[P2P 回归测试被破坏]\n{p2p_out}")

        result = SandboxTestResult(
            f2p_passed=f2p_ok,
            p2p_passed=p2p_ok,
            resolved=(f2p_ok and p2p_ok),
            branch_coverage_pct=coverage,
            error_summary="\n\n".join(errors),
            raw_stdout=f"{f2p_out}\n{p2p_out}".strip(),
        )

    # 宿主隔离校验：沙箱执行不得改变宿主仓库任何字节
    host_hash_after = snapshot_tree_hash(repo_dir)
    if host_hash_before != host_hash_after:  # pragma: no cover - 防御性断言
        raise RuntimeError("沙箱执行污染了宿主仓库！请检查 _clone_repo 隔离逻辑。")

    return result


def verify_f2p_red(
    repo_dir: Path,
    f2p_tests: list[str],
    *,
    patches: dict[str, str] | None = None,
    timeout_sec: int = 30,
) -> bool:
    """验证 F2P 复现测试在（打补丁前的）仓库上稳定失败（TDD Red 锚定态）。

    返回 False 的情形（均视为锚点无效）：
    - 测试文件缺失 / 收集错误（说明复现测试本身写错了）
    - 测试意外通过（说明没有复现 Bug，"假修"风险）
    - 超时或环境异常
    """
    if patches:
        # 带 patches 时在隔离副本中验证（如"修复后不再 Red"的反向校验）
        with tempfile.TemporaryDirectory(prefix="codeguard_red_") as tmp_str:
            tmp_repo = Path(tmp_str) / "repo"
            _clone_repo(repo_dir, tmp_repo)
            for rel_path, content in patches.items():
                target_file = tmp_repo / rel_path
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(content, encoding="utf-8")
            ok, out = _run_pytest_targets(tmp_repo, f2p_tests, timeout_sec=timeout_sec)
    else:
        ok, out = _run_pytest_targets(repo_dir, f2p_tests, timeout_sec=timeout_sec)
    if ok:
        return False  # 测试通过 → 未复现 Bug → 无效锚点
    if "timeout" in out.lower() or "启动失败" in out:
        return False  # 超时/环境异常 → 不可靠
    if "error" in out.lower() and "no tests ran" in out.lower():
        return False  # 收集失败（文件缺失等）
    return True
