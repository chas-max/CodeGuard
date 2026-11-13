"""沙箱执行工具：命令白名单、network=none、超时收敛与基础设施错误传播。"""
from __future__ import annotations

from pathlib import Path

from codeguard.config import Settings
from codeguard.engine.policy import Policy
from codeguard.models import SandboxStatus
from codeguard.sandbox.models import SandboxResult
from codeguard.tools.registry import build_toolbox
from tests.fakes import FakeSandbox


class Ctx:
    def __init__(self, sandbox: FakeSandbox | None = None):
        self.cfg = Settings()
        self.sandbox = sandbox or FakeSandbox()
        self.sandbox_slots = None


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    return root


def _box(root: Path, *, ctx: Ctx | None = None, policy: Policy | None = None):
    ctx = ctx or Ctx()
    return build_toolbox(
        role="fixer", root=root, job_id="j", ctx=ctx, policy=policy or Policy(), tier="T1",
    ), ctx


def test_run_tests_uses_template_and_no_network(tmp_path: Path):
    root = _repo(tmp_path)
    box, ctx = _box(root)
    result = box.dispatch("run_tests", {"paths": ["tests/test_a.py"]})
    assert result.ok
    spec = ctx.sandbox.specs[-1]
    assert spec.network == "none"
    assert "tests/test_a.py" in spec.commands[0]
    assert spec.timeout_sec <= 120


def test_run_tests_rejects_source_file(tmp_path: Path):
    root = _repo(tmp_path)
    box, ctx = _box(root)
    result = box.dispatch("run_tests", {"paths": ["src/a.py"]})
    assert result.status == "denied"
    assert not ctx.sandbox.specs


def test_run_tests_timeout_is_tightened(tmp_path: Path):
    root = _repo(tmp_path)
    box, ctx = _box(root)
    box.dispatch("run_tests", {"paths": ["tests/test_a.py"], "timeout_sec": 30})
    assert ctx.sandbox.specs[-1].timeout_sec == 30


def test_run_command_rejects_forbidden_and_unknown(tmp_path: Path):
    root = _repo(tmp_path)
    box, ctx = _box(root)
    assert box.dispatch("run_command", {"argv": ["curl", "http://x"]}).status == "denied"
    assert box.dispatch("run_command", {"argv": ["python", "-c", "print(1)"]}).status == "denied"
    assert box.dispatch("run_command", {"argv": ["git", "push"]}).status == "denied"
    assert not ctx.sandbox.specs


def test_run_command_accepts_allowlisted_prefix(tmp_path: Path):
    root = _repo(tmp_path)
    box, ctx = _box(root)
    result = box.dispatch("run_command", {"argv": ["python", "-m", "pytest", "-q"]})
    assert result.ok
    assert ctx.sandbox.specs[-1].commands[0] == "python -m pytest -q"
    assert ctx.sandbox.specs[-1].network == "none"


def test_run_check_requires_configured_name(tmp_path: Path):
    root = _repo(tmp_path)
    policy = Policy.model_validate({
        "sandbox": {"checks": [{"name": "lint", "command": "ruff check ."}]},
    })
    box, ctx = _box(root, policy=policy)
    assert box.dispatch("run_check", {"name": "nope"}).status == "denied"
    assert box.dispatch("run_check", {"name": "lint"}).ok
    assert ctx.sandbox.specs[-1].commands[0] == "ruff check ."


def test_failed_and_infra_error_are_distinguished(tmp_path: Path):
    root = _repo(tmp_path)
    failed = FakeSandbox(handler=lambda spec: SandboxResult(
        status=SandboxStatus.failed, exit_code=1, stdout="1 failed"))
    box, _ = _box(root, ctx=Ctx(failed))
    result = box.dispatch("run_tests", {"paths": []})
    assert result.status == "failed"
    assert "1 failed" in result.content

    broken = FakeSandbox(handler=lambda spec: SandboxResult(
        status=SandboxStatus.infra_error, exit_code=-1, infra_error="daemon down"))
    box2, _ = _box(root, ctx=Ctx(broken))
    result2 = box2.dispatch("run_tests", {"paths": []})
    assert result2.status == "infra_error"
    assert "daemon down" in result2.error


def test_sandbox_missing_is_infra_error(tmp_path: Path):
    root = _repo(tmp_path)
    ctx = Ctx()
    ctx.sandbox = None
    box = build_toolbox(
        role="reviewer", root=root, job_id="j", ctx=ctx, policy=Policy(), tier="T1",
    )
    result = box.dispatch("run_tests", {"paths": []})
    assert result.status == "infra_error"
