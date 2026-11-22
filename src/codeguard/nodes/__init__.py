"""节点容器 ReviewNodes：绑定 CodeGuard v2 全部 16 个节点与 9 条条件路由。"""
from __future__ import annotations

import functools
import threading
from pathlib import Path
from typing import Callable, Optional

from ..config import Settings, load_settings
from ..errors import SandboxError
from ..github.client import GitHubClient
from ..llm import make_llm
from ..mcp import McpClientManager
from ..memory import ProceduralMemoryStore, build_embedder
from ..orchestration.coordinator import Coordinator
from ..sandbox.backend import SandboxBackend
from ..sandbox.docker_backend import DockerSandboxBackend
from ..sandbox.fake_backend import FakeSandboxBackend
from ..tools.registry import build_toolbox
from ..tools.spec import ToolLimits
from .context import pr_reader_node, prepare_context
from .delivery import (
    human_approval_node,
    merge_gate_node,
    merge_node,
    persist_memory_and_finalize_node,
    summary_and_submit_node,
)
from .fix_qa import coder_node, qa_checker_node, verify_gate_node
from .reviewers import (
    dispatch_reviewers,
    review_architecture,
    review_logic,
    review_security,
    review_tests,
    synthesize_findings,
)
from .routing import (
    route_after_coder,
    route_after_human,
    route_after_merge_gate,
    route_after_pr_reader,
    route_after_prepare,
    route_after_qa,
    route_after_summary,
    route_after_synthesize,
    route_after_verify_gate,
)


def _resolve_memory_db_path(cfg: Settings) -> Path:
    """将默认向量库路径隔离到测试/任务专属的 data_dir，避免测试交叉污染。"""
    default_mem = Path("data/procedural_memory.db")
    default_data = Path("data/jobs")
    if cfg.memory_db_path == default_mem and cfg.data_dir != default_data:
        return Path(cfg.data_dir) / "procedural_memory.db"
    return Path(cfg.memory_db_path)


class ReviewNodes:
    """LangGraph v2 节点与共享依赖上下文容器。"""

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
        memory_store: Optional[ProceduralMemoryStore] = None,
    ):
        self.cfg = cfg or load_settings()
        # GitHub App 模式：按仓库解析 installation token；PAT 模式保持 cfg.github_token。
        self.token_provider = token_provider
        self._uses_default_llm = llm_provider is None
        self._llm_provider = llm_provider or (lambda role, **kw: make_llm(role, self.cfg, **kw))
        self._llms: dict = {}
        self._llm_lock = threading.Lock()
        self.coordinator = coordinator or Coordinator(max_parallel=self.cfg.coordinator_max_parallel)
        self.mcp_manager = mcp_manager or McpClientManager()
        # Agent 工具执行与 verify_gate 节点共享同一并发上限，避免容器风暴。
        self.sandbox_slots = threading.BoundedSemaphore(max(1, self.cfg.sandbox_max_concurrency))
        self.rt = None
        self.sandbox_error = ""

        self._owns_memory_store = memory_store is None
        if memory_store is not None:
            self.memory_store = memory_store
        else:
            mem_path = _resolve_memory_db_path(self.cfg)
            self.memory_store = ProceduralMemoryStore(
                mem_path,
                embedder=build_embedder(self.cfg),
            )

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

        # ── 16 个 v2 节点绑定 ──
        self.prepare_context = functools.partial(prepare_context, ctx=self)
        self.pr_reader = functools.partial(pr_reader_node, ctx=self)
        self.dispatch_reviewers = functools.partial(dispatch_reviewers, ctx=self)
        self.review_logic = functools.partial(review_logic, ctx=self)
        self.review_security = functools.partial(review_security, ctx=self)
        self.review_architecture = functools.partial(review_architecture, ctx=self)
        self.review_tests = functools.partial(review_tests, ctx=self)
        self.synthesize_findings = functools.partial(synthesize_findings, ctx=self)
        self.coder_agent = functools.partial(coder_node, ctx=self)
        self.qa_checker = functools.partial(qa_checker_node, ctx=self)
        self.verify_gate = functools.partial(verify_gate_node, ctx=self)
        self.summary_and_submit = functools.partial(summary_and_submit_node, ctx=self)
        self.merge_gate = functools.partial(merge_gate_node, ctx=self)
        self.human_approval = functools.partial(human_approval_node, ctx=self)
        self.merge = functools.partial(merge_node, ctx=self)
        self.persist_memory_and_finalize = functools.partial(
            persist_memory_and_finalize_node, ctx=self,
        )

        # ── 9 条条件路由绑定 ──
        self.route_after_prepare = functools.partial(route_after_prepare, ctx=self)
        self.route_after_pr_reader = functools.partial(route_after_pr_reader, ctx=self)
        self.route_after_synthesize = functools.partial(route_after_synthesize, ctx=self)
        self.route_after_coder = functools.partial(route_after_coder, ctx=self)
        self.route_after_qa = functools.partial(route_after_qa, ctx=self)
        self.route_after_verify_gate = functools.partial(route_after_verify_gate, ctx=self)
        self.route_after_summary = functools.partial(route_after_summary, ctx=self)
        self.route_after_merge_gate = functools.partial(route_after_merge_gate, ctx=self)
        self.route_after_human = functools.partial(route_after_human, ctx=self)

    def github_token(self, repo: str | None = None) -> str | None:
        """当前可用的 GitHub 凭据：App 模式按仓库取 installation token，否则回退 PAT。"""
        if self.token_provider is not None:
            token = self.token_provider(repo)
            if token:
                return token
        return self.cfg.github_token

    def llm(self, role: str, *, tier: str = "T1", phase: str = "default"):
        """按角色（及思考分级/阶段）懒加载并缓存 LLM 客户端（线程安全）。"""
        supports_profile = self._uses_default_llm or bool(getattr(self._llm_provider, "_supports_profile", False))
        key = (role, tier, phase) if supports_profile else role
        with self._llm_lock:
            if key not in self._llms:
                self._llms[key] = (
                    self._llm_provider(role, tier=tier, phase=phase)
                    if supports_profile
                    else self._llm_provider(role)
                )
            return self._llms[key]

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
        allowed_test_addition_files: list[str] | None = None,
        limits: ToolLimits | None = None,
        base_root=None,
        pr_context: dict | None = None,
        findings: list[dict] | None = None,
        memory_store: Optional[ProceduralMemoryStore] = None,
    ):
        """构造该 Agent 的受限工具箱；任何构造失败都降级为无工具（fail closed）。"""
        try:
            return build_toolbox(
                role=role,
                root=root,
                job_id=job_id,
                ctx=self,
                policy=policy,
                tier=tier,
                repo_map=repo_map,
                allowed_files=allowed_files,
                allowed_test_addition_files=allowed_test_addition_files,
                limits=limits,
                slots=self.sandbox_slots,
                base_root=base_root,
                pr_context=pr_context,
                findings=findings,
                mcp_manager=self.mcp_manager,
                memory_store=memory_store if memory_store is not None else self.memory_store,
            )
        except Exception:  # noqa: BLE001 - 工具层不可用不应让 job 崩在构造阶段
            return None

    def close(self) -> None:
        """释放持有的 GitHub HTTP 客户端与程序化向量记忆库连接。"""
        if self.github is not None and hasattr(self.github, "close"):
            self.github.close()
        if self._owns_memory_store and self.memory_store is not None and hasattr(self.memory_store, "close"):
            self.memory_store.close()


__all__ = ["ReviewNodes"]
