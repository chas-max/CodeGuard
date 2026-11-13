"""测试基建：Fake LLM / GitHub / Sandbox 与 fixture 仓库。"""
from __future__ import annotations

import json
import subprocess
from collections import deque
from pathlib import Path
from typing import Iterable

from codeguard.models import SandboxStatus
from codeguard.sandbox.models import SandboxResult, SandboxSpec
from codeguard.tools.fs_tools import ReadFileArgs
from codeguard.tools.spec import ToolResult, ToolSpec
from eval.local_github import LocalGitHub


class FakeResponse:
    def __init__(self, content: str):
        self.content = content
        self.usage_metadata = None


class FakeLLM:
    def __init__(self, responses: list[str]):
        self._queue = deque(responses)
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.prompts.append(messages[0].content)
        if not self._queue:
            raise RuntimeError(f"FakeLLM 队列耗尽: {messages[0].content[:80]!r}")
        return FakeResponse(self._queue.popleft())


class FakeProvider:
    def __init__(self, mapping: dict[str, list[str]]):
        self._mapping = mapping

    def __call__(self, role: str) -> FakeLLM:
        return FakeLLM(list(self._mapping.get(role, [])))


class RepeatingLLM(FakeLLM):
    """队列耗尽后重复最后一个响应。

    全链路伪 PR 只需要「每个 role 至少有一个合法输出」，不该关心调用了几次
    （重试、多轮修复都会多调），所以这里不做逐条脚本化。
    """

    def invoke(self, messages):
        self.prompts.append(messages[0].content)
        if not self._queue:
            raise RuntimeError(f"RepeatingLLM 没有配置响应: {messages[0].content[:80]!r}")
        if len(self._queue) > 1:
            return FakeResponse(self._queue.popleft())
        return FakeResponse(self._queue[0])


class RepeatingProvider:
    """按 role 给一个固定响应；role 未配置直接报错，避免静默走空分支。"""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping

    def __call__(self, role: str) -> RepeatingLLM:
        if role not in self._mapping:
            raise RuntimeError(f"RepeatingProvider 未配置 role: {role}")
        return RepeatingLLM([self._mapping[role]])



class FakeGitHub(LocalGitHub):
    """测试替身：行为复用 `eval.local_github.LocalGitHub`，只改默认 merge sha。

    单一实现避免测试替身与离线运行器的行为漂移。
    """

    def __init__(self, merge_sha: str = "merge0123456789abcdef0123456789abcdef"):
        super().__init__(merge_sha=merge_sha)

    def get_user(self) -> dict:
        return {"login": "tester"}


class FakeAppAuth:
    """GitHub App 鉴权替身：不触网，记录 invalidate/refresh 调用。"""

    def __init__(self, *, configured: bool = True, slug: str = "codeguard", repos: list[dict] | None = None):
        self._configured = configured
        self.slug = slug
        self._repos = list(repos if repos is not None else [{
            "full_name": "acme/demo", "private": False, "default_branch": "main",
            "description": "demo repo", "html_url": "https://github.com/acme/demo",
            "language": "Python", "pushed_at": "", "open_issues": 0, "installation_id": 1,
        }])
        self.invalidated: list[str | None] = []
        self.refreshed = 0
        self.cleared = 0
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return self._configured

    def load(self):
        return None

    def manifest(self, public_url: str, *, name: str = "CodeGuard") -> dict:
        base = public_url.rstrip("/")
        return {
            "name": name,
            "hook_attributes": {"url": f"{base}/webhooks/github", "active": True},
            "redirect_url": f"{base}/api/github/app/callback",
        }

    def exchange_manifest(self, code: str):
        from codeguard.github.app_auth import AppCredentials

        if code == "used":
            from codeguard.errors import GitHubAuthError

            raise GitHubAuthError("code 已使用")
        self._configured = True
        return AppCredentials(app_id=123, slug=self.slug, private_key="pem", webhook_secret="s" * 32)

    def installations(self, *, refresh: bool = False) -> list[dict]:
        if refresh:
            self.refreshed += 1
        return [{"id": 1, "account": {"login": "acme"}}]

    def repos(self, *, refresh: bool = False) -> list[dict]:
        if refresh:
            self.refreshed += 1
        return list(self._repos)

    def token_for_repo(self, repo: str | None = None) -> str:
        return "ghs_fake"

    def invalidate(self, repo: str | None = None) -> None:
        self.invalidated.append(repo)

    def install_url(self) -> str:
        return f"https://github.com/apps/{self.slug}/installations/new"

    def webhook_secret(self) -> str:
        return "s" * 32

    def status(self) -> dict:
        return {
            "configured": self._configured,
            "app_id": 123 if self._configured else 0,
            "slug": self.slug if self._configured else "",
            "name": "CodeGuard",
            "owner": "acme",
            "html_url": f"https://github.com/apps/{self.slug}",
            "install_url": self.install_url() if self._configured else "",
            "installations": 1 if self._configured else 0,
            "repos": len(self._repos) if self._configured else 0,
            "webhook_configured": self._configured,
            "private_key_configured": self._configured,
            "last_error": self.last_error,
        }

    def clear(self) -> None:
        self.cleared += 1
        self._configured = False


class FakeSandbox:
    """根据 workspace 目录名返回脚本化结果。"""

    def __init__(self, handler=None):
        self.specs: list[SandboxSpec] = []
        self._handler = handler

    def run(self, spec: SandboxSpec) -> SandboxResult:
        self.specs.append(spec)
        if self._handler is not None:
            return self._handler(spec)
        name = spec.workspace.name
        if name == "base":
            return SandboxResult(status=SandboxStatus.failed, exit_code=1, stdout="base failed")
        if name.startswith("fix_"):
            return SandboxResult(status=SandboxStatus.passed, exit_code=0, stdout="fix passed")
        return SandboxResult(status=SandboxStatus.passed, exit_code=0, stdout="head passed")


class FakeToolBox:
    """脚本化工具箱：按队列返回 ToolResult，记录 dispatch 调用。"""

    def __init__(
        self,
        results: Iterable[ToolResult] | None = None,
        *,
        specs: list[ToolSpec] | None = None,
        role: str = "fake",
        capability: str = "read",
    ):
        self.role = role
        self.capability = capability
        self._specs = specs if specs is not None else [
            ToolSpec(
                name="read_file", description="fake read", args_model=ReadFileArgs,
                capability="read",
            )
        ]
        self._queue = deque(results or [])
        self.calls: list[tuple[str, dict]] = []
        self._events: list[dict] = []
        self._deadline = None

    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    def set_deadline(self, deadline) -> None:
        self._deadline = deadline

    def dispatch(self, name: str, args: dict) -> ToolResult:
        self.calls.append((name, args))
        if self._queue:
            result = self._queue.popleft()
        else:
            result = ToolResult(status="failed", error="FakeToolBox 结果队列已耗尽")
        result.output_chars = result.output_chars or len(result.content)
        self._events.append({
            "role": self.role, "tool": name, "args": json.dumps(args, ensure_ascii=False)[:200],
            "status": result.status, "duration_ms": result.duration_ms,
            "output_chars": result.output_chars, "error": result.error,
        })
        return result

    @property
    def events(self) -> list[dict]:
        return list(self._events)


def init_git(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=False, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=False, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=False, capture_output=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=False, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=False, capture_output=True)


def make_fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    """base 有 bug + 复现测试；head 为修复版本。"""
    base = tmp_path / "base"
    head = tmp_path / "head"
    for root in (base, head):
        (root / "tests").mkdir(parents=True, exist_ok=True)
    (base / "calc.py").write_text(
        "def divide_safe(a: float, b: float) -> float:\n"
        "    return a / b\n",
        encoding="utf-8",
    )
    (head / "calc.py").write_text(
        "def divide_safe(a: float, b: float) -> float:\n"
        "    if b == 0:\n"
        "        return 0.0\n"
        "    return a / b\n",
        encoding="utf-8",
    )
    (base / "tests" / "test_calc.py").write_text(
        "from calc import divide_safe\n\n"
        "def test_zero_divide():\n"
        "    assert divide_safe(10, 0) == 0.0\n",
        encoding="utf-8",
    )
    (head / "tests" / "test_calc.py").write_text(
        "from calc import divide_safe\n\n"
        "def test_zero_divide():\n"
        "    assert divide_safe(10, 0) == 0.0\n",
        encoding="utf-8",
    )
    init_git(base)
    init_git(head)
    return base, head


CLAIM_JSON = json.dumps({
    "kind": "bugfix",
    "statement": "divide_safe(10, 0) 抛 ZeroDivisionError",
    "expected": "返回 0.0",
    "actual": "抛异常",
    "repro_steps": ["调用 divide_safe(10, 0)"],
    "affected_paths": ["calc.py"],
    "linked_issue": 1,
    "confidence": 0.95,
}, ensure_ascii=False)

SEMANTIC_EMPTY = json.dumps({"findings": [], "limitations": []}, ensure_ascii=False)
REVIEW_APPROVE = json.dumps({
    "verdict": "approve",
    "confidence": 0.9,
    "summary": "修复正确",
    "findings": [],
    "fix_instructions": [],
    "residual_risks": [],
}, ensure_ascii=False)
