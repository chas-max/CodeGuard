from __future__ import annotations

import pytest

from codeguard.config import Settings, load_settings
from codeguard.engine.policy import Policy
from codeguard.errors import ConfigError
from codeguard.github.merge import merge_blockers, outcome_for_blockers
from codeguard.models import (
    MergeMode,
    Outcome,
    ReviewDecision,
    ReviewVerdict,
    SandboxStatus,
    ValidationReport,
)


def test_settings_validation_collects_errors(tmp_path):
    cfg = Settings(data_dir=tmp_path, checkpoint_path=tmp_path / "ck.db")
    with pytest.raises(ConfigError):
        cfg.validate(require_github=True)
    good = Settings(
        github_token="t", deepseek_api_key="k",
        data_dir=tmp_path, checkpoint_path=tmp_path / "ck.db",
    )
    good.validate(require_github=True)


def test_merge_blockers_sha_and_high_risk(tmp_path):
    policy = Policy()
    validation = ValidationReport(
        status=SandboxStatus.passed, repro_passed=True, regression_passed=True,
    )
    review = ReviewDecision(verdict=ReviewVerdict.approve)
    blockers = merge_blockers(
        pr_open=True, is_draft=False, mergeable=True,
        head_sha="b" * 40, reviewed_sha="a" * 40,
        validation=validation, review=review,
        changed_paths=["src/calc.py"], policy=policy, merge_mode=MergeMode.auto,
    )
    assert any("SHA" in b for b in blockers)
    assert outcome_for_blockers(blockers) == Outcome.stale

    blockers2 = merge_blockers(
        pr_open=True, is_draft=False, mergeable=True,
        head_sha="b" * 40, reviewed_sha="b" * 40,
        validation=validation, review=review,
        changed_paths=["src/auth/login.py"], policy=policy, merge_mode=MergeMode.auto,
    )
    assert any("高风险" in b for b in blockers2)
    assert outcome_for_blockers(blockers2) == Outcome.blocked_human


def test_load_settings_overrides(tmp_path):
    cfg = load_settings(data_dir=tmp_path, checkpoint_path=tmp_path / "ck.db",
                        sandbox_backend="fake", merge_mode="human")
    assert cfg.sandbox_backend == "fake"
    assert cfg.merge_mode == MergeMode.human
