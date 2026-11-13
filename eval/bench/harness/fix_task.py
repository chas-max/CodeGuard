"""修复任务 harness：快照 → 补丁 → 测试 → FAIL_TO_PASS / PASS_TO_PASS。

同时产出 F1 外部探针：`patch_paths(patch)` 与 `git status --porcelain` 的实际
变更路径必须一致，作为 PR 理解维度的确定性证据。环境起不来（pytest/依赖导入
失败）标 `env_error`，不计入分母。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..models import TaskSpec
from .patch_source import resolve_patch
from .pytest_runner import prepare_environment, run_tests
from .workspace import apply_candidate_patch, prepare_workspace, workspace_tree_hash


def snapshot_path(task: TaskSpec, snapshot_root: Path | str) -> Path | None:
    repo = str(task.identity.get("repo") or "").strip()
    commit = str(task.identity.get("base_commit") or "").strip()
    if not repo or not commit:
        return None
    return Path(snapshot_root) / f"{repo.replace('/', '__')}@{commit[:12]}"


def _clean_path(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.replace("\\", "/").strip()


def _git_name_list(root: Path | str, args: list[str]) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [_clean_path(line) for line in (proc.stdout or "").splitlines() if line.strip()]


def workspace_changed_paths(root: Path | str) -> list[str]:
    """真实变更路径（工作树 vs 索引 + 未跟踪文件），只用于 F1 探针。

    不用 `git status --porcelain`：工作区是 `git init` 出来的，索引相对空 HEAD
    全是 “新增”，无法区分基线文件与补丁引入的文件。
    """
    tracked = _git_name_list(root, ["diff", "--name-only", "--no-renames"])
    untracked = _git_name_list(root, ["ls-files", "--others", "--exclude-standard"])
    return sorted({path for path in [*tracked, *untracked] if path})


def build_f1_probe(root: Path | str, patch_text: str) -> dict:
    """补丁解析路径 vs 实际变更路径；两者一致才说明变更集理解正确。"""
    from codeguard.engine.diff import patch_paths

    error = ""
    try:
        expected = sorted({_clean_path(item) for item in patch_paths(patch_text)})
    except Exception as exc:  # noqa: BLE001 - 非法补丁是探针结论，不是崩溃
        expected = []
        error = f"{type(exc).__name__}: {exc}"
    actual = workspace_changed_paths(root)
    payload: dict = {
        "expected": expected,
        "actual": actual,
        "passed": not error and actual == expected,
    }
    if error:
        payload["error"] = error
    return payload


def run_fix_task(
    task: TaskSpec,
    *,
    snapshot_root: Path | str,
    sandbox,
    work_root: Path | str,
    patch_source: str = "gold",
    provider=None,
    keep_work: bool = False,
) -> tuple[str, dict]:
    snapshot = snapshot_path(task, snapshot_root)
    if snapshot is None or not snapshot.is_dir():
        return "skipped", {
            "stage": "snapshot",
            "error": f"快照不存在: {snapshot}（先执行 fetch --snapshot）",
        }

    workspace = prepare_workspace(snapshot, Path(work_root) / task.task_id)
    try:
        patch = resolve_patch(task, patch_source, provider)
        if not patch.ok:
            status = "skipped" if patch_source == "system" and provider is None else "error"
            return status, {
                "stage": "patch_source", "patch_source": patch.source,
                "resolved": False, "applied": False, "error": patch.error,
            }

        ok, output = apply_candidate_patch(workspace, patch.patch)
        probe = build_f1_probe(workspace.root, patch.patch)
        if not ok:
            return "ok", {
                "stage": "apply", "patch_source": patch.source,
                "resolved": False, "applied": False,
                "error": output[:800], "f1_probe": probe,
                "cost_cny": sum(
                    float(item.get("cost_cny", 0.0)) for item in patch.costs
                ),
            }

        test_patch = str((task.input or {}).get("test_patch") or "")
        if test_patch.strip():
            ok, output = apply_candidate_patch(workspace, test_patch)
            if not ok:
                return "error", {
                    "stage": "test_patch", "patch_source": patch.source,
                    "resolved": False, "applied": True, "f1_probe": probe,
                    "error": f"test_patch 应用失败: {output[:800]}",
                }

        image = str((task.env or {}).get("image") or "codeguard-sandbox-python:3.12")
        prepare_commands = [
            str(item) for item in ((task.env or {}).get("prepare_commands") or [])
        ]
        if prepare_commands:
            prepared = prepare_environment(
                sandbox, workspace=workspace.root, image=image,
                commands=prepare_commands, job_id=task.task_id,
            )
            if prepared.infra_error or prepared.exit_code != 0:
                return "error", {
                    "stage": "prepare", "patch_source": patch.source,
                    "resolved": False, "applied": True, "f1_probe": probe,
                    "error": prepared.infra_error or "prepare 命令退出码非 0",
                    "test_logs": prepared.logs,
                }

        fail_to_pass = [str(item) for item in (task.expected.get("fail_to_pass") or [])]
        pass_to_pass = [str(item) for item in (task.expected.get("pass_to_pass") or [])]
        run = run_tests(
            sandbox, workspace=workspace.root, image=image,
            nodes=fail_to_pass + pass_to_pass,
            test_command=str((task.env or {}).get("test_command") or "python -m pytest -q"),
            job_id=task.task_id,
        )
        if run.infra_error:
            return "error", {
                "stage": "tests", "patch_source": patch.source,
                "resolved": False, "applied": True, "infra_error": run.infra_error,
                "f1_probe": probe, "test_logs": run.logs,
            }
        if not any(
            status in ("passed", "failed", "error") for status in run.statuses.values()
        ):
            markers = (
                "ModuleNotFoundError", "ImportError", "SyntaxError",
                "Traceback (most recent call last)",
            )
            if any(marker in (run.logs or "") for marker in markers):
                return "error", {
                    "stage": "tests", "patch_source": patch.source,
                    "resolved": False, "applied": True, "env_error": True,
                    "error": "测试环境无法启动（pytest/依赖导入失败），不计入分母",
                    "f1_probe": probe, "test_logs": run.logs,
                    "invalid_nodes": list(run.invalid),
                }

        f2p_status = {node: run.statuses.get(node, "missing") for node in fail_to_pass}
        p2p_status = {node: run.statuses.get(node, "missing") for node in pass_to_pass}
        resolved = bool(fail_to_pass) and all(
            status == "passed" for status in f2p_status.values()
        )
        return "ok", {
            "stage": "done",
            "patch_source": patch.source,
            "applied": True,
            "resolved": resolved,
            "fail_to_pass": f2p_status,
            "pass_to_pass": p2p_status,
            "test_statuses": dict(run.statuses),
            "test_logs": run.logs,
            "invalid_nodes": list(run.invalid),
            "exit_code": run.exit_code,
            "tree_hash": workspace_tree_hash(workspace),
            "f1_probe": probe,
            "cost_cny": sum(float(item.get("cost_cny", 0.0)) for item in patch.costs),
        }
    finally:
        if not keep_work:
            workspace.cleanup()


__all__ = [
    "snapshot_path", "workspace_changed_paths", "build_f1_probe", "run_fix_task",
]
