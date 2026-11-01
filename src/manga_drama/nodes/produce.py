"""下层漫剧制作 Agent：锚定图生成 + 视频制作（ReAct per-shot 架构）。

逐镜 ReAct 循环：
- 首次尝试：直接 Act（构建 prompt + 生成），无需 LLM Think
- 失败重试：Think（LLM 分析失败原因）→ Decide（重试/跳过）→ Act → Observe

约束：每个 shot 最多重试 2 次（MAX_RETRIES_PER_SHOT），失败 shot 不阻塞其他 shot。
"""
from __future__ import annotations

import json

from ..errors import ContentPolicyError
from ..prompts.produce import shot_retry_prompt
from ..state import WorkflowState
from ..tools.consistency import build_image_prompt
from ..tools.ffmpeg_tool import compose_vertical_video
from ..tools.storyboard import generate_storyboard
from ._common import call_llm_json, settle_costs, workdir_for

T2I_UNIT_COST_CNY = 0.04
TTS_UNIT_COST_CNY = 0.02
T2I_SIZE = "720*1280"  # 接近 9:16 竖屏
MAX_RETRIES_PER_SHOT = 2


def _style(state: WorkflowState) -> tuple[str, str]:
    brief = state.get("brief") or {}
    plan = state.get("plan") or {}
    return (
        brief.get("style_prefix", "") or plan.get("style_prefix", ""),
        brief.get("negative_prompt", ""),
    )


def _merge_shots(shots: list[dict], updates: list[dict]) -> list[dict]:
    by_id = {s["shot_id"]: dict(s) for s in shots if s.get("shot_id")}
    for u in updates:
        sid = u.get("shot_id")
        if sid:
            by_id[sid] = {**by_id.get(sid, {}), **u}
    return list(by_id.values())


def _observe_result(img_path: str | None, aud_path: str | None) -> tuple[bool, str]:
    """Observe 阶段：检查生成结果，返回 (success, error_detail)。

    图片是必须的；音频缺失不阻止流程（合成时跳过无声分镜）。
    """
    from pathlib import Path

    issues = []
    if not img_path:
        issues.append("图片生成失败")
    elif not Path(img_path).exists():
        issues.append(f"图片文件不存在: {img_path}")
    elif Path(img_path).stat().st_size < 1000:
        issues.append(f"图片文件过小: {Path(img_path).stat().st_size} bytes")

    # 音频缺失不阻止流程，仅记录警告
    if not aud_path:
        pass  # 允许无声分镜
    elif not Path(aud_path).exists():
        pass
    elif Path(aud_path).stat().st_size < 500:
        pass

    if issues:
        return False, "; ".join(issues)
    return True, ""


def _react_single_shot(
    shot: dict, bible: dict, style_prefix: str, negative: str,
    prev_summary: str | None, wd, t2i, tts, ctx, all_costs: list[dict],
) -> dict:
    """单个 shot 的 ReAct 循环：Act → Observe → (Think → Decide → Act) × retry。

    返回 shot_update dict。
    """
    sid = shot.get("shot_id") or f"S{shot.get('index', 0):03d}"

    # --- Act：首次尝试 ---
    ip, neg, seed = build_image_prompt(shot, bible, style_prefix, negative, prev_summary)
    img_path, aud_path = _act_generate(
        sid, ip, neg, seed, shot, bible, t2i, tts, wd, all_costs
    )

    # --- Observe：检查结果 ---
    success, error_detail = _observe_result(img_path, aud_path)
    if success:
        return _shot_update(sid, ip, neg, seed, img_path, aud_path, prev_summary,
                           shot, "generated")

    # --- ReAct 重试循环 ---
    last_error = error_detail
    for attempt in range(1, MAX_RETRIES_PER_SHOT + 1):
        # Think：LLM 分析失败原因，决定重试策略
        retry_decision = _think_retry(shot, last_error, attempt, ctx, all_costs)
        if retry_decision.get("action") == "skip":
            break

        # Act：重试生成（使用调整后的策略）
        strategy = retry_decision.get("new_strategy", "")
        if strategy:
            # 根据策略调整 prompt
            ip = f"{ip}，{strategy}"
        img_path, aud_path = _act_generate(
            sid, ip, neg, seed, shot, bible, t2i, tts, wd, all_costs
        )

        # Observe：再次检查
        success, last_error = _observe_result(img_path, aud_path)
        if success:
            return _shot_update(sid, ip, neg, seed, img_path, aud_path, prev_summary,
                               shot, "generated")

    # 所有重试均失败
    return _shot_update(sid, ip, neg, seed, img_path, aud_path, prev_summary,
                       shot, "failed", last_error)


def _act_generate(
    sid: str, ip: str, neg: str, seed: int,
    shot: dict, bible: dict, t2i, tts, wd, all_costs: list[dict],
) -> tuple[str | None, str | None]:
    """Act 阶段：生成图片 + 音频。返回 (img_path, aud_path)。"""
    img_path = None
    img_out = wd / f"{sid}.png"
    try:
        t2i.text_to_image(ip, neg, seed, T2I_SIZE, img_out)
        all_costs.append({"type": "t2i", "model": getattr(t2i, "model", "wanx"),
                          "cost_cny": T2I_UNIT_COST_CNY, "detail": {"shot_id": sid}})
        img_path = str(img_out)
    except Exception as e:  # noqa: BLE001
        all_costs.append({"type": "t2i", "model": "wanx", "cost_cny": 0.0,
                          "detail": {"shot_id": sid, "error": str(e)}})

    text = shot.get("dialogue") or shot.get("narration") or shot.get("scene_desc", "")
    aud_path = None
    if text.strip():
        chars = shot.get("characters_in_shot", []) or []
        voice = bible.get(chars[0], {}).get("voice_id", "longxiaochun") if chars else "longxiaochun"
        aud_out = wd / f"{sid}.mp3"
        try:
            tts.synthesize(text, voice, aud_out)
            all_costs.append({"type": "tts", "model": "cosyvoice",
                              "cost_cny": TTS_UNIT_COST_CNY, "detail": {"shot_id": sid}})
            aud_path = str(aud_out)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.warning(f"[produce] TTS failed for {sid}: {e}")
            all_costs.append({"type": "tts", "model": "cosyvoice", "cost_cny": 0.0,
                              "detail": {"shot_id": sid, "error": str(e)}})

    return img_path, aud_path


def _think_retry(
    shot: dict, last_error: str, attempt: int, ctx, all_costs: list[dict],
) -> dict:
    """Think 阶段：LLM 分析失败原因，决定重试策略。"""
    prompt = shot_retry_prompt(shot, last_error, attempt)
    obj, costs = call_llm_json(ctx, "produce_react", prompt, temperature=0.3)
    all_costs.extend(costs)
    return obj


def _shot_update(
    sid: str, ip: str, neg: str, seed: int,
    img_path: str | None, aud_path: str | None,
    prev_summary: str | None, shot: dict, status: str,
    error: str = "",
) -> dict:
    """构建 shot 更新 dict。"""
    update = {
        "shot_id": sid,
        "image_path": img_path,
        "audio_path": aud_path,
        "image_prompt": ip,
        "negative_prompt": neg,
        "seed": seed,
        "status": status,
        "prev_shot_summary": prev_summary,
        "image_attempts": int(shot.get("image_attempts", 0)) + 1,
    }
    if error:
        update["last_error"] = error
    return update


def build_anchors(state: WorkflowState, ctx) -> dict:
    """为每个未确认角色生成锚定图（human_review 后角色名单稳定）。"""
    if hasattr(ctx, "rt") and ctx.rt:
        ctx.rt.update_progress(state.get("job_id"), "正在生成主要角色的外观设定锚定图...", 70)
    bible = state.get("character_bible") or {}
    style_prefix, negative = _style(state)
    t2i = ctx.t2i()
    wd = workdir_for(state, ctx)
    wd.mkdir(parents=True, exist_ok=True)

    costs: list[dict] = []
    updates: dict = {}
    for cid, card in bible.items():
        if card.get("confirmed") and card.get("anchor_image_path"):
            continue
        spec = card.get("appearance_spec", "")
        prompt = (
            (f"{style_prefix}，" if style_prefix else "")
            + f"角色设定参考图：{spec}，正面半身像，中性表情，纯白背景，均匀光照，无道具"
        )
        seed = int(card.get("base_seed", 0) or 0)
        out = wd / f"anchor_{cid}.png"
        try:
            t2i.text_to_image(prompt, negative, seed, T2I_SIZE, out)
            costs.append({
                "type": "t2i", "model": getattr(t2i, "model", "wanx"),
                "cost_cny": T2I_UNIT_COST_CNY, "detail": {"char_id": cid},
            })
            updates[cid] = {"anchor_image_path": str(out)}
        except ContentPolicyError as e:
            costs.append({"type": "t2i", "model": "wanx", "cost_cny": 0.0,
                          "detail": {"char_id": cid, "error": str(e)}})
            updates[cid] = {"anchor_image_path": None}
        except Exception as e:  # noqa: BLE001 记录错误但不阻塞其它角色
            costs.append({"type": "t2i", "model": "wanx", "cost_cny": 0.0,
                          "detail": {"char_id": cid, "error": str(e)}})
            updates[cid] = {"anchor_image_path": None}

    update = settle_costs(state, ctx, costs)
    if updates:
        update["character_bible"] = updates
    update["status"] = "confirming_anchors"
    return update


def produce_video(state: WorkflowState, ctx) -> dict:
    """分镜→逐镜 ReAct 生成→ffmpeg 合成。

    每个 shot 内部执行 ReAct 循环：
    - 首次尝试：直接 Act（构建 prompt + 生成）
    - 失败重试：Think（LLM 分析）→ Decide → Act → Observe
    - 每 shot 最多重试 MAX_RETRIES_PER_SHOT 次
    """
    cfg = ctx.cfg
    bible = state.get("character_bible") or {}
    style_prefix, negative = _style(state)
    draft = state.get("draft") or ""
    wd = workdir_for(state, ctx)
    wd.mkdir(parents=True, exist_ok=True)
    costs: list[dict] = []

    # 1. 分镜（幂等）
    shots = list(state.get("shots") or [])
    if not shots:
        if hasattr(ctx, "rt") and ctx.rt:
            ctx.rt.update_progress(state.get("job_id"), "正在规划漫剧分镜脚本...", 75)
        sb_shots, sb_costs = generate_storyboard(draft, bible, ctx.llm("produce"))
        costs.extend(sb_costs)
        if not sb_shots:
            update = settle_costs(state, ctx, costs)
            update.update({
                "production_attempt_count": state.get("production_attempt_count", 0) + 1,
                "status": "failed",
                "error": "分镜生成失败",
            })
            return update
        shots = sb_shots

    # 2. 逐镜 ReAct 生成（幂等跳过 generated）
    t2i = ctx.t2i()
    tts = ctx.tts()
    shot_updates: list[dict] = []
    prev_summary = None
    total_shots = len(shots)
    for idx, shot in enumerate(shots, 1):
        sid = shot.get("shot_id") or f"S{shot.get('index', 0):03d}"
        
        # 实时更新逐镜生成状态与进度
        if hasattr(ctx, "rt") and ctx.rt:
            percent = 75 + int((idx / total_shots) * 20)
            ctx.rt.update_progress(
                state.get("job_id"),
                f"正在生成第 {idx}/{total_shots} 分镜画面与配音...",
                percent
            )
            
        # 幂等跳过
        if shot.get("status") == "generated" and shot.get("image_path") and shot.get("audio_path"):
            prev_summary = shot.get("scene_desc") or prev_summary
            continue

        # ReAct 循环
        update_entry = _react_single_shot(
            shot, bible, style_prefix, negative, prev_summary,
            wd, t2i, tts, ctx, costs
        )
        shot_updates.append(update_entry)
        prev_summary = shot.get("scene_desc") or prev_summary

        # 发射 SSE 事件：单镜完成
        if hasattr(ctx, "rt") and ctx.rt:
            ctx.rt.emit_event(state.get("job_id"), "shot_progress", {
                "shot_id": sid,
                "index": idx,
                "total": total_shots,
                "status": update_entry.get("status", "unknown"),
            })

    update = settle_costs(state, ctx, costs)
    if shot_updates:
        update["shots"] = shot_updates

    # 3. 全部就绪则合成；否则重试
    # 图片必须有，音频可选（无声分镜用静音音轨）
    all_shots = _merge_shots(shots, shot_updates)
    ready = [s for s in all_shots
             if s.get("status") == "generated" and s.get("image_path")]
    attempt = state.get("production_attempt_count", 0)

    if all_shots and len(ready) == len(all_shots):
        compose_inputs = [{
            "image_path": s["image_path"],
            "audio_path": s.get("audio_path"),  # 可选，缺失时用静音
            "subtitle": s.get("dialogue") or s.get("narration") or s.get("scene_desc", ""),
            "duration_sec": s.get("duration_sec", 3.0),
        } for s in all_shots]
        video_out = cfg.output_dir / f"{state.get('job_id', 'default')}.mp4"
        
        if hasattr(ctx, "rt") and ctx.rt:
            ctx.rt.update_progress(state.get("job_id"), "分镜全部就绪，正在进行音视频合成与渲染...", 98)
            
        try:
            compose_vertical_video(compose_inputs, video_out,
                                   ffmpeg_path=cfg.ffmpeg_path, workdir=wd / "compose")
            update["video_path"] = str(video_out)
            update["status"] = "done"
            if hasattr(ctx, "rt") and ctx.rt:
                ctx.rt.update_progress(state.get("job_id"), "漫剧视频合成完成！", 100)
                ctx.rt.emit_event(state.get("job_id"), "video_done", {
                    "video_path": str(video_out),
                })
        except Exception as e:  # noqa: BLE001
            update["production_attempt_count"] = attempt + 1
            update["status"] = "failed"
            update["error"] = f"视频合成失败: {e}"
    else:
        update["production_attempt_count"] = attempt + 1
        if update["production_attempt_count"] >= cfg.max_prod:
            update["status"] = "failed"
            update["error"] = "部分分镜生成失败，达到重试上限"
        else:
            update["status"] = "producing"
    return update
