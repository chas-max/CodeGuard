"""异常统筹 Agent 提示词：分析各 Agent 上报的异常，路由自愈策略。"""
from __future__ import annotations

import json


def resolve_prompt(
    issues: list[dict],
    current_status: str,
    resolve_count: int,
    max_resolve: int,
) -> str:
    """director_resolve：异常分析 + 自愈策略路由。

    输出的 action/target_node 将由 route_after_resolve 确定性执行：
    retry/downgrade → 跳转 target_node；escalate → 终止；skip → 按状态续跑。
    """
    return f"""你是 SRE 统筹专家。流水线中多个 Agent 上报了异常，请给出处理策略。

## 待处理异常
{json.dumps(issues, ensure_ascii=False, indent=2)}

## 当前流水线状态
status={current_status}，已是第 {resolve_count}/{max_resolve} 次统筹。

## 可选 action 与 target_node
- retry：重试某节点。target_node ∈ write_draft（重新生成补丁）/ build_anchors（重建复现测试）/ sandbox_execute（重跑沙箱）
- downgrade：降级策略。如：放弃覆盖率门控、跳过 Ruff、缩小修复范围
- skip：跳过阻塞项继续（仅在异常不阻塞主线时）
- escalate：无法自动处理，保留产物终止（例如环境不可用、异常反复出现）

## 硬性规则
若 {resolve_count} >= {max_resolve}，必须选择 escalate。

## 输出要求
只输出一个 JSON 对象：
{{
  "action": "retry | downgrade | skip | escalate",
  "target_node": "retry/downgrade 时的目标节点，否则空字符串",
  "strategy": "具体处理策略说明",
  "escalate_reason": "escalate 时的原因，否则空字符串"
}}"""
