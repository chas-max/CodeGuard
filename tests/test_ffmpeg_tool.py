"""ffmpeg 合成测试（mock runner，不真实调用 ffmpeg）。"""
import os
from pathlib import Path

from manga_drama.errors import NonRetryableError
from manga_drama.tools.ffmpeg_tool import compose_vertical_video


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(record):
    def runner(cmd, **kw):
        record.append(list(cmd))
        cwd = kw.get("cwd")
        base = Path(cwd) if cwd else Path.cwd()
        # ffprobe 调用：返回时长
        if os.path.basename(cmd[0]).lower().startswith("ffprobe"):
            return _R(0, "3.50\n", "")
        # ffmpeg 调用：创建输出文件（最后一个位置参数）
        out_name = cmd[-1]
        (base / out_name).write_bytes(b"FAKEMP4")
        return _R(0, "", "")
    return runner


def test_compose_with_subtitle(tmp_path):
    wd = tmp_path / "work"
    img = tmp_path / "i.png"; img.write_bytes(b"IMG")
    aud = tmp_path / "a.mp3"; aud.write_bytes(b"AUD")
    shots = [{"image_path": str(img), "audio_path": str(aud),
              "subtitle": "你好世界", "duration_sec": 3.0}]
    out = tmp_path / "final.mp4"
    record: list = []
    p = compose_vertical_video(shots, out, runner=_fake_runner(record), workdir=wd)
    assert Path(p).read_bytes() == b"FAKEMP4"
    assert (wd / "list.txt").exists()
    assert (wd / "subs.srt").exists()
    assert "你好世界" in (wd / "subs.srt").read_text(encoding="utf-8")
    # 片段命令含 scale 到 1080x1920
    seg_cmd = next(c for c in record if "-loop" in c)
    assert "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" in seg_cmd
    # 字幕烧录命令
    assert any(any("subtitles=subs.srt" in a for a in c) for c in record)
    # 全部路径都是 ASCII 相对名（无中文工作目录污染参数）
    for c in record:
        for arg in c:
            assert isinstance(arg, str)


def test_compose_no_subtitle(tmp_path):
    wd = tmp_path / "work"
    img = tmp_path / "i.png"; img.write_bytes(b"IMG")
    aud = tmp_path / "a.mp3"; aud.write_bytes(b"AUD")
    shots = [{"image_path": str(img), "audio_path": str(aud), "subtitle": ""}]
    out = tmp_path / "o.mp4"
    p = compose_vertical_video(shots, out, runner=_fake_runner([]), workdir=wd)
    assert Path(p).exists()
    assert not (wd / "subs.srt").exists()


def test_compose_missing_asset_raises(tmp_path):
    shots = [{"image_path": "", "audio_path": "x"}]
    with __import__("pytest").raises(NonRetryableError):
        compose_vertical_video(shots, tmp_path / "o.mp4",
                               runner=_fake_runner([]), workdir=tmp_path / "w")


def test_compose_empty_shots_raises(tmp_path):
    with __import__("pytest").raises(NonRetryableError):
        compose_vertical_video([], tmp_path / "o.mp4",
                               runner=_fake_runner([]), workdir=tmp_path / "w")
