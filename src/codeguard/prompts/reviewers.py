"""Node 3 四个并行视角 Reviewer 的独立提示词模板。"""
from __future__ import annotations

import json

from ..models import REVIEWER_LABELS, ReviewerMemory, ReviewerRole
from .shared import UNTRUSTED_NOTICE, wrap_untrusted

ROLE_FOCUS: dict[str, str] = {
    "review_logic": """你专责【逻辑错误与边界处理】审查，重点关注：
1. 空值/None、除零、索引越界、空集合、类型不匹配、边界条件遗漏；
2. 控制流错误、早退漏清理、状态机转换异常、异常处理吞错或语义颠倒；
3. 并发竞态、锁/连接/文件句柄泄漏、幂等性与重试逻辑漏洞。
注意：不要越界评价纯格式或无关视角的问题。""",
    "review_security": """你专责【安全规范与漏检】审查，重点关注：
1. 注入风险（SQL/命令/路径穿越/模板/反序列化）、SSRF、XSS、不安全反射；
2. 认证鉴权绕过、权限提升、硬编码密钥/Token、敏感信息日志泄露；
3. 确定性静态扫描未覆盖到的跨函数污点传播、不安全默认配置与高危依赖使用。
注意：所有安全阻塞项必须给出明确的触发路径与可验证证据。""",
    "review_architecture": """你专责【项目规范与架构设计】审查，重点关注：
1. 严格对照仓库基线规范（review.md）与工程约定检查命名、类型标注、分层边界；
2. 跨模块耦合、循环依赖、公共 API 签名破坏与向后兼容性；
3. 重复代码、职责过载、未受控副作用或违反最小权限原则的设计。""",
    "review_tests": """你专责【项目测试与回归覆盖】审查，重点关注：
1. 修改的核心逻辑或修复路径是否缺少对应单元测试/边界用例；
2. PR 中新增或修改的测试是否存在空断言、Mock 过度失真、放宽或删除原有断言；
3. 是否存在未覆盖的失败路径与回归风险（需补测时明确指出目标测试文件路径与用例设计）。""",
}


def reviewer_perspective_prompt(
    *,
    role: ReviewerRole,
    pr_brief: dict,
    diff_text: str,
    repo_context: str,
    guidance: str,
    deterministic_findings: list[dict],
    memory: ReviewerMemory,
    round_no: int,
    round_mode: str,
    tools_block: str = "",
) -> str:
    focus = ROLE_FOCUS.get(role, ROLE_FOCUS["review_logic"])
    label = REVIEWER_LABELS.get(role, role)
    tools_section = f"\n{tools_block}" if tools_block else ""
    det_brief = [
        {
            "rule_id": f.get("rule_id"),
            "path": f.get("path"),
            "line": f.get("line"),
            "severity": f.get("severity"),
            "message": str(f.get("message", ""))[:160],
        }
        for f in deterministic_findings[:30]
    ]
    mem_dict = memory.model_dump(mode="json")
    mode_hint = (
        "增量复审（只聚焦本轮 fix_patch 是否彻底解决此前问题或引入新缺陷，不要重复未变动代码的旧问题）"
        if round_mode == "incremental"
        else "全量初审（基于代码分类划分、前置扫描和仓库工具开展本视角深度审查）"
    )
    return f"""你是 CodeGuard 专业代码审查 Agent（角色：{role} / {label}）。
{focus}

{UNTRUSTED_NOTICE}

## 审查轮次与模式
- 当前轮次：第 {round_no} 轮
- 审查模式：{mode_hint}

## 你的独立短期记忆（严禁重复提出已记录/已解决的同一问题）
{json.dumps(mem_dict, ensure_ascii=False, indent=2)}

## Node 2 PR 结构化简报
{json.dumps(pr_brief, ensure_ascii=False, indent=2)[:8000]}

## 项目规范（来自 base 分支）
{wrap_untrusted("guidance", guidance or "无特殊项目规范", max_chars=5000)}

## 前序确定性静态检测摘要（已发现项勿原样复读）
{json.dumps(det_brief, ensure_ascii=False, indent=2)}

## 待审查代码 Diff
{wrap_untrusted("diff", diff_text, max_chars=35000)}

## 关联仓库上下文
{wrap_untrusted("repo_context", repo_context, max_chars=25000)}
{tools_section}
## 输出要求
只输出一个合法 JSON 对象：
{{
  "role": "{role}",
  "needs_modification": true 或 false,
  "summary": "本视角审查结论摘要",
  "findings": [
    {{
      "category": "correctness | security | architecture | design | tests | api_contract | concurrency | data_flow | style",
      "severity": "critical | major | minor | info",
      "blocking": true 或 false,
      "auto_fixable": true 或 false,
      "path": "真实仓库相对路径",
      "line": 整数行号,
      "message": "清晰问题描述",
      "evidence": "具体代码事实/符号引用/行号证据（blocking 必填）",
      "suggestion": "明确可执行的修改建议",
      "confidence": 0.0 到 1.0
    }}
  ],
  "memory_notes": ["供下一轮增量复审保留的本视角短期记忆要点"],
  "confidence": 0.0 到 1.0
}}

红线约束：
- 无明确问题时必须设 `needs_modification: false` 与 `findings: []`；
- 只有 `severity` 为 `critical` 或 `major` 且非纯格式问题时才允许 `blocking: true`；
- 必须引用真实存在的路径与行号，禁止猜测或编造文件名；
- 已存在于短期记忆 `raised_fingerprints` / `resolved_fingerprints` / `dismissed_fingerprints` 中且未在本轮变更重现的问题，严禁重复提交。"""


__all__ = ["ROLE_FOCUS", "reviewer_perspective_prompt"]
