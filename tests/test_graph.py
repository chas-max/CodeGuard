"""完整 12 节点图集成测试：HITL 中断恢复、happy path、escalate 路径。"""
from __future__ import annotations

from pathlib import Path

from langgraph.types import Command

from codeguard.config import load_config
from codeguard.graph import build_graph, make_checkpointer
from codeguard.nodes import CodeGuardNodes
from codeguard.state import initial_state
from tests._codeguard_fakes import (
    HAPPY_MAPPING,
    PARSE_CLEAR,
    PARSE_PARTIAL,
    FakeProvider,
    make_buggy_repo,
)

CFG = {"configurable": {"thread_id": "job-g"}, "recursion_limit": 50}
ISSUE = "divide_safe(10, 0) 抛 ZeroDivisionError，期望返回 0.0"


def _build(tmp_path: Path, mapping: dict):
    cfg = load_config(output_dir=tmp_path / "out", checkpoint_path=tmp_path / "ckpt.db")
    nodes = CodeGuardNodes(cfg, llm_provider=FakeProvider(mapping))
    graph = build_graph(nodes, checkpointer=make_checkpointer(tmp_path / "ckpt.db"))
    return graph, nodes


def _drive_through_interrupts(graph, job_id: str, answer: str, max_rounds: int = 6):
    """循环恢复 HITL 中断（全部批准），直到图到达终态。"""
    config = {"configurable": {"thread_id": job_id}, "recursion_limit": 50}
    for _ in range(max_rounds):
        st = graph.get_state(config)
        if not st.next:
            return st
        interrupt = None
        for t in st.tasks:
            if t.interrupts:
                interrupt = t.interrupts[0].value
                break
        if interrupt is None:
            raise RuntimeError(f"图停在 {st.next} 但无 interrupt 信息")
        itype = interrupt.get("type")
        if itype == "clarify":
            resume = {"answer": answer}
        else:
            resume = {"approved": True, "feedback": ""}
        graph.invoke(Command(resume=resume), config)
    raise RuntimeError("中断轮次超限")


def test_graph_happy_path_with_two_hitl(tmp_path: Path):
    repo = make_buggy_repo(tmp_path)
    host_hash_before = repo.joinpath("calc.py").read_text(encoding="utf-8")
    graph, _ = _build(tmp_path, HAPPY_MAPPING)

    graph.invoke(initial_state(ISSUE, "job-g", str(repo)), CFG)
    st = _drive_through_interrupts(graph, "job-g", answer="补充：b=0 时期望返回 0.0")

    values = st.values
    assert values["status"] == "done", values.get("error")
    assert values["f2p_tests"]
    patch_path = Path(values["patch_path"])
    assert patch_path.is_file()
    patch_text = patch_path.read_text(encoding="utf-8")
    assert "+++ b/calc.py" in patch_text
    assert "+++ b/test_repro_issue.py" in patch_text
    # 宿主仓库未被污染
    assert repo.joinpath("calc.py").read_text(encoding="utf-8") == host_hash_before
    # 审计日志留痕
    log_events = [e["event"] for e in values.get("production_log", [])]
    assert "director_plan" in log_events
    assert "machine_review" in log_events
    assert "sandbox_execute" in log_events


def test_graph_clarify_interrupt_then_proceed(tmp_path: Path):
    repo = make_buggy_repo(tmp_path)
    mapping = {**HAPPY_MAPPING, "understand": [PARSE_PARTIAL, PARSE_CLEAR]}
    graph, _ = _build(tmp_path, mapping)

    graph.invoke(initial_state(ISSUE, "job-g", str(repo)), CFG)
    st = _drive_through_interrupts(graph, "job-g", answer="divide_safe(10, 0) 期望返回 0.0")

    values = st.values
    assert values["clarification_round"] == 1
    assert values["clarification_qa"], "澄清问答必须留档"
    assert values["issue_context"]["completeness"] == "clear"
    assert values["status"] == "done", values.get("error")


def test_graph_llm_total_failure_escalates(tmp_path: Path):
    """所有 LLM 均不可用：understand 失败 → 统筹失败 → escalate 保留产物终止。"""
    repo = make_buggy_repo(tmp_path)
    graph, _ = _build(tmp_path, {})  # 空映射：所有角色队列耗尽

    graph.invoke(initial_state(ISSUE, "job-g", str(repo)), CFG)
    st = graph.get_state(CFG)

    values = st.values
    assert values["status"] == "failed"
    assert values["director_resolution"]["action"] == "escalate"
    assert not st.next  # 到达终态（escalate → END），绝不静默卡死
