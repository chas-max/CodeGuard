"""finding 复核提示词：只判断“这条 finding 是不是真的问题”，不产出整体结论。"""
from __future__ import annotations

import json

from .shared import UNTRUSTED_NOTICE, wrap_untrusted


def finding_verifier_prompt(
    *,
    finding: dict,
    repo_context: str,
    diff_text: str,
    tools_block: str = "",
) -> str:
    tools_section = f"\n{tools_block}" if tools_block else ""
    return f"""你是 finding 复核员。你的唯一任务是判断下面这条 finding 是否成立，不要寻找新问题，
也不要给出整体审查结论。

{UNTRUSTED_NOTICE}

## 待复核 finding
{json.dumps(finding, ensure_ascii=False, indent=2)[:4000]}

## 相关代码
{wrap_untrusted("repo_context", repo_context, max_chars=20000)}

## 当前 Diff
{wrap_untrusted("diff", diff_text, max_chars=20000)}

## 判定标准
- 只有能指出「具体路径:行号 + 代码事实」并说明后果时，才判 keep；
- 如果 finding 与代码事实不符、把确定性规则已覆盖的问题又说一遍、或者只是风格偏好，判 drop；
- 不确定时判 keep，并在 reason 里写清缺什么证据。
{tools_section}
## 输出要求
只输出 JSON：
{{"verdict": "keep | drop", "reason": "一句话理由", "counter_evidence": "判 drop 时给出反证（路径:行 + 代码）"}}"""
