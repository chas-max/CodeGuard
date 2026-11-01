"""节点容器 GraphNodes：持有 cfg/llm/t2i/tts，把节点函数与路由绑定为可调用对象。"""
from __future__ import annotations

import functools
from typing import Callable, Optional

from ..config import MangaDramaConfig
from ..llm import make_llm
from ..tools import get_t2i, get_tts
from .director import (
    director_plan,
    director_resolve,
    director_supervise,
    route_after_anchors,
    route_after_human,
    route_after_machine,
    route_after_produce,
    route_after_resolve,
    route_after_supervise,
)
from .escalate import escalate
from .human import confirm_anchors, human_review
from .produce import build_anchors, produce_video
from .review import machine_review
from .understand import clarify_ask, route_after_understand, understand
from .write import write_draft


class GraphNodes:
    """所有节点与路由的可调用集合。

    节点函数签名为 fn(state, ctx)；通过 functools.partial 注入 ctx=self 绑定为
    fn(state)，便于 LangGraph 注册。llm_provider/t2i/tts 可注入以便测试 mock。
    """

    def __init__(
        self,
        cfg: MangaDramaConfig,
        llm_provider: Optional[Callable] = None,
        t2i=None,
        tts=None,
    ):
        self.cfg = cfg
        self._llm_provider = llm_provider or (lambda role: make_llm(role, cfg))
        self._llms: dict = {}
        self._t2i = t2i
        self._tts = tts
        # Tavily 临时禁用标记（由 director_resolve 在降级时设置）
        self._tavily_disabled = False
        # 跨节点待上报 issues 暂存区（write_draft 搜索失败时使用）
        self._pending_issues: list[dict] = []

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
        self.produce_video = functools.partial(produce_video, ctx=self)
        self.escalate = functools.partial(escalate, ctx=self)
        # 路由
        self.route_after_understand = functools.partial(route_after_understand, ctx=self)
        self.route_after_machine = functools.partial(route_after_machine, ctx=self)
        self.route_after_supervise = functools.partial(route_after_supervise, ctx=self)
        self.route_after_human = functools.partial(route_after_human, ctx=self)
        self.route_after_anchors = functools.partial(route_after_anchors, ctx=self)
        self.route_after_produce = functools.partial(route_after_produce, ctx=self)
        self.route_after_resolve = functools.partial(route_after_resolve, ctx=self)

    def llm(self, role: str):
        if role not in self._llms:
            self._llms[role] = self._llm_provider(role)
        return self._llms[role]

    def t2i(self):
        return self._t2i or get_t2i(self.cfg)

    def tts(self):
        return self._tts or get_tts(self.cfg)

    @property
    def tavily_enabled(self) -> bool:
        """Tavily 搜索是否可用（考虑 director_resolve 的降级标记）。"""
        return self.cfg.tavily_enabled and not self._tavily_disabled
