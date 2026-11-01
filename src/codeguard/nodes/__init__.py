"""节点容器 CodeGuardNodes：持有 cfg/llm，把 12 节点与 7 路由绑定为可调用对象。

与原漫剧 GraphNodes 同构：节点函数签名为 fn(state, ctx)，通过 functools.partial
注入 ctx=self；llm_provider 可注入以便测试 mock。rt 属性由 Runtime 在构造时挂载，
节点通过 ctx.rt 发射 SSE 事件与进度。
"""
from __future__ import annotations

import functools
from typing import Callable, Optional

from ..config import CodeGuardConfig, load_config
from ..llm import make_llm
from .pipeline_nodes import (
    build_anchors,
    clarify_ask,
    confirm_anchors,
    director_plan,
    director_resolve,
    director_supervise,
    escalate,
    human_review,
    machine_review,
    route_after_anchors,
    route_after_execute,
    route_after_human,
    route_after_machine,
    route_after_resolve,
    route_after_supervise,
    route_after_understand,
    sandbox_execute,
    understand,
    write_draft,
)


class CodeGuardNodes:
    """所有节点与路由的可调用集合（LangGraph 注册入口）。"""

    def __init__(
        self,
        cfg: Optional[CodeGuardConfig] = None,
        llm_provider: Optional[Callable] = None,
    ):
        self.cfg = cfg or load_config()
        self._llm_provider = llm_provider or (lambda role: make_llm(role, self.cfg))
        self._llms: dict = {}
        # 覆盖率门控降级标记（director_resolve downgrade 时设置，sandbox_execute 消费）
        self._coverage_disabled = False
        # Runtime 引用（由 MangaDramaRuntime 风格的封装在构造后挂载）
        self.rt = None

        # 节点
        self.understand = functools.partial(understand, ctx=self)
        self.clarify_ask = functools.partial(clarify_ask, ctx=self)
        self.director_plan = functools.partial(director_plan, ctx=self)
        self.director_resolve = functools.partial(director_resolve, ctx=self)
        self.write_draft = functools.partial(write_draft, ctx=self)
        self.machine_review = functools.partial(machine_review, ctx=self)
        self.director_supervise = functools.partial(director_supervise, ctx=self)
        self.human_review = functools.partial(human_review, ctx=self)
        self.build_anchors = functools.partial(build_anchors, ctx=self)
        self.confirm_anchors = functools.partial(confirm_anchors, ctx=self)
        self.sandbox_execute = functools.partial(sandbox_execute, ctx=self)
        self.escalate = functools.partial(escalate, ctx=self)
        # 路由
        self.route_after_understand = functools.partial(route_after_understand, ctx=self)
        self.route_after_machine = functools.partial(route_after_machine, ctx=self)
        self.route_after_supervise = functools.partial(route_after_supervise, ctx=self)
        self.route_after_human = functools.partial(route_after_human, ctx=self)
        self.route_after_anchors = functools.partial(route_after_anchors, ctx=self)
        self.route_after_execute = functools.partial(route_after_execute, ctx=self)
        self.route_after_resolve = functools.partial(route_after_resolve, ctx=self)

    def llm(self, role: str):
        if role not in self._llms:
            self._llms[role] = self._llm_provider(role)
        return self._llms[role]
