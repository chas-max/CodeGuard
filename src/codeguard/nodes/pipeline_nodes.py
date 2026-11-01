"""CodeGuard 12 节点流水线实现 + 7 条件路由（四能力闭环核心）。

四能力 → 节点映射：
① 自动发现异常：machine_review（确定性静态门控）+ sandbox_execute（F2P/P2P/Traceback 捕获）
② 规划处理方案：director_plan（AST 定位 + 契约冻结）+ director_resolve（异常统筹路由）
③ 执行处理：write_draft（四阶段补丁编写）+ sandbox_execute（逐槽位 ReAct 自愈）
④ 检测处理结果：沙箱 F2P + P2P + 分支覆盖率三重门控，交付零退化 fix.patch

对原系统两处关键缺陷的修复（见 git 历史 director.py）：
- 修复①：路由信号改用无 reducer 的 open_issues（覆盖式），director_resolve 返回
  {"open_issues": []} 即真正清空；agent_issues 退化为 append-only 审计日志。
- 修复②：director_resolve_count >= max_resolve 在节点内硬编码强制 escalate，
  不再依赖 LLM 自觉。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from langgraph.graph import END
from langgraph.types import interrupt

from ..coding_agent.ast_analyzer import (
    extract_repo_symbols,
    find_repo_test_files,
    rank_suspect_files,
)
from ..coding_agent.models import StaticAuditVerdict
from ..coding_agent.patch_writer import read_relevant_code, synthesize_unified_diff
from ..coding_agent.sandbox_runner import run_sandbox_verification, verify_f2p_red
from ..coding_agent.static_guard import audit_patched_code
from ..prompts.heal import f2p_test_prompt, react_think_prompt
from ..prompts.plan import plan_prompt
from ..prompts.resolve import resolve_prompt
from ..prompts.supervise import supervise_prompt, supervise_verify_prompt
from ..prompts.understand import parse_issue_prompt, revision_classify_prompt, summarize_prompt
from ..prompts.write import outline_prompt, patch_code_prompt, reflect_prompt
from ..state import CodeGuardState
from ._common import artifacts_dir_for, call_llm_json, settle_costs

MAX_SUMMARIZE_HISTORY = 8  # 对话历史超过此长度触发摘要压缩


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _emit(ctx, job_id: str, event: str, data: dict | None = None):
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.emit_event(job_id, event, data or {})


def _progress(ctx, job_id: str, text: str, pct: int):
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(job_id, text, pct)


def _issue(agent: str, issue_type: str, detail: str) -> dict:
    return {"agent": agent, "issue_type": issue_type, "detail": detail, "timestamp": _now_iso()}


def _report_issue(agent: str, issue_type: str, detail: str) -> dict:
    """构造异常上报的 state 更新：agent_issues 追加审计 + open_issues 覆盖为待处理信号。"""
    issue = _issue(agent, issue_type, detail)
    return {"agent_issues": [issue], "open_issues": [issue]}


def _strip_code_fence(text: str) -> str:
    """剥掉 LLM 输出中的 ```python 围栏。"""
    s = (text or "").strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip() + "\n" if s.strip() and not s.endswith("\n") else s


def _repo_dir(state: CodeGuardState) -> Path:
    return Path(state.get("repo_dir") or ".")


# ══════════════════════════ ①② 前段：理解 → 规划 ══════════════════════════


def understand(state: CodeGuardState, ctx) -> dict:
    """故障解析：提取结构化上下文 + 完备度评估（四能力②的输入）。"""
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在解析故障报告并评估复现条件...", 8)

    history = state.get("conversation_history") or []
    update: dict[str, Any] = {}

    # 对话历史摘要压缩（控制 token，与原系统 _maybe_summarize 同思路）
    if len(history) >= MAX_SUMMARIZE_HISTORY and not state.get("conversation_summary"):
        try:
            hist_text = "\n".join(
                f"{m.get('role', 'user')}: {m.get('content', '')}" for m in history[:-2]
            )
            obj, costs = call_llm_json(ctx, "understand_summary", summarize_prompt(hist_text))
            raw = json.dumps(obj, ensure_ascii=False)
            update["conversation_summary"] = raw[:600]
            update.update(settle_costs(state, ctx, costs))
        except Exception:  # noqa: BLE001 — 摘要失败不阻塞主线
            pass

    # revision 模式：用户在修复过程中补充信息 / 修改诉求
    if state.get("chat_mode") == "revision" and state.get("user_revision_request"):
        try:
            obj, costs = call_llm_json(
                ctx, "understand", revision_classify_prompt(state["user_revision_request"]),
                temperature=0.1,
            )
            update.update(settle_costs(state, ctx, costs))
            if obj.get("intent") == "revise_scope":
                update["issue_context"] = {
                    **(state.get("issue_context") or {}),
                    "symptom": obj.get("detail", ""),
                    "completeness": "clear",
                }
                update.update({
                    "user_revision_request": None,
                    "_revision_target": "director_plan",
                    "chat_mode": "idle",
                    "status": "parsed",
                })
                return update
        except Exception:  # noqa: BLE001
            pass
        update["user_revision_request"] = None
        update["chat_mode"] = "idle"

    # 故障解析（含澄清问答上下文）
    repo = _repo_dir(state)
    from ..coding_agent.ast_analyzer import _iter_py_files

    file_tree = (
        [p.relative_to(repo).as_posix() for p in _iter_py_files(repo)] if repo.is_dir() else []
    )
    qa = state.get("clarification_qa") or []
    qa_text = "\n".join(f"问：{x.get('question', '')}\n答：{x.get('answer', '')}" for x in qa)
    issue_text = state.get("user_input", "")
    if qa_text:
        issue_text = f"{issue_text}\n\n## 澄清问答补充\n{qa_text}"
    if update.get("conversation_summary"):
        issue_text = f"{issue_text}\n\n## 历史摘要\n{update['conversation_summary']}"

    try:
        obj, costs = call_llm_json(ctx, "understand", parse_issue_prompt(issue_text, file_tree),
                                   temperature=0.2)
    except Exception as e:  # noqa: BLE001
        update.update(_report_issue("understand", "llm_error", str(e)))
        update["status"] = "parsing"
        return update
    update.update(settle_costs(state, ctx, costs))

    issue_context: dict = {
        "title": obj.get("title", ""),
        "symptom": obj.get("symptom", ""),
        "stack_trace": obj.get("stack_trace", ""),
        "suspected_modules": obj.get("suspected_modules") or [],
        "repro_steps": obj.get("repro_steps") or [],
        "env_hints": obj.get("env_hints", ""),
        "missing_info": obj.get("missing_info") or [],
        "completeness": obj.get("completeness", "partial"),
    }
    update.update({
        "issue_context": issue_context,
        "status": "parsed",
        "_revision_target": None,
    })
    update["conversation_history"] = [{
        "role": "assistant",
        "content": f"【故障解析】{issue_context['title']}：{issue_context['symptom'][:200]}",
        "timestamp": _now_iso(),
    }]
    _emit(ctx, job_id, "issue_parsed", {"title": issue_context["title"],
                                         "completeness": issue_context["completeness"]})
    return update


def route_after_understand(state: CodeGuardState, ctx) -> str:
    if state.get("open_issues"):
        return "director_resolve"
    if state.get("budget_exceeded"):
        return "escalate"
    target = state.get("_revision_target")
    if target in ("write_draft", "director_plan"):
        return target
    if state.get("clarification_round", 0) >= ctx.cfg.max_clarify:
        return "director_plan"  # 澄清封顶：带现有信息强制推进
    ic = state.get("issue_context") or {}
    if ic.get("completeness") == "clear":
        return "director_plan"
    return "clarify_ask"


def clarify_ask(state: CodeGuardState, ctx) -> dict:
    """HITL #1：澄清追问，补齐故障上下文。"""
    job_id = state.get("job_id", "")
    ic = state.get("issue_context") or {}
    missing = ic.get("missing_info") or ["请更具体地描述错误现象与复现步骤"]
    question = "为继续定位与修复该 Bug，请补充以下信息：\n" + "\n".join(
        f"{i}. {m}" for i, m in enumerate(missing, 1)
    )

    answer = interrupt({
        "type": "clarify",
        "question": question,
        "missing_info": missing,
        "round": state.get("clarification_round", 0),
    })

    if isinstance(answer, dict):
        answer = answer.get("answer") or answer.get("message") or json.dumps(answer, ensure_ascii=False)
    update: dict[str, Any] = {
        "clarification_qa": [{
            "question": question,
            "answer": str(answer),
            "timestamp": _now_iso(),
        }],
        "clarification_round": state.get("clarification_round", 0) + 1,
        "conversation_history": [{"role": "user", "content": str(answer), "timestamp": _now_iso()}],
        "chat_mode": "briefing",
        "status": "clarifying",
    }
    update["messages"] = [{"role": "assistant", "content": question},
                          {"role": "user", "content": str(answer)}]
    return update


def director_plan(state: CodeGuardState, ctx) -> dict:
    """架构规划（四能力②）：AST 调用图定位 Top-K 故障文件 + 冻结符号契约 + 修复策略。"""
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在扫描 AST 调用图，定位故障文件并冻结符号契约...", 25)
    repo = _repo_dir(state)
    ic = state.get("issue_context") or {}

    # 确定性定位（不依赖 LLM）：词法 + 符号命中 + 调用链传播
    locate_text = " ".join([
        state.get("user_input", ""), ic.get("symptom", ""), ic.get("stack_trace", ""),
        " ".join(ic.get("suspected_modules") or []),
    ])
    try:
        located = rank_suspect_files(repo, locate_text, top_k=ctx.cfg.topk_files)
    except Exception as e:  # noqa: BLE001
        update = _report_issue("director_plan", "tool_error", f"AST 定位失败: {e}")
        update["status"] = "planning"
        return update

    # 符号契约冻结（同构角色圣经）
    contracts = extract_repo_symbols(repo)
    symbol_bible = {sid: c.model_dump() for sid, c in contracts.items()}
    frozen_text = "\n".join(
        f"{s.file_path} :: {s.symbol_name}({', '.join(s.arg_names)}) -> {s.return_annotation or 'Any'}"
        for s in contracts.values()
        if s.frozen_signature and s.file_path in located
    ) or "（定位文件中无公共符号）"

    file_excerpts = read_relevant_code(repo, located)
    existing_tests = find_repo_test_files(repo)

    try:
        obj, costs = call_llm_json(
            ctx, "director_plan", plan_prompt(ic, located, file_excerpts, frozen_text, existing_tests),
            temperature=0.3,
        )
    except Exception as e:  # noqa: BLE001
        update = settle_costs(state, ctx, [])
        update.update(_report_issue("director_plan", "llm_error", str(e)))
        update["status"] = "planning"
        return update
    update = settle_costs(state, ctx, costs)

    # 修复策略 → 补丁槽位（约束 target_file 必须在定位列表内）
    strategy = [
        s for s in (obj.get("repair_strategy") or [])
        if s.get("target_file") in located
    ]
    if not strategy:  # LLM 未给出有效策略：按定位文件兜底建槽
        strategy = [{
            "target_file": f,
            "test_file": f"test_repro_{Path(f).stem}.py",
            "hypothesis": f"{f} 中存在与故障相关的缺陷",
            "change_sketch": "根据故障上下文最小化修复",
        } for f in located[:2]]

    patch_units = []
    for i, s in enumerate(strategy, 1):
        patch_units.append({
            "unit_id": f"U{i:03d}",
            "target_file": s["target_file"],
            "test_file": s.get("test_file") or f"test_repro_{Path(s['target_file']).stem}.py",
            "issue_hypothesis": s.get("hypothesis", ""),
            "change_sketch": s.get("change_sketch", ""),
            "status": "pending",
            "attempts": 0,
        })

    update.update({
        "plan": {"plan_summary": obj.get("plan_summary", ""), "repair_strategy": strategy},
        "located_files": located,
        "symbol_bible": symbol_bible,
        "patch_units": patch_units,
        "p2p_tests": existing_tests,  # 仓库既有测试作为 P2P 回归门控
        "status": "planning",
        "production_log": [{"event": "director_plan", "located": located, "timestamp": _now_iso()}],
    })
    _emit(ctx, job_id, "plan_ready", {"located_files": located, "units": len(patch_units)})
    return update


# ══════════════════════════ ③ 中段：补丁编写 + 静态门控 ══════════════════════════


def _pick_feedback(state: CodeGuardState) -> str:
    """多源反馈优先级：人工 > 静态门控 > Tech Lead（复用原系统 _pick_feedback 思想）。"""
    hd = state.get("human_decision") or {}
    if not hd.get("approved", True) and hd.get("feedback"):
        return f"人工审核驳回意见：{hd['feedback']}"
    sv = state.get("static_verdict") or {}
    if not sv.get("passed", True) and sv.get("feedback"):
        return f"静态门控拦截：{sv['feedback']}"
    if state.get("director_feedback"):
        return f"Tech Lead 审查意见：{state['director_feedback']}"
    return ""


def write_draft(state: CodeGuardState, ctx) -> dict:
    """四阶段补丁编写（四能力③）：Search → Plan → Execute → Reflect。"""
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在检索代码、生成修复大纲并编写补丁...", 40)
    repo = _repo_dir(state)
    ic = state.get("issue_context") or {}
    bible = state.get("symbol_bible") or {}
    units = [dict(u) for u in state.get("patch_units") or []]
    feedback = _pick_feedback(state)
    costs: list[dict] = []
    update: dict[str, Any] = {}

    # 幂等：仅重写 pending / failed / 未写过代码的槽位（已 passed 的跳过）
    todo = [u for u in units if u.get("status") in ("pending", "failed") or not u.get("patched_code")]
    if not todo:
        update["status"] = "writing"
        return update
    targets = sorted({u["target_file"] for u in todo})

    # Stage 1 Search：按需裁剪上下文
    excerpts = read_relevant_code(repo, targets)
    if feedback:
        excerpts += f"\n\n## 需要修正的反馈\n{feedback}"

    # Stage 2 Plan：修复大纲
    outline = state.get("patch_outline")
    if not outline or feedback:
        try:
            obj, c = call_llm_json(ctx, "write_outline",
                                   outline_prompt(ic, state.get("plan", {}).get("repair_strategy", [])),
                                   temperature=0.3)
            outline = obj.get("outline", "")
            costs.extend(c)
        except Exception as e:  # noqa: BLE001
            outline = outline or "（大纲生成失败，按根因假设直接修复）"
            update.update(_report_issue("write_draft", "llm_error", f"大纲阶段: {e}"))

    # Stage 3 Execute：逐槽位生成修复代码
    for u in todo:
        fpath = u["target_file"]
        fobj = repo / fpath
        file_code = fobj.read_text(encoding="utf-8") if fobj.is_file() else ""
        contracts_text = "\n".join(
            f"{s.get('symbol_name')}({', '.join(s.get('arg_names', []))})"
            for s in bible.values()
            if s.get("file_path") == fpath and s.get("frozen_signature")
        ) or "（该文件无冻结公共符号）"
        try:
            obj, c = call_llm_json(ctx, "write", patch_code_prompt(
                file_code, u.get("issue_hypothesis", ""),
                (u.get("change_sketch", "") + f"\n大纲约束：{outline}" if outline else u.get("change_sketch", "")),
                contracts_text, ic,
            ), temperature=0.2)
            costs.extend(c)
            patched = _strip_code_fence(obj.get("patched_code", ""))
            if not patched.strip():
                u["status"] = "failed"
                u["last_traceback"] = "LLM 返回空补丁"
                continue
            u["patched_code"] = patched
            u["status"] = "pending"  # 待静态门控
            u["git_diff"] = synthesize_unified_diff(repo, {fpath: patched})
        except Exception as e:  # noqa: BLE001
            update.update(_report_issue("write_draft", "llm_error", f"{fpath}: {e}"))
            u["status"] = "failed"
            u["last_traceback"] = str(e)[:500]
            continue

    # Stage 4 Reflect：跨文件契约一致性自省（一次修订机会）
    patched_files = {u["target_file"]: u.get("patched_code", "") for u in todo if u.get("patched_code")}
    if patched_files and not state.get("open_issues"):
        try:
            digest = "\n".join(
                f"{s.get('file_path')} :: {s.get('symbol_name')}({', '.join(s.get('arg_names', []))})"
                for s in bible.values() if s.get("frozen_signature")
            )
            obj, c = call_llm_json(ctx, "write_reflect",
                                   reflect_prompt(patched_files, digest, ic), temperature=0.1)
            costs.extend(c)
            if not obj.get("consistent", True):
                issues = obj.get("issues") or []
                update["director_feedback"] = "；".join(issues)[:500]
                update["patch_outline"] = None  # 反馈后大纲重建
            else:
                update["director_feedback"] = ""
        except Exception:  # noqa: BLE001 — 自省失败不阻塞
            pass

    # 成本统一结算一次（budget_events 为 append reducer，避免重复计入）
    if costs:
        update.update(settle_costs(state, ctx, costs))
    update.update({
        "patch_units": units,
        "patch_outline": outline if not update.get("director_feedback") else None,
        "status": "writing",
        "human_decision": None,  # 新补丁需重新人工审核
    })
    _emit(ctx, job_id, "patches_ready", {"units": [u["unit_id"] for u in todo]})
    return update


def machine_review(state: CodeGuardState, ctx) -> dict:
    """静态门控（四能力①）：AST 语法 + 高危调用 + 冻结签名契约，毫秒级拦截。

    确定性审计，不调 LLM —— 这是"左移测试"：把明显废品挡在沙箱之前。
    """
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在执行 AST 语法与接口契约静态审计...", 50)
    bible = state.get("symbol_bible") or {}
    units = [dict(u) for u in state.get("patch_units") or []]

    unit_reports: dict[str, dict] = {}
    all_issues: list[str] = []
    passed_all = True
    for u in units:
        code = u.get("patched_code", "")
        if not code.strip():
            passed_all = False
            unit_reports[u["unit_id"]] = {"passed": False, "category": "empty_patch",
                                          "issues": ["补丁代码为空"]}
            u["status"] = "failed"
            continue
        verdict: StaticAuditVerdict = audit_patched_code(u["target_file"], code, bible)
        unit_reports[u["unit_id"]] = verdict.model_dump()
        if not verdict.passed:
            passed_all = False
            u["status"] = "failed"
            u["last_traceback"] = verdict.feedback
            all_issues.extend(verdict.issues)
        else:
            u["status"] = "pending"  # 门控通过，等待沙箱

    severity_rank = {"none": 0, "minor": 1, "major": 2, "critical": 3}
    worst = max(
        (rep.get("severity", "none") for rep in unit_reports.values()),
        key=lambda s: severity_rank.get(s, 0),
        default="none",
    )

    update: dict[str, Any] = {
        "static_verdict": {
            "passed": passed_all,
            "category": (unit_reports and next(
                (r["category"] for r in unit_reports.values() if not r.get("passed")), "none")),
            "severity": worst,
            "issues": all_issues,
            "unit_reports": unit_reports,
            "feedback": "；".join(all_issues)[:800] if all_issues else "静态审计通过",
        },
        "patch_units": units,
        "status": "reviewing",
        "production_log": [{"event": "machine_review", "passed": passed_all,
                            "timestamp": _now_iso()}],
    }
    if not passed_all:
        update["machine_revision_count"] = state.get("machine_revision_count", 0) + 1
        # 全部槽位代码为空属于写作环节故障，交统筹节点而非无限打回
        if all(not u.get("patched_code", "").strip() for u in units):
            update.update(_report_issue("machine_review", "quality", "所有补丁槽位代码为空，写作环节故障"))
    _emit(ctx, job_id, "static_gate_done", {"passed": passed_all, "issues": all_issues[:5]})
    return update


def route_after_machine(state: CodeGuardState, ctx) -> str:
    if state.get("open_issues"):
        return "director_resolve"
    if state.get("budget_exceeded"):
        return "escalate"
    sv = state.get("static_verdict") or {}
    if sv.get("passed"):
        return "director_supervise"
    if state.get("machine_revision_count", 0) >= ctx.cfg.max_machine:
        return "director_supervise"  # 封顶放行，把关权交人工
    return "write_draft"


# ══════════════════════════ ④⑤ 后段：监督 → 人工 → 锚定 → 沙箱 ══════════════════════════


def _full_diff(state: CodeGuardState) -> str:
    repo = _repo_dir(state)
    patches = {u["target_file"]: u["patched_code"] for u in state.get("patch_units") or []
               if u.get("patched_code")}
    return synthesize_unified_diff(repo, patches)


def director_supervise(state: CodeGuardState, ctx) -> dict:
    """Tech Lead 两阶段自省：初始判断 + 复核（把关边界条件）。"""
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "Tech Lead 正在审查补丁边界条件并自省复核...", 60)
    ic = state.get("issue_context") or {}
    diff_text = _full_diff(state)
    all_costs: list[dict] = []
    update: dict[str, Any] = {}

    try:
        obj, c1 = call_llm_json(ctx, "review", supervise_prompt(diff_text, ic), temperature=0.3)
        all_costs.extend(c1)
        initial = {"decision": obj.get("decision", "revise"), "feedback": obj.get("feedback", "")}
        obj2, c2 = call_llm_json(ctx, "review_verify",
                                 supervise_verify_prompt(diff_text, ic, initial), temperature=0.2)
        all_costs.extend(c2)
    except Exception as e:  # noqa: BLE001
        update.update(settle_costs(state, ctx, all_costs))
        update.update(_report_issue("director_supervise", "llm_error", str(e)))
        # 监督失败不阻塞：放行至人工审核
        update.update({"director_decision": "approve", "director_feedback": "（监督 LLM 失败，放行至人工审核）",
                       "status": "awaiting_human"})
        return update

    if not obj2.get("confirmed", True):
        decision = obj2.get("revised_decision") or initial["decision"]
        feedback = obj2.get("revised_feedback") or initial["feedback"]
    else:
        decision = initial["decision"]
        feedback = initial["feedback"]

    update.update(settle_costs(state, ctx, all_costs))
    update.update({
        "director_decision": decision,
        "director_feedback": feedback,
        "status": "supervising",
        "production_log": [{"event": "director_supervise", "decision": decision,
                            "timestamp": _now_iso()}],
    })
    _emit(ctx, job_id, "supervise_done", {"decision": decision, "feedback": feedback[:300]})
    return update


def route_after_supervise(state: CodeGuardState, ctx) -> str:
    if state.get("open_issues"):
        return "director_resolve"
    if state.get("budget_exceeded"):
        return "escalate"
    if state.get("director_decision") == "approve":
        return "human_review"
    total = state.get("machine_revision_count", 0) + state.get("human_revision_count", 0)
    if total >= (ctx.cfg.max_machine + ctx.cfg.max_human):
        return "human_review"  # 打回封顶：强制交人工终审
    return "write_draft"


def human_review(state: CodeGuardState, ctx) -> dict:
    """HITL #2：人工审查修改方案与 Diff 预览。"""
    decision = interrupt({
        "type": "human_review",
        "diff": _full_diff(state),
        "static_verdict": state.get("static_verdict"),
        "director_feedback": state.get("director_feedback", ""),
        "issue_title": (state.get("issue_context") or {}).get("title", ""),
    })
    if isinstance(decision, dict):
        approved = bool(decision.get("approved"))
        feedback = decision.get("feedback", "")
    else:
        approved = str(decision).strip().lower() in ("y", "yes", "approve", "true", "1")
        feedback = ""
    update: dict[str, Any] = {"human_decision": {"approved": approved, "feedback": feedback}}
    if not approved:
        update["human_revision_count"] = state.get("human_revision_count", 0) + 1
    update["status"] = "awaiting_human"
    return update


def route_after_human(state: CodeGuardState, ctx) -> str:
    if state.get("budget_exceeded"):
        return "escalate"
    d = state.get("human_decision") or {}
    if d.get("approved"):
        return "build_anchors"
    if state.get("human_revision_count", 0) >= ctx.cfg.max_human:
        return "escalate"
    return "write_draft"


def build_anchors(state: CodeGuardState, ctx) -> dict:
    """TDD 复现测试锚定（四能力④ Ground Truth）：生成 F2P 测试并验证原仓库必败（Red 态）。"""
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在生成 Fail-to-Pass 复现测试并验证 Red 锚定态...", 70)
    repo = _repo_dir(state)
    ic = state.get("issue_context") or {}
    units = state.get("patch_units") or []
    primary = units[0]["target_file"] if units else ""
    file_code = ""
    if primary:
        fobj = repo / primary
        if fobj.is_file():
            file_code = fobj.read_text(encoding="utf-8")

    update: dict[str, Any] = {}
    costs: list[dict] = []
    for attempt in (1, 2):  # Red 验证失败时带反馈重试一次
        try:
            obj, c = call_llm_json(ctx, "f2p_writer", f2p_test_prompt(
                ic, file_code, _existing_test_style(repo)), temperature=0.2)
            costs.extend(c)
        except Exception as e:  # noqa: BLE001
            update.update(settle_costs(state, ctx, costs))
            update.update(_report_issue("build_anchors", "llm_error", str(e)))
            update["status"] = "anchoring"
            return update

        test_file = (obj.get("test_file") or "test_repro_issue.py").replace("\\", "/").lstrip("/")
        test_code = _strip_code_fence(obj.get("test_code", ""))
        node_ids = obj.get("node_ids") or [f"{test_file}::test_bug_repro"]

        if not test_code.strip():
            if attempt == 1:
                continue
            update.update(settle_costs(state, ctx, costs))
            update.update(_report_issue("build_anchors", "quality", "F2P 测试生成为空"))
            update["status"] = "anchoring"
            return update

        # Red 态验证：未打修复补丁的仓库上，该测试必须失败
        is_red = verify_f2p_red(repo, node_ids, patches={test_file: test_code},
                                timeout_sec=ctx.cfg.sandbox_timeout_sec)
        if is_red:
            break
        if attempt == 2:
            update.update(settle_costs(state, ctx, costs))
            update.update(_report_issue(
                "build_anchors", "quality",
                f"复现测试无法稳定触发 Bug（Red 验证失败）: {node_ids}",
            ))
            update["status"] = "anchoring"
            return update
        ic = {**ic, "symptom": (ic.get("symptom", "") +
              "\n（注意：上一版复现测试未能触发 Bug，请让断言更直接命中故障行为）")}

    update.update(settle_costs(state, ctx, costs))
    update.update({
        "f2p_tests": node_ids,
        "f2p_test_file": test_file,
        "f2p_test_code": test_code,
        "status": "anchoring",
        "production_log": [{"event": "build_anchors", "f2p": node_ids, "timestamp": _now_iso()}],
    })
    _emit(ctx, job_id, "anchors_ready", {"f2p_tests": node_ids, "red_verified": True})
    return update


def _existing_test_style(repo: Path) -> str:
    tests = find_repo_test_files(repo)
    if not tests:
        return ""
    excerpt = read_relevant_code(repo, tests[:1], max_chars_per_file=1500)
    return excerpt


def confirm_anchors(state: CodeGuardState, ctx) -> dict:
    """HITL #3：确认复现测试在原仓库稳定触发 Bug（Red 锚定态可信）。"""
    decision = interrupt({
        "type": "confirm_anchors",
        "f2p_tests": state.get("f2p_tests") or [],
        "f2p_test_file": state.get("f2p_test_file", ""),
        "f2p_test_code": state.get("f2p_test_code", ""),
    })
    if isinstance(decision, dict):
        approved = bool(decision.get("approved"))
        feedback = decision.get("feedback", "")
    else:
        approved = str(decision).strip().lower() in ("y", "yes", "approve", "true", "1")
        feedback = ""
    update: dict[str, Any] = {"anchor_decision": {"approved": approved, "feedback": feedback},
                              "status": "confirming_anchors"}
    if not approved:
        update["anchor_round"] = state.get("anchor_round", 0) + 1
        # 驳回意见注入下一轮测试生成（修复原系统"驳回意见被丢弃"的缺陷）
        if feedback:
            update["issue_context"] = {
                **(state.get("issue_context") or {}),
                "symptom": (state.get("issue_context") or {}).get("symptom", "")
                + f"\n（人工对复现测试的驳回意见：{feedback}）",
            }
    return update


def route_after_anchors(state: CodeGuardState, ctx) -> str:
    if state.get("open_issues"):
        return "director_resolve"
    d = state.get("anchor_decision") or {}
    if d.get("approved"):
        return "sandbox_execute"
    if state.get("anchor_round", 0) >= ctx.cfg.max_anchor:
        return "director_resolve"  # 锚定反复失败交统筹决策（降级/放弃）
    return "build_anchors"


def sandbox_execute(state: CodeGuardState, ctx) -> dict:
    """隔离沙箱 ReAct 自愈执行（四能力③④）：Act → Observe → Think → Retry。

    同构复用原 _react_single_shot 闭环：
    - Act：沙箱写补丁 + 跑 F2P/P2P pytest
    - Observe：解析双门控结果与覆盖率（文件级确定性检查）
    - Think：produce_react 分析裁剪后的 Traceback，定向重写该槽位
    - Retry：每槽位 ≤ per_unit_attempts 次；轮次级 production_attempt_count 封顶
    """
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "沙箱自愈执行中：应用补丁并运行三重门控...", 78)
    repo = _repo_dir(state)
    cfg = ctx.cfg
    units = [dict(u) for u in state.get("patch_units") or []]
    f2p = state.get("f2p_tests") or []
    p2p = state.get("p2p_tests") or []
    f2p_file = state.get("f2p_test_file", "")
    f2p_code = state.get("f2p_test_code", "")

    # 轮次级幂等：passed 跳过；failed 槽位重置进入新一轮自愈
    for u in units:
        if u.get("status") == "passed":
            continue
        if u.get("status") == "failed":
            u["status"] = "pending"
            u["attempts"] = 0

    cov_target: str | None = "."
    if getattr(ctx, "_coverage_disabled", False) or cfg.coverage_min_pct <= 0:
        cov_target = None

    update: dict[str, Any] = {}
    last_result = None
    for idx, u in enumerate(units, 1):
        if u.get("status") == "passed":
            continue
        if not u.get("patched_code", "").strip():
            u["status"] = "failed"
            continue

        # ── ReAct 循环（单槽位） ──
        while True:
            patches = {u["target_file"]: u["patched_code"]}
            if f2p_file and f2p_code:
                patches[f2p_file] = f2p_code
            try:
                res = run_sandbox_verification(
                    repo, patches, f2p, p2p,
                    timeout_sec=cfg.sandbox_timeout_sec, cov_target=cov_target,
                )
            except Exception as e:  # noqa: BLE001 — 沙箱环境异常交统筹
                update.update(_report_issue("sandbox_execute", "tool_error",
                                            f"沙箱环境异常: {e}"))
                update["patch_units"] = units
                update["status"] = "executing"
                return update

            u["attempts"] = (u.get("attempts", 0)) + 1
            last_result = res
            coverage_ok = (cov_target is None or
                           res.branch_coverage_pct >= cfg.coverage_min_pct)

            if res.resolved and coverage_ok:  # ── Observe: 通过
                u["status"] = "passed"
                u["f2p_passed"] = True
                u["p2p_passed"] = True
                u["last_traceback"] = ""
                break

            u["f2p_passed"] = res.f2p_passed
            u["p2p_passed"] = res.p2p_passed
            u["last_traceback"] = res.error_summary[:1500]
            u["status"] = "failed"

            if u["attempts"] > cfg.per_unit_attempts:  # 槽位重试封顶
                break

            # ── Think：Traceback 驱动定向重写 ──
            _progress(ctx, job_id,
                      f"槽位 {u['unit_id']} 第 {u['attempts']} 次自愈：分析 Traceback 并重写...", 80)
            try:
                obj, _ = call_llm_json(ctx, "produce_react", react_think_prompt(
                    u, res.error_summary, u["patched_code"]), temperature=0.2)
                if obj.get("should_skip"):
                    u["status"] = "skipped"
                    break
                new_code = _strip_code_fence(obj.get("patched_code", ""))
                if new_code.strip():
                    u["patched_code"] = new_code
            except Exception as e:  # noqa: BLE001 — 自愈 LLM 失败：终止该槽位本轮尝试
                update.update(_report_issue("sandbox_execute", "llm_error",
                                            f"槽位 {u['unit_id']} 自愈失败: {e}"))
                update["patch_units"] = units
                update["status"] = "executing"
                return update

        _emit(ctx, job_id, "unit_progress", {
            "unit_id": u["unit_id"], "target_file": u["target_file"],
            "status": u["status"], "attempts": u.get("attempts", 0),
        })

    # ── 轮次汇总：全部通过则合成最终交付物 ──
    all_passed = bool(units) and all(u.get("status") in ("passed", "skipped") for u in units)
    update["patch_units"] = units

    if all_passed:
        final_patches = {u["target_file"]: u["patched_code"] for u in units
                         if u.get("status") == "passed" and u.get("patched_code")}
        if f2p_file and f2p_code:
            final_patches[f2p_file] = f2p_code  # 单测生成：复现测试一并交付
        art_dir = artifacts_dir_for(state, ctx)
        diff_text = synthesize_unified_diff(repo, final_patches)
        patch_path = art_dir / "fix.patch"
        patch_path.write_text(diff_text, encoding="utf-8")

        cov_pct = last_result.branch_coverage_pct if last_result else 0.0
        cov_path = art_dir / "coverage_report.md"
        cov_path.write_text(
            f"# 覆盖率报告\n\n- 分支覆盖率: {cov_pct:.1f}%\n"
            f"- F2P 复现测试: {json.dumps(f2p, ensure_ascii=False)}\n"
            f"- P2P 回归测试: {json.dumps(p2p, ensure_ascii=False)}\n"
            f"- 门槛: {cfg.coverage_min_pct}%\n",
            encoding="utf-8",
        )
        (art_dir / "sandbox_log.md").write_text(
            "\n\n".join(
                f"## {u['unit_id']} ({u['target_file']})\n"
                f"- status: {u['status']}\n- attempts: {u.get('attempts', 0)}\n"
                f"- last_traceback: {(u.get('last_traceback') or '（无）')[:800]}"
                for u in units
            ),
            encoding="utf-8",
        )
        update.update({
            "sandbox_result": (last_result.model_dump() if last_result else {}),
            "patch_path": str(patch_path),
            "coverage_report_path": str(cov_path),
            "status": "done",
            "production_log": [{"event": "sandbox_execute", "result": "resolved",
                                "timestamp": _now_iso()}],
        })
        _progress(ctx, job_id, f"修复完成！补丁已交付：{patch_path}", 100)
        _emit(ctx, job_id, "patch_done", {"patch_path": str(patch_path),
                                          "coverage_pct": cov_pct})
        return update

    attempt = state.get("production_attempt_count", 0) + 1
    update["production_attempt_count"] = attempt
    if attempt >= cfg.max_prod:
        update["status"] = "failed"
        update["error"] = "沙箱多轮自愈后仍有槽位未通过（F2P/P2P 门控）"
        update.update(_report_issue("sandbox_execute", "quality", update["error"]))
    else:
        update["status"] = "executing"
    _emit(ctx, job_id, "unit_progress", {"round": attempt, "all_passed": all_passed})
    return update


def route_after_execute(state: CodeGuardState, ctx) -> str:
    if state.get("open_issues"):
        return "director_resolve"
    if state.get("budget_exceeded"):
        return "escalate"
    if state.get("status") == "done":
        return END
    if state.get("production_attempt_count", 0) >= ctx.cfg.max_prod:
        return "escalate"
    return "sandbox_execute"


# ══════════════════════════ 异常统筹 + 终止 ══════════════════════════


def director_resolve(state: CodeGuardState, ctx) -> dict:
    """异常统筹（四能力②的闭环保障）：分析 open_issues，路由自愈策略。

    修复①：返回 open_issues=[] 真正清空路由信号（原系统 append reducer 清空无效）。
    修复②：resolve_count >= max_resolve 硬编码强制 escalate（原系统仅拼进提示词）。
    """
    job_id = state.get("job_id", "")
    _progress(ctx, job_id, "正在分析异常并协调自愈策略...", 55)
    issues = state.get("open_issues") or []
    resolve_count = state.get("director_resolve_count", 0)

    # 修复②：硬封顶
    if resolve_count >= ctx.cfg.max_resolve:
        return _finalize_escalation(state, resolve_count, "异常统筹次数达到硬上限")

    if not issues:
        return {"open_issues": [],
                "director_resolution": {"action": "skip", "target_node": "", "strategy": "无待处理异常"}}

    try:
        prompt = resolve_prompt(issues, state.get("status", ""), resolve_count, ctx.cfg.max_resolve)
        obj, costs = call_llm_json(ctx, "director_resolve", prompt, temperature=0.3)
        update = settle_costs(state, ctx, costs)
    except Exception:  # noqa: BLE001 — 统筹自身失败：直接终止
        update = {}
        obj = {"action": "escalate", "target_node": "", "strategy": "",
               "escalate_reason": "异常统筹模块 LLM 调用失败，无法继续"}

    action = obj.get("action", "escalate")
    target_node = obj.get("target_node", "")
    strategy = obj.get("strategy", "")

    # 降级策略落地：放弃覆盖率门控（本次会话有效）
    if action == "downgrade" and "覆盖率" in strategy:
        ctx._coverage_disabled = True

    resolution = {
        "action": action,
        "target_node": target_node,
        "strategy": strategy,
        "escalate_reason": obj.get("escalate_reason", ""),
        "resolved_issues": obj.get("resolved_issues", []),
    }
    update.update({
        "director_resolution": resolution,
        "director_resolve_count": resolve_count + 1,
        "open_issues": [],  # 修复①：真正清空
        "production_log": [{"event": "director_resolve", "action": action,
                            "strategy": strategy, "timestamp": _now_iso()}],
    })
    if action == "escalate":
        update.update({
            "status": "failed",
            "error": obj.get("escalate_reason") or "异常统筹判断无法继续推进",
        })
    return update


def _finalize_escalation(state: CodeGuardState, resolve_count: int, reason: str) -> dict:
    return {
        "director_resolution": {"action": "escalate", "target_node": "", "strategy": "",
                                "escalate_reason": reason},
        "director_resolve_count": resolve_count + 1,
        "open_issues": [],
        "status": "failed",
        "error": reason,
        "production_log": [{"event": "director_resolve", "action": "escalate",
                            "timestamp": _now_iso()}],
    }


def route_after_resolve(state: CodeGuardState, ctx) -> str:
    """统筹后的确定性路由：retry/downgrade → target_node；skip → 按状态续跑；escalate → 终止。"""
    resolution = state.get("director_resolution") or {}
    action = resolution.get("action", "escalate")
    if action == "escalate" or state.get("status") == "failed":
        return "escalate"

    target = resolution.get("target_node", "")
    valid_nodes = {
        "write_draft", "director_plan", "machine_review", "director_supervise",
        "sandbox_execute", "build_anchors",
    }
    if action in ("retry", "downgrade") and target in valid_nodes:
        return target

    status_map = {
        "planning": "write_draft",
        "writing": "machine_review",
        "reviewing": "director_supervise",
        "supervising": "human_review",
        "anchoring": "build_anchors",
        "executing": "sandbox_execute",
    }
    return status_map.get(state.get("status", ""), "escalate")


def escalate(state: CodeGuardState, ctx) -> dict:
    """确定性终止：保留已有产物（fix.patch 若已生成），记录终止原因。"""
    return {
        "status": "failed",
        "error": state.get("error") or "流程被异常统筹终止",
        "production_log": [{"event": "escalate", "timestamp": _now_iso()}],
    }
