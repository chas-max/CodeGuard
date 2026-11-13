"""节点容器 ReviewNodes：把 11 个节点与 7 个路由绑定为可调用对象。"""
from __future__ import annotations

import functools
import threading
from typing import Callable, Optional

from ..config import Settings, load_settings
from ..errors import SandboxError
from ..github.client import GitHubClient
from ..llm import make_llm
from ..mcp import McpClientManager
from ..orchestration.coordinator import Coordinator
from ..sandbox.backend import SandboxBackend
from ..sandbox.docker_backend import DockerSandboxBackend
from ..sandbox.fake_backend import FakeSandboxBackend
from ..tools.registry import build_toolbox
from ..tools.spec import ToolLimits
from .pipeline import (
    finalize,
    fix_node,
    human_approval,
    hybrid_scan,
    ingest,
    merge_gate,
    merge_node,
    preflight,
    review_node,
    route_after_fix,
    route_after_human,
    route_after_hybrid,
    route_after_merge_gate,
    route_after_preflight,
    route_after_review,
    route_after_validate,
    route_after_verify,
    validate,
    verify_issue,
)


class ReviewNodes:
    def __init__(
        self,
        cfg: Optional[Settings] = None,
        *,
        llm_provider: Optional[Callable] = None,
        github=None,
        sandbox: SandboxBackend | None = None,
        coordinator: Coordinator | None = None,
        token_provider: Optional[Callable[[str | None], str | None]] = None,
        mcp_manager: McpClientManager | None = None,
    ):
        self.cfg = cfg or load_settings()
        # GitHub App 模式：按仓库解析 installation token；PAT 模式保持 cfg.github_token。
        self.token_provider = token_provider
        self._llm_provider = llm_provider or (lambda role: make_llm(role, self.cfg))
        self._llms: dict = {}
        self._llm_lock = threading.Lock()
        self.coordinator = coordinator or Coordinator(max_parallel=self.cfg.coordinator_max_parallel)
        self.mcp_manager = mcp_manager or McpClientManager()
        # Agent 工具执行与 validate 节点共享同一并发上限，避免容器风暴。
        self.sandbox_slots = threading.BoundedSemaphore(max(1, self.cfg.sandbox_max_concurrency))
        self.rt = None
        self.sandbox_error = ""

        if github is not None:
            self.github = github
        elif self.cfg.github_token:
            self.github = GitHubClient(self.cfg.github_token, api_url=self.cfg.github_api_url)
        else:
            self.github = None

        if sandbox is not None:
            self.sandbox = sandbox
        elif self.cfg.sandbox_backend == "fake":
            self.sandbox = FakeSandboxBackend()
        else:
            try:
                self.sandbox = DockerSandboxBackend()
            except SandboxError as exc:
                self.sandbox = None
                self.sandbox_error = str(exc)

        self.ingest = functools.partial(ingest, ctx=self)
        self.preflight = functools.partial(preflight, ctx=self)
        self.hybrid_scan = functools.partial(hybrid_scan, ctx=self)
        self.verify_issue = functools.partial(verify_issue, ctx=self)
        self.validate = functools.partial(validate, ctx=self)
        self.review = functools.partial(review_node, ctx=self)
        self.fix = functools.partial(fix_node, ctx=self)
        self.merge_gate = functools.partial(merge_gate, ctx=self)
        self.human_approval = functools.partial(human_approval, ctx=self)
        self.merge = functools.partial(merge_node, ctx=self)
        self.finalize = functools.partial(finalize, ctx=self)

        self.route_after_preflight = functools.partial(route_after_preflight, ctx=self)
        self.route_after_hybrid = functools.partial(route_after_hybrid, ctx=self)
        self.route_after_verify = functools.partial(route_after_verify, ctx=self)
        self.route_after_validate = functools.partial(route_after_validate, ctx=self)
        self.route_after_review = functools.partial(route_after_review, ctx=self)
        self.route_after_fix = functools.partial(route_after_fix, ctx=self)
        self.route_after_merge_gate = functools.partial(route_after_merge_gate, ctx=self)
        self.route_after_human = functools.partial(route_after_human, ctx=self)

    def github_token(self, repo: str | None = None) -> str | None:
        """当前可用的 GitHub 凭据：App 模式按仓库取 installation token，否则回退 PAT。"""
        if self.token_provider is not None:
            token = self.token_provider(repo)
            if token:
                return token
        return self.cfg.github_token

    def llm(self, role: str):
        with self._llm_lock:
            if role not in self._llms:
                self._llms[role] = self._llm_provider(role)
            return self._llms[role]

    def tools_for(
        self,
        role: str,
        *,
        root,
        job_id: str,
        policy,
        tier: str = "T1",
        repo_map: dict | None = None,
        allowed_files: list[str] | None = None,
        limits: ToolLimits | None = None,
        base_root=None,
        pr_context: dict | None = None,
        findings: list[dict] | None = None,
    ):
        """构造该 Agent 的受限工具箱；任何构造失败都降级为无工具（fail closed）。"""
        try:
            return build_toolbox(
                role=role, root=root, job_id=job_id, ctx=self, policy=policy,
                tier=tier, repo_map=repo_map, allowed_files=allowed_files,
                limits=limits, slots=self.sandbox_slots,
                base_root=base_root, pr_context=pr_context,
                findings=findings, mcp_manager=self.mcp_manager,
            )
        except Exception:  # noqa: BLE001 - 工具层不可用不应让 job 崩在构造阶段
            return None

    def close(self) -> None:
        if self.github is not None and hasattr(self.github, "close"):
            self.github.close()


__all__ = ["ReviewNodes"]
