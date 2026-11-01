"""HITL：人工内容审核 + 锚定图确认。两处 interrupt，均不调 LLM。"""
from __future__ import annotations

from langgraph.types import interrupt

from ..state import WorkflowState


def human_review(state: WorkflowState, ctx) -> dict:
    """机器审核+导演监督通过后，硬约束：必须人工审核才进制作。

    interrupt payload 暴露 draft/机器结论/导演意见/计数；人工 resume
    {approved: bool, feedback: str}。rejected 时由 write_draft 递增 human_revision_count。
    """
    payload = {
        "type": "human_review",
        "draft": state.get("draft", ""),
        "shots": state.get("shots", []),
        "machine_verdict": state.get("machine_verdict"),
        "director_feedback": state.get("director_feedback", ""),
        "machine_revision_count": state.get("machine_revision_count", 0),
        "human_revision_count": state.get("human_revision_count", 0),
    }
    decision = interrupt(payload)
    if not isinstance(decision, dict):
        decision = {"approved": False, "feedback": "无效的人工决策"}
    decision.setdefault("approved", False)
    decision.setdefault("feedback", "")
    return {
        "human_decision": decision,
        "status": "anchors_pending" if decision.get("approved") else "writing",
    }


def confirm_anchors(state: WorkflowState, ctx) -> dict:
    """锚定图生成后的人工确认。approve 则冻结角色外观（confirmed=True），否则重画。

    resume {approved: bool, revisions: {char_id: ...}}。rejected 时递增 anchor_round。
    """
    bible = state.get("character_bible") or {}
    anchors = [
        {
            "char_id": cid,
            "name": card.get("name", ""),
            "anchor_image_path": card.get("anchor_image_path"),
            "appearance_spec": card.get("appearance_spec", ""),
        }
        for cid, card in bible.items()
        if card.get("anchor_image_path")
    ]
    payload = {"type": "confirm_anchors", "anchors": anchors}
    decision = interrupt(payload)
    if not isinstance(decision, dict):
        decision = {"approved": False, "revisions": {}}
    decision.setdefault("approved", False)
    decision.setdefault("revisions", {})

    if decision.get("approved"):
        confirmed = {cid: {"confirmed": True} for cid in bible}
        return {
            "anchor_decision": decision,
            "character_bible": confirmed,
            "status": "producing",
        }
    return {
        "anchor_decision": decision,
        "anchor_round": state.get("anchor_round", 0) + 1,
        "status": "confirming_anchors",
    }
