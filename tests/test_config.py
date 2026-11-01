"""config 加载与按 provider 校验测试。"""
import pytest

from manga_drama.config import load_config
from manga_drama.errors import MissingConfigError


def test_load_config_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deep")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash")
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setenv("T2I_PROVIDER", "wanx")
    monkeypatch.setenv("TTS_PROVIDER", "cosyvoice")
    monkeypatch.setenv("MAX_CLARIFY", "8")
    monkeypatch.setenv("BUDGET_MAX_CNY", "5.5")
    cfg = load_config(
        output_dir=tmp_path / "out",
        checkpoint_path=tmp_path / "ck.db",
        dotenv_path=tmp_path / "no-such.env",
    )
    assert cfg.deepseek_api_key == "sk-deep"
    assert cfg.dashscope_api_key == "sk-dash"
    assert cfg.mimo_api_key is None
    assert cfg.max_clarify == 8
    assert cfg.budget_max_cny == 5.5
    assert cfg.t2i_provider == "wanx"


def test_load_config_invalid_int_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_CLARIFY", "not-a-number")
    cfg = load_config(
        output_dir=tmp_path / "out",
        checkpoint_path=tmp_path / "ck.db",
        dotenv_path=tmp_path / "no.env",
    )
    assert cfg.max_clarify == 5  # 回退默认


def test_require_raises_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    cfg = load_config(
        output_dir=tmp_path / "out",
        checkpoint_path=tmp_path / "ck.db",
        dotenv_path=tmp_path / "no.env",
    )
    with pytest.raises(MissingConfigError):
        cfg.require_deepseek()
    with pytest.raises(MissingConfigError):
        cfg.require_dashscope()
    with pytest.raises(MissingConfigError):
        cfg.require_mimo()


def test_validate_for_pipeline_volcano_needs_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk")
    monkeypatch.setenv("TTS_PROVIDER", "volcano")
    monkeypatch.delenv("VOLC_ACCESS_KEY", raising=False)
    cfg = load_config(
        output_dir=tmp_path / "out",
        checkpoint_path=tmp_path / "ck.db",
        dotenv_path=tmp_path / "no.env",
    )
    with pytest.raises(MissingConfigError):
        cfg.validate_for_pipeline()


def test_validate_for_pipeline_ok(mock_cfg):
    # mock_cfg 已带齐假 key + cosyvoice
    mock_cfg.validate_for_pipeline()  # 不抛
