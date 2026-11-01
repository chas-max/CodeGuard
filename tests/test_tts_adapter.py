"""CosyVoice TTS 适配器测试（mock dashscope，无真实 API）。"""
import dashscope.audio.tts_v2 as v2

from manga_drama.tools.tts import CosyVoiceAdapter, VolcanoAdapter, get_tts


class _FakeSynth:
    def __init__(self, **kw):
        self.kw = kw

    def call(self, text):
        self.text = text
        return b"MP3AUDIOBYTES"


def test_cosyvoice_synthesize(mock_cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(v2, "SpeechSynthesizer", _FakeSynth)
    ad = CosyVoiceAdapter(mock_cfg, model="cosyvoice-v1")
    out = tmp_path / "a.mp3"
    p = ad.synthesize("你好世界", "longxiaochun", out)

    assert p == str(out)
    assert out.read_bytes() == b"MP3AUDIOBYTES"


def test_cosyvoice_passes_voice_and_model(mock_cfg, monkeypatch, tmp_path):
    captured = {}

    class _S:
        def __init__(self, **kw):
            captured.update(kw)

        def call(self, text):
            return b"x"

    monkeypatch.setattr(v2, "SpeechSynthesizer", _S)
    ad = CosyVoiceAdapter(mock_cfg, model="cosyvoice-v2")
    ad.synthesize("hi", "longwan", tmp_path / "b.mp3")
    assert captured["model"] == "cosyvoice-v2"
    assert captured["voice"] == "longwan"


def test_get_tts_factory(mock_cfg):
    assert isinstance(get_tts(mock_cfg), CosyVoiceAdapter)


def test_volcano_not_implemented(mock_cfg):
    from manga_drama.errors import NonRetryableError
    import pytest
    from dataclasses import replace

    cfg = replace(mock_cfg, tts_provider="volcano")
    ad = get_tts(cfg)
    assert isinstance(ad, VolcanoAdapter)
    with pytest.raises(NonRetryableError):
        ad.synthesize("hi", "v", "out.mp3")
