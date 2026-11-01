"""CodeGuard-Agent 统一入口：Gradio Web UI（默认）/ CLI（--cli）。

用法：
    python codeguard_app.py                                    # Gradio UI，端口 7861
    python codeguard_app.py --cli --repo <仓库路径> --issue "bug 描述"
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from codeguard.config import load_config  # noqa: E402
from codeguard.runtime import CodeGuardRuntime  # noqa: E402


# ══════════════════════════ CLI 模式 ══════════════════════════


def _render_interrupt(payload: dict) -> str:
    t = payload.get("type")
    if t == "clarify":
        return f"[需要澄清]\n{payload.get('question', '')}"
    if t == "human_review":
        return (
            "[人工审查：修复 Diff 预览]\n"
            f"问题：{payload.get('issue_title', '')}\n"
            f"Tech Lead 意见：{payload.get('director_feedback', '') or '（无）'}\n"
            f"静态门控：{'通过' if (payload.get('static_verdict') or {}).get('passed') else '未通过'}\n"
            "────────── Diff ──────────\n"
            f"{payload.get('diff', '') or '（空）'}"
        )
    if t == "confirm_anchors":
        return (
            "[确认复现测试锚点（Red 态）]\n"
            f"F2P 测试：{payload.get('f2p_tests', [])}\n"
            "────────── 测试代码 ──────────\n"
            f"{payload.get('f2p_test_code', '')}"
        )
    return f"[中断] {payload}"


def run_cli(rt: CodeGuardRuntime, repo: str, issue: str):
    job_id = uuid.uuid4().hex[:12]
    print(f"== CodeGuard 任务启动 job={job_id} repo={repo}")
    snap = rt.start(issue, job_id, repo)
    while True:
        if snap.get("done"):
            values = snap.get("values", {})
            if values.get("status") == "done":
                print("\n✅ 修复完成")
                print(f"   补丁: {values.get('patch_path')}")
                print(f"   覆盖率报告: {values.get('coverage_report_path')}")
                print(f"   覆盖率: {((values.get('sandbox_result') or {}).get('branch_coverage_pct', 0)):.1f}%")
            else:
                print(f"\n❌ 任务失败: {values.get('error')}")
                if values.get("agent_issues"):
                    print("   异常审计:")
                    for a in values["agent_issues"]:
                        print(f"     - [{a['agent']}] {a['issue_type']}: {a['detail'][:120]}")
            return
        payload = snap.get("interrupt") or {}
        print("\n" + _render_interrupt(payload))
        if payload.get("type") == "clarify":
            answer = input("你的回答 > ").strip()
            snap = rt.resume(job_id, {"answer": answer})
        else:
            choice = input("批准? (y=批准 / n=驳回 / 直接回车=批准) > ").strip().lower()
            approved = choice != "n"
            feedback = ""
            if not approved:
                feedback = input("驳回意见 > ").strip()
            snap = rt.resume(job_id, {"approved": approved, "feedback": feedback})


# ══════════════════════════ Gradio 模式 ══════════════════════════


def build_ui(rt: CodeGuardRuntime):
    import gradio as gr

    state = gr.State({"job_id": None})

    with gr.Blocks(title="CodeGuard-Agent") as demo:
        gr.Markdown("# 🛡️ CodeGuard-Agent — Repo 自动化 Bug 修复与单测生成")
        gr.Markdown(
            "四能力闭环：静态门控+沙箱测试**发现异常** → AST定位+异常统筹**规划方案** → "
            "四阶段补丁+沙箱ReAct**执行处理** → F2P/P2P+覆盖率**检测结果**"
        )

        with gr.Row():
            inp_repo = gr.Textbox(label="目标仓库根目录（绝对路径）", placeholder=r"D:\path\to\your\repo")
        inp_issue = gr.Textbox(
            label="Bug 描述（GitHub Issue / 报错堆栈）", lines=4,
            placeholder="例如：divide_safe(10, 0) 抛 ZeroDivisionError，期望返回 0.0",
        )
        btn_start = gr.Button("🚀 启动修复", variant="primary")

        md_status = gr.Markdown("待命")
        md_interrupt = gr.Markdown("")
        with gr.Row():
            txt_answer = gr.Textbox(label="回答 / 驳回意见", lines=2)
        with gr.Row():
            btn_answer = gr.Button("💬 发送回答（澄清中）")
            btn_approve = gr.Button("✅ 批准")
            btn_reject = gr.Button("❌ 驳回")
        md_result = gr.Markdown("")

        def _fmt_snapshot(snap: dict) -> tuple[str, str, str]:
            if not snap:
                return "任务不存在", "", ""
            prog = snap.get("progress") or {}
            status_md = f"**{prog.get('status_text', '')}**（{prog.get('percentage', 0)}%）"
            interrupt_md = _render_interrupt_md(snap.get("interrupt") or {})
            values = snap.get("values") or {}
            if snap.get("done") and values.get("status") == "done":
                sb = values.get("sandbox_result") or {}
                interrupt_md = ""
                status_md = f"### ✅ 修复完成\n- 补丁: `{values.get('patch_path')}`\n- 分支覆盖率: {sb.get('branch_coverage_pct', 0):.1f}%"
            elif snap.get("done"):
                issues = "\n".join(
                    f"- [{a.get('agent')}] {a.get('issue_type')}: {a.get('detail', '')[:150]}"
                    for a in (values.get("agent_issues") or [])
                )
                status_md = f"### ❌ 失败\n{values.get('error', '')}\n{issues}"
                interrupt_md = ""
            return status_md, interrupt_md, status_md

        def _render_interrupt_md(payload: dict) -> str:
            if not payload:
                return ""
            t = payload.get("type")
            if t == "clarify":
                return f"### 💬 需要澄清\n{payload.get('question', '')}"
            if t == "human_review":
                return (
                    "### 👀 人工审查：修复 Diff\n"
                    f"**问题**：{payload.get('issue_title', '')}\n\n"
                    f"**Tech Lead 意见**：{payload.get('director_feedback', '') or '（无）'}\n\n"
                    "```diff\n"
                    f"{payload.get('diff', '') or '（空）'}\n"
                    "```\n填写意见后点 ✅批准 / ❌驳回"
                )
            if t == "confirm_anchors":
                return (
                    "### 🧪 确认 F2P 复现测试（Red 锚定态）\n"
                    f"**测试节点**：{payload.get('f2p_tests', [])}\n\n"
                    "```python\n"
                    f"{payload.get('f2p_test_code', '')}\n"
                    "```\n确认该测试能在未修复的仓库上稳定触发 Bug 后点 ✅批准"
                )
            return f"### 中断：{payload}"

        def on_start(repo: str, issue: str, st):
            if not repo.strip() or not issue.strip():
                return "请填写仓库路径与 Bug 描述", "", "", st
            repo_p = Path(repo.strip()).resolve()
            if not repo_p.is_dir():
                return f"仓库目录不存在: {repo_p}", "", "", st
            job_id = (st or {}).get("job_id")
            try:
                if job_id:
                    snap = rt.resume(job_id, {"answer": issue.strip()})
                else:
                    job_id = uuid.uuid4().hex[:12]
                    st = {"job_id": job_id}
                    snap = rt.start(issue.strip(), job_id, str(repo_p))
            except Exception as e:  # noqa: BLE001
                return f"### ❌ 执行异常: {e}", "", "", st
            s, i, _ = _fmt_snapshot(snap)
            return s, i, "", st

        def _act(approved: bool, answer: str, st):
            job_id = (st or {}).get("job_id")
            if not job_id:
                return "尚无进行中的任务", "", st
            try:
                snap = rt.resume(job_id, {"approved": approved, "feedback": answer.strip()})
            except Exception as e:  # noqa: BLE001
                return f"### ❌ 执行异常: {e}", "", st
            s, i, _ = _fmt_snapshot(snap)
            return s, i, st

        def on_answer(answer: str, st):
            """澄清模式下把输入作为回答发送。"""
            job_id = (st or {}).get("job_id")
            if not job_id:
                return "尚无进行中的任务", "", st
            try:
                snap = rt.resume(job_id, {"answer": answer.strip()})
            except Exception as e:  # noqa: BLE001
                return f"### ❌ 执行异常: {e}", "", st
            s, i, _ = _fmt_snapshot(snap)
            return s, i, st

        btn_start.click(on_start, [inp_repo, inp_issue, state], [md_status, md_interrupt, txt_answer, state])
        btn_answer.click(on_answer, [txt_answer, state], [md_status, md_interrupt, state])
        btn_approve.click(lambda a, s: _act(True, a, s), [txt_answer, state], [md_status, md_interrupt, state])
        btn_reject.click(lambda a, s: _act(False, a, s), [txt_answer, state], [md_status, md_interrupt, state])

    return demo


def main():
    parser = argparse.ArgumentParser(description="CodeGuard-Agent")
    parser.add_argument("--cli", action="store_true", help="CLI 模式")
    parser.add_argument("--repo", default="", help="目标仓库根目录（CLI 模式）")
    parser.add_argument("--issue", default="", help="Bug 描述或堆栈（CLI 模式）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7861)
    args = parser.parse_args()

    cfg = load_config()
    cfg.validate_for_pipeline()
    rt = CodeGuardRuntime(cfg)

    if args.cli:
        if not args.repo or not args.issue:
            parser.error("--cli 模式需要 --repo 与 --issue")
        run_cli(rt, str(Path(args.repo).resolve()), args.issue)
        return

    demo = build_ui(rt)
    demo.launch(server_name=args.host, server_port=args.port, show_error=True)


if __name__ == "__main__":
    main()
