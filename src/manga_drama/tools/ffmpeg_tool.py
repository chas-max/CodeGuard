"""ffmpeg 合成竖屏漫剧视频。

规避中文路径问题：把每镜的图/音频以 ASCII 名拷进工作目录，ffmpeg 以 cwd=workdir
运行、仅引用 ASCII 相对名（图/音/片段/字幕/输出全为 ASCII 相对路径），最后用 Python
把成品 mp4 拷到目标输出路径（Python 对 Unicode 路径稳健）。字幕走 SRT + libass，
对中文渲染比 drawtext 可靠。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from ..errors import NonRetryableError

_CJK_FONTS = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]

DEFAULT_SUBTITLE_STYLE = (
    "FontName=Microsoft YaHei,FontSize=22,PrimaryColour=&H00FFFFFF,"
    "Outline=1,Shadow=0,Alignment=2,MarginV=50"
)


def find_font() -> Optional[str]:
    """查找一个 CJK 字体文件路径；找不到返回 None（libass 也可用系统默认）。"""
    f = os.environ.get("FFMPEG_FONT")
    if f and Path(f).exists():
        return f
    for p in _CJK_FONTS:
        if Path(p).exists():
            return p
    return None


def _ffprobe_path(ffmpeg_path: str) -> str:
    f = os.environ.get("FFPROBE_PATH")
    if f:
        return f
    d = Path(ffmpeg_path)
    if d.name.lower() in ("ffmpeg", "ffmpeg.exe"):
        exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        return str(d.with_name(exe))
    return "ffprobe"


def _probe_duration(
    audio_rel: str,
    ffmpeg_path: str,
    runner: Callable,
    cwd: str,
) -> Optional[float]:
    """探测音频时长（秒）；失败返回 None。"""
    ffprobe = _ffprobe_path(ffmpeg_path)
    try:
        r = runner(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", audio_rel,
            ],
            capture_output=True, text=True, cwd=cwd,
        )
        if getattr(r, "returncode", 1) == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None


def _srt_timestamp(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(path: Path, entries: list[tuple[float, float, str]]) -> None:
    lines: list[str] = []
    for i, (start, end, text) in enumerate(entries, 1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}")
        lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def compose_vertical_video(
    shots: list[dict],
    output_path: str | Path,
    ffmpeg_path: str = "ffmpeg",
    subtitle_style: str = DEFAULT_SUBTITLE_STYLE,
    runner: Optional[Callable] = None,
    workdir: Optional[str | Path] = None,
) -> str:
    """把分镜（image_path/audio_path/subtitle）合成 1080x1920 竖屏 mp4。

    Args:
        shots: 每镜 dict，含 image_path、audio_path、subtitle(可选)、duration_sec(回退)。
        output_path: 最终 mp4 输出路径。
        ffmpeg_path: ffmpeg 可执行文件。
        subtitle_style: libass force_style 字符串。
        runner: subprocess.run 替身（测试用）。
        workdir: 工作目录（存放 ASCII 命名中间文件）。

    Returns:
        最终 mp4 路径字符串。
    """
    if not shots:
        raise NonRetryableError("无分镜，无法合成视频")

    runner = runner or subprocess.run
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wd = Path(workdir or tempfile.mkdtemp(prefix="manga_drama_"))
    wd.mkdir(parents=True, exist_ok=True)
    cwd = str(wd)

    segs: list[str] = []
    srt_entries: list[tuple[float, float, str]] = []
    cursor = 0.0

    for i, shot in enumerate(shots):
        if not shot.get("image_path"):
            raise NonRetryableError(f"分镜 {i} 缺少 image_path")
        rel_img = f"img{i}.png"
        shutil.copyfile(shot["image_path"], wd / rel_img)

        has_audio = bool(shot.get("audio_path"))
        if has_audio:
            rel_aud = f"aud{i}.mp3"
            shutil.copyfile(shot["audio_path"], wd / rel_aud)
            dur = _probe_duration(rel_aud, ffmpeg_path, runner, cwd)
        else:
            rel_aud = None
            dur = None

        if not dur:
            dur = float(shot.get("duration_sec", 3.0))

        seg = f"seg{i}.mp4"
        vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
        if rel_aud:
            cmd = [
                ffmpeg_path, "-y", "-loop", "1", "-i", rel_img, "-i", rel_aud,
                "-vf", vf, "-t", f"{dur:.3f}", "-shortest",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "128k", "-r", "24", seg,
            ]
        else:
            # 无音频：生成静音音轨
            cmd = [
                ffmpeg_path, "-y", "-loop", "1", "-i", rel_img,
                "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
                "-vf", vf, "-t", f"{dur:.3f}", "-shortest",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "128k", "-r", "24", seg,
            ]
        r = runner(cmd, capture_output=True, text=True, cwd=cwd)
        if getattr(r, "returncode", 1) != 0:
            raise NonRetryableError(
                f"ffmpeg 生成片段 {i} 失败: {(getattr(r,'stderr','') or '')[-400:]}"
            )
        segs.append(seg)

        sub = (shot.get("subtitle") or "").strip()
        if sub:
            srt_entries.append((cursor, cursor + dur, sub))
        cursor += dur

    # 拼接
    (wd / "list.txt").write_text(
        "\n".join(f"file '{s}'" for s in segs), encoding="utf-8"
    )
    r = runner(
        [ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", "list.txt",
         "-c", "copy", "raw.mp4"],
        capture_output=True, text=True, cwd=cwd,
    )
    if getattr(r, "returncode", 1) != 0:
        raise NonRetryableError(f"ffmpeg 拼接失败: {(getattr(r,'stderr','') or '')[-400:]}")

    # 烧录字幕
    if srt_entries:
        _write_srt(wd / "subs.srt", srt_entries)
        vf = f"subtitles=subs.srt:force_style='{subtitle_style}'"
        r = runner(
            [ffmpeg_path, "-y", "-i", "raw.mp4", "-vf", vf, "-c:a", "copy", "final.mp4"],
            capture_output=True, text=True, cwd=cwd,
        )
        if getattr(r, "returncode", 1) != 0:
            # 字幕烧录失败时退回无字幕版，避免阻塞
            shutil.copyfile(wd / "raw.mp4", wd / "final.mp4")
    else:
        shutil.copyfile(wd / "raw.mp4", wd / "final.mp4")

    shutil.copyfile(wd / "final.mp4", out)
    return str(out)
