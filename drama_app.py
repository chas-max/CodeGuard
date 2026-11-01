"""漫剧制作系统入口：Gradio Web UI（默认）或 CLI（--cli）。

用法：
  python drama_app.py            # 启动 Gradio UI
  python drama_app.py --cli      # 交互式命令行
首次运行前请在 .env 配置 DEEPSEEK_API_KEY 与 DASHSCOPE_API_KEY（见 .env.example）。

聊天式改造：
- 用户可在任意阶段发送消息，理解 Agent 自动识别是澄清需求还是修改请求
- 修改请求路由到对应节点（write_draft / director_plan / understand）
- 对话历史自动摘要压缩，无轮数上限
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

# 把 src 加入路径，方便直接 python drama_app.py 运行
sys.path.insert(0, str(Path(__file__).parent / "src"))

import gradio as gr

from manga_drama.config import MissingConfigError, load_config
from manga_drama.runtime import MangaDramaRuntime


def _fmt_interrupt(interrupt: dict | None) -> str:
    if not interrupt:
        return ""
    t = interrupt.get("type")
    if t == "clarify":
        missing = "、".join(interrupt.get("missing", [])) or "（无）"
        chat_mode = interrupt.get("chat_mode", "briefing")
        is_chat_reply = interrupt.get("is_chat_reply", False)

        if is_chat_reply:
            # 纯聊天回复，不是追问
            return f"💬 {interrupt.get('question', '')}"

        if chat_mode == "briefing":
            missing_part = f"\n\n（待确认维度：{missing}）" if missing and missing != "（无）" else ""
            return (
                f"💬 **需求澄清**（第 {interrupt.get('round', '?')} 轮）\n\n"
                f"{interrupt.get('question', '')}"
                f"{missing_part}\n"
                f"请在下方输入框回答，或直接描述您对已生成内容的修改建议。"
            )
        else:
            # revision 或 idle 模式
            return (
                "✏️ **聊天修改模式**\n\n"
                "制作流程暂停，您可以：\n"
                "- 对已生成内容提出修改建议（如：「把第二场台词改得更感伤一些」）\n"
                "- 输入新的创作想法\n\n"
                "请在下方输入框输入您的想法。"
            )

    if t == "human_review":
        mv = interrupt.get("machine_verdict") or {}
        passed = "✅ 通过" if mv.get("passed") else "❌ 不通过"
        draft = (interrupt.get("draft", "") or "")[:1500]
        return (
            f"🔍 **人工内容审核**（硬约束：通过后才会制作）\n\n"
            f"机器结论：{passed}"
            f"{('｜问题：' + '、'.join(mv.get('issues', []))) if mv.get('issues') else ''}\n"
            f"导演意见：{interrupt.get('director_feedback', '') or '（无）'}\n\n"
            f"--- 剧本草稿 ---\n{draft}\n\n"
            f"请点「✅ 批准」或「❌ 驳回」（在修订意见框填写意见）。\n\n"
            f"💡 **或者**：您也可以在下方输入框直接提出修改建议，系统将自动识别并调整。"
        )
    if t == "confirm_anchors":
        anchors = interrupt.get("anchors", [])
        lines = [f"  - {a.get('char_id')} {a.get('name')}：{a.get('anchor_image_path')}"
                 for a in anchors]
        return (
            "🖼️ **锚定图确认**（角色外观将锁定用于全片）\n\n"
            + "\n".join(lines)
            + "\n\n请点「✅ 批准」或「❌ 驳回」要求重画。"
        )
    return f"⏸️ 中断：{interrupt}"


def _status_line(values: dict) -> str:
    status = values.get("status", "")
    cost = sum(float(e.get("cost_cny", 0.0)) for e in values.get("budget_events", []))
    chat_mode = values.get("chat_mode", "")
    mapping = {
        "clarifying": "澄清需求中", "briefed": "需求已确认", "planning": "导演规划中",
        "writing": "剧本创作中", "reviewing": "机器审核中", "supervising": "导演监督中",
        "awaiting_human": "等待人工审核", "anchors_pending": "等待锚定确认",
        "confirming_anchors": "锚定图生成/确认", "producing": "视频制作中",
        "done": "✅ 完成", "failed": "❌ 失败",
    }
    label = mapping.get(status, status)
    mode_tag = ""
    if chat_mode == "revision":
        mode_tag = " ｜ ✏️ 修改模式"
    elif chat_mode == "briefing":
        mode_tag = " ｜ 💬 需求确认"
    return f"状态：{label}{mode_tag}｜累计成本：¥{cost:.4f}"


def _fmt_resolution(values: dict) -> str | None:
    """格式化导演解决方案（若有）用于展示。"""
    resolution = values.get("director_resolution")
    if not resolution or resolution.get("action") in ("skip", ""):
        return None
    action_map = {
        "retry": "🔄 重试",
        "downgrade": "⬇️ 降级处理",
        "skip": "⏭️ 跳过",
        "escalate": "🛑 终止",
    }
    action = action_map.get(resolution.get("action", ""), resolution.get("action", ""))
    strategy = resolution.get("strategy", "")
    if strategy:
        return f"🎬 **导演协调**：{action} — {strategy}"
    return None


def build_ui(rt: MangaDramaRuntime):
    def refresh(job_id, history, note):
        snap = rt.snapshot(job_id) if job_id else None
        if not snap:
            return job_id, None, history, ""
        interrupt = snap["interrupt"]
        values = snap["values"]
        
        # In Gradio 6+, history is a list of dicts, e.g. [{"role": "user", "content": "..."}, ...]
        if note:
            history = history + [{"role": "assistant", "content": note}]
        history = history + [{"role": "assistant", "content": _status_line(values)}]

        # 展示导演协调信息
        resolution_msg = _fmt_resolution(values)
        if resolution_msg:
            history = history + [{"role": "assistant", "content": resolution_msg}]

        if interrupt:
            history = history + [{"role": "assistant", "content": _fmt_interrupt(interrupt)}]
        else:
            if values.get("status") == "done":
                history = history + [{"role": "assistant", "content": f"🎉 制作完成！视频文件：{values.get('video_path')}"}]
            elif values.get("status") == "failed":
                history = history + [{"role": "assistant", "content": f"⚠️ 流程失败：{values.get('error', '')}"}]
        return job_id, interrupt, history, ""

    def on_send(msg, job_id, interrupt, history):
        """处理用户发送消息。

        无论当前是否有中断、处于何种状态，用户的消息都会被处理：
        - 无任务：启动新任务
        - 有中断（clarify）：回答追问或提出修改建议
        - 无中断（流程自动进行中）：暂存修改建议，等流程到达下个 interrupt 点时处理
        """
        msg = (msg or "").strip()
        if not msg:
            return job_id, interrupt, history, ""

        if not job_id:
            # 启动新任务
            job_id = uuid.uuid4().hex[:12]
            history = history + [{"role": "user", "content": msg}]
            rt.start(msg, job_id)
            return refresh(job_id, history, "🎬 已启动制作流程")

        if interrupt and interrupt.get("type") == "clarify":
            # 中断等待用户输入（澄清或修改）
            history = history + [{"role": "user", "content": msg}]
            rt.resume(job_id, msg)
            return refresh(job_id, history, "💬 已发送")

        # 当前无中断（流程执行中）：用户的修改建议需要等待下次中断
        # 显示提示消息，建议用户等流程完成或下次 interrupt 再提
        history = history + [
            {"role": "user", "content": msg},
            {"role": "assistant", "content": "⏳ 当前制作流程正在运行，您的建议已记录。\n待当前步骤完成后，请在下次提问时再次输入您的修改建议。"}
        ]
        return job_id, interrupt, history, ""

    def on_approve(job_id, interrupt, history):
        if not interrupt:
            return job_id, interrupt, history
        t = interrupt.get("type")
        if t == "human_review":
            rt.resume(job_id, {"approved": True, "feedback": ""})
        elif t == "confirm_anchors":
            rt.resume(job_id, {"approved": True})
        else:
            return job_id, interrupt, history
        return refresh(job_id, history, "✅ 已批准")[:3]

    def on_reject(job_id, interrupt, feedback, history):
        if not interrupt:
            return job_id, interrupt, history, ""
        t = interrupt.get("type")
        if t == "human_review":
            rt.resume(job_id, {"approved": False, "feedback": feedback or "请按意见修订"})
        elif t == "confirm_anchors":
            rt.resume(job_id, {"approved": False, "revisions": {}})
        else:
            return job_id, interrupt, history, ""
        return refresh(job_id, history, "❌ 已驳回")

    def on_new_task(history):
        """重置，开始新任务。"""
        return None, None, [], ""

    with gr.Blocks(title="AI 漫剧制作 · 聊天式多 Agent 协作") as demo:
        gr.Markdown(
            "# 🎬 AI 漫剧制作系统\n"
            "**聊天式五 Agent 三层协作**：需求理解（带记忆）→ 导演（异常统筹）→ 创作（Tavily增强）/ 审核 / 制作。\n"
            "💡 **任意阶段均可发送修改建议**，理解 Agent 会自动识别意图并路由到对应环节。"
        )
        job_id_state = gr.State(None)
        interrupt_state = gr.State(None)

        with gr.Row():
            with gr.Column(scale=3):
                chatbot = gr.Chatbot(
                    label="对话与进度",
                    height=560,
                )
                with gr.Row():
                    msg = gr.Textbox(
                        label="💬 输入（创作想法 / 回答问题 / 修改建议）",
                        placeholder="例：做一部黛玉葬花的古风漫剧，30秒 | 或在创作中途输入：把第二场台词改得更哀伤",
                        scale=4,
                        lines=2,
                    )
                    send_btn = gr.Button("发送 ▶", variant="primary", scale=1, min_width=80)
                with gr.Row():
                    approve_btn = gr.Button("✅ 批准", variant="secondary")
                    reject_btn = gr.Button("❌ 驳回", variant="stop")
                    new_task_btn = gr.Button("🆕 新任务", variant="secondary")
                feedback = gr.Textbox(
                    label="驳回 / 修订意见（驳回时填写，或直接在消息框提建议）",
                    placeholder="如：第2段过于暴力，请弱化",
                    lines=2,
                )

            with gr.Column(scale=1):
                gr.Markdown("### 💡 使用说明")
                gr.Markdown(
                    "**聊天式交互**\n\n"
                    "1. 输入创意想法，AI 会追问细节\n"
                    "2. 需求确认后自动进入创作\n"
                    "3. **任意时刻**可输入修改建议\n"
                    "   - 「改台词」→ 重写剧本\n"
                    "   - 「换角色设定」→ 重新规划\n"
                    "   - 「重新来过」→ 回到需求理解\n\n"
                    "**审核节点**\n\n"
                    "- 🔍 人工内容审核：批准或驳回\n"
                    "- 🖼️ 锚定图确认：批准角色外观\n\n"
                    "**导演统筹**\n\n"
                    "遇到异常时导演自动协调，\n"
                    "尽量保持流程推进而非直接终止。"
                )

        send_btn.click(
            on_send, [msg, job_id_state, interrupt_state, chatbot],
            [job_id_state, interrupt_state, chatbot, msg],
            api_name=False
        )
        msg.submit(
            on_send, [msg, job_id_state, interrupt_state, chatbot],
            [job_id_state, interrupt_state, chatbot, msg],
            api_name=False
        )
        approve_btn.click(
            on_approve, [job_id_state, interrupt_state, chatbot],
            [job_id_state, interrupt_state, chatbot],
            api_name=False
        )
        reject_btn.click(
            on_reject, [job_id_state, interrupt_state, feedback, chatbot],
            [job_id_state, interrupt_state, chatbot, feedback],
            api_name=False
        )
        new_task_btn.click(
            on_new_task, [chatbot],
            [job_id_state, interrupt_state, chatbot, msg],
            api_name=False
        )
    return demo


def run_cli(rt: MangaDramaRuntime):
    """简易交互式 CLI：用于无 UI 环境或脚本测试。"""
    print("=== AI 漫剧制作 CLI（聊天式）（输入 quit 退出）===")
    print("提示：在任意阶段均可输入修改建议，系统会自动识别并路由到对应节点。\n")
    job_id = None
    while True:
        snap = rt.snapshot(job_id) if job_id else None
        if snap and snap["interrupt"]:
            interrupt = snap["interrupt"]
            print("\n" + _fmt_interrupt(interrupt))
            t = interrupt.get("type")
            ans = input("你> ").strip()
            if ans.lower() in ("quit", "exit"):
                break
            if t == "clarify":
                rt.resume(job_id, ans)
            elif t == "human_review":
                if ans.lower() in ("y", "yes", "通过", "批准"):
                    rt.resume(job_id, {"approved": True, "feedback": ""})
                else:
                    rt.resume(job_id, {"approved": False, "feedback": ans})
            elif t == "confirm_anchors":
                if ans.lower() in ("y", "yes", "通过", "批准"):
                    rt.resume(job_id, {"approved": True})
                else:
                    rt.resume(job_id, {"approved": False, "revisions": {}})
            continue
        if snap and snap["done"]:
            v = snap["values"]
            if v.get("status") == "done":
                print(f"\n🎉 完成：{v.get('video_path')}")
            else:
                print(f"\n⚠️ {v.get('status')}：{v.get('error', '')}")
            job_id = None
            print("\n--- 新任务 ---")
        idea = input("你> ").strip()
        if idea.lower() in ("quit", "exit"):
            break
        if not idea:
            continue
        job_id = uuid.uuid4().hex[:12]
        print(f"🎬 启动任务 {job_id} ...")
        rt.start(idea, job_id)


def main():
    try:
        cfg = load_config()
        cfg.validate_for_pipeline()
    except MissingConfigError as e:
        print(str(e), file=sys.stderr)
        print("请参考 .env.example 配置 .env 后重试。", file=sys.stderr)
        sys.exit(1)

    rt = MangaDramaRuntime(cfg)

    if "--cli" in sys.argv:
        run_cli(rt)
        return

    demo = build_ui(rt)
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=False, share=True, theme=gr.themes.Soft())


if __name__ == "__main__":
    main()
