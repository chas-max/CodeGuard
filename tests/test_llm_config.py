"""LLM 角色表、成本采集、JSON 解析与 CodeGuardConfig 配置测试。"""
from __future__ import annotations

import pytest

from codeguard.config import load_config
from codeguard.errors import MissingConfigError, NonRetryableError
from codeguard.llm import (
    ROLE_MODELS,
    estimate_llm_cost,
    make_llm,
    parse_json_loose,
    role_model,
)


# ---- llm 角色 ----


def test_role_models_cover_pipeline_roles():
    required = {
        "understand", "understand_summary", "director_plan", "director_resolve",
        "write", "write_outline", "write_reflect", "review", "review_verify",
        "f2p_writer", "produce_react",
    }
    assert required <= set(ROLE_MODELS)
    # fallback 角色保留
    assert "director_plan_fallback" in ROLE_MODELS
    assert "write_fallback" in ROLE_MODELS


def test_role_model_unknown_raises():
    with pytest.raises(ValueError):
        role_model("no_such_role")


def test_make_llm_has_request_timeout(monkeypatch):
    """修复原系统 P0：ChatOpenAI 必须带 request_timeout，防止单请求无限挂起。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    cfg = load_config()
    llm = make_llm("understand", cfg, temperature=0.2)
    assert llm.request_timeout == cfg.request_timeout_sec == 120
    assert llm.max_retries == 0


# ---- 成本估算 ----


def test_estimate_llm_cost():
    cost = estimate_llm_cost("deepseek-chat", 1000, 2000)
    assert cost == pytest.approx(0.001 * 1 + 0.002 * 2)
    assert estimate_llm_cost("unknown-model", 100, 100) == 0.0


# ---- JSON 宽松解析 ----


def test_parse_json_loose_variants():
    assert parse_json_loose('{"a": 1}') == {"a": 1}
    assert parse_json_loose('前言```json\n{"a": 2}\n```后记') == {"a": 2}
    assert parse_json_loose('说明如下 {"a": {"b": 3}} 完毕') == {"a": {"b": 3}}
    with pytest.raises(NonRetryableError):
        parse_json_loose("完全不是JSON")
    with pytest.raises(NonRetryableError):
        parse_json_loose("")


# ---- 配置 ----


def test_load_config_defaults(monkeypatch):
    for k in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MIMO_API_KEY",
              "MAX_CLARIFY", "SANDBOX_TIMEOUT_SEC"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.max_clarify == 5
    assert cfg.max_machine == 3
    assert cfg.max_human == 3
    assert cfg.max_anchor == 2
    assert cfg.max_prod == 2
    assert cfg.max_resolve == 3
    assert cfg.sandbox_timeout_sec == 30
    assert cfg.per_unit_attempts == 2
    assert cfg.topk_files == 3
    assert cfg.coverage_min_pct == 0.0
    assert cfg.budget_max_cny == 20.0


def test_validate_for_pipeline_fail_fast(monkeypatch):
    for k in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    with pytest.raises(MissingConfigError):
        cfg.validate_for_pipeline()
