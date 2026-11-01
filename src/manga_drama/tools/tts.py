"""云端 TTS 可插拔适配器。

默认 CosyVoiceAdapter 走 DashScope（与 Qwen/万相同 key）。voice_id 由 CharacterCard
锁定，同角色全程同 voice_id 防声音跳变。不用本地 Edge-TTS。
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from ..config import MangaDramaConfig
from ..errors import NonRetryableError, RetryableError
from ..retry import with_retry

DEFAULT_TTS_MODEL = os.environ.get("TTS_MODEL", "cosyvoice-v1")
# 默认音色（龙小纯，女声）；导演会按角色分配具体 voice_id
DEFAULT_VOICE = os.environ.get("TTS_DEFAULT_VOICE", "longxiaochun")


class TTSAdapter(ABC):
    """TTS 适配器抽象基类。"""

    @abstractmethod
    def synthesize(self, text: str, voice_id: str, out_path: str | Path) -> str:
        """合成语音，返回保存后的本地音频路径。"""


class CosyVoiceAdapter(TTSAdapter):
    """DashScope CosyVoice 适配器（v2 SpeechSynthesizer，阻塞返回完整音频字节）。"""

    def __init__(self, cfg: MangaDramaConfig, model: str = DEFAULT_TTS_MODEL):
        self.cfg = cfg
        self.model = model

    def synthesize(self, text: str, voice_id: str, out_path: str | Path) -> str:
        from dashscope.audio.tts_v2 import (  # 延迟导入，便于测试 mock
            AudioFormat,
            SpeechSynthesizer,
        )
        import dashscope

        api_key = self.cfg.require_dashscope()
        dashscope.api_key = api_key  # v2 通过全局 api_key 鉴权
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        @with_retry()
        def _do():
            try:
                synth = SpeechSynthesizer(
                    model=self.model,
                    voice=voice_id or DEFAULT_VOICE,
                    format=AudioFormat.MP3_22050HZ_MONO_256KBPS,
                )
                audio = synth.call(text)
                return audio
            except httpx.RequestError as e:
                raise RetryableError(f"TTS 网络错误: {e}") from e
            except Exception as e:
                name = type(e).__name__
                msg = str(e)
                if "ApiKey" in name or "401" in msg or "Unauthorized" in msg:
                    raise NonRetryableError(f"TTS 鉴权失败: {e}") from e
                raise RetryableError(f"TTS 调用异常 ({name}): {e}") from e

        audio = _do()
        if audio is None:
            raise NonRetryableError("TTS 返回空音频")
        if isinstance(audio, (bytes, bytearray)):
            out.write_bytes(bytes(audio))
        else:
            # 个别版本返回文件路径或其它结构
            raise NonRetryableError(f"TTS 返回非字节音频: {type(audio)}")
        return str(out)


class VolcanoAdapter(TTSAdapter):
    """火山引擎 TTS 适配器（占位）。

    火山 TTS 需AccessKey/SecretKey 签名 + appid/cluster，调用方式与阿里系差异较大。
    默认不实现；如需启用，填入火山 SDK 的签名与调用逻辑。
    """

    def __init__(self, cfg: MangaDramaConfig):
        self.cfg = cfg

    def synthesize(self, text: str, voice_id: str, out_path: str | Path) -> str:
        raise NonRetryableError(
            "VolcanoAdapter 尚未实现：请接入火山引擎 TTS SDK，或改用 TTS_PROVIDER=cosyvoice"
        )


# ---- 注册表与工厂 ----

_REGISTRY: dict[str, type[TTSAdapter]] = {
    "cosyvoice": CosyVoiceAdapter,
    "volcano": VolcanoAdapter,
}


def register_tts(name: str, cls: type[TTSAdapter]) -> None:
    _REGISTRY[name.lower()] = cls


def get_tts(cfg: MangaDramaConfig) -> TTSAdapter:
    name = cfg.tts_provider.lower()
    if name not in _REGISTRY:
        raise NonRetryableError(
            f"未知 TTS provider: {name}（已注册: {list(_REGISTRY)}）"
        )
    return _REGISTRY[name](cfg)
