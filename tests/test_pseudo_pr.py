"""伪 PR 离线用例：不建真 PR、不连 GitHub、不跑容器。

这里是**快速回归**：LLM/沙箱用替身，保证判定与路由本身不变。
真实配置（真 LLM + 真 docker，仍然不关联 GitHub）见 `test_simulate_pr_real.py`。

一次用例 = 一次 PR 审查。PR 是本地模仿出来的（base/head 两个目录 + 伪造的
PullRequestContext），所以断言可以落在「结果」上：该拦的拦住、该放行的真的
走到合并调用。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeguard.config import load_settings
from eval.pseudo_pr import PseudoPR
from eval.simulate_pr import matches, simulate
from tests.fakes import (
    FakeGitHub,
    FakeSandbox,
    RepeatingProvider,
    v2_repeating_map,
)

PROVIDER = v2_repeating_map()

CLEAN = PseudoPR(
    name="clean_helper",
    title="feat: 新增 clamp",
    body="新增 clamp 工具函数",
    head={"src/util.py": "def clamp(v, low, high):\n    return max(low, min(v, high))\n"},
)

SECRET = PseudoPR(
    name="secret_leak",
    head={"src/config.py": "AWS_KEY = 'AKIAABCDEFGHIJKLMNOP'\n"},
    expected_block=True,
    expected_rules=("SEC-001",),
)

TEST_WEAKENED = PseudoPR(
    name="test_weakened",
    head={"tests/test_calc.py": "from src.calc import divide\n\ndef test_ok():\n    pass\n"},
    expected_block=True,
    expected_rules=("TEST-001",),
)

TEST_DELETED = PseudoPR(
    name="test_deleted",
    head={"tests/test_calc.py": None},
    expected_block=True,
    expected_rules=("TEST-001",),
)

API_BREAK = PseudoPR(
    name="api_break",
    head={"src/calc.py": "def divide(a):\n    return a\n"},
    expected_block=True,
    expected_rules=("API-001",),
)


def _cfg(tmp_path: Path):
    return load_settings(
        data_dir=tmp_path / "data",
        checkpoint_path=tmp_path / "ckpt.db",
        sandbox_backend="fake",
        merge_mode="auto",
    )


def _simulate(tmp_path: Path, pseudo: PseudoPR) -> dict:
    return simulate(
        pseudo,
        cfg=_cfg(tmp_path),
        root=tmp_path / pseudo.name,
        llm_provider=RepeatingProvider(PROVIDER),
        github=FakeGitHub(),
        sandbox=FakeSandbox(),
    )


def test_pseudo_pr_只把真正变化的文件算进变更():
    assert CLEAN.changed() == ["src/util.py"]
    assert TEST_DELETED.changed() == ["tests/test_calc.py"]
    assert PseudoPR(name="noop").changed() == []


def test_干净的伪PR走到合并(tmp_path: Path):
    result = _simulate(tmp_path, CLEAN)
    assert result["done"], result["error"]
    assert result["outcome"] == "MERGED", result
    assert result["merge_called"] is True
    assert result["blocking_rules"] == []
    assert matches(CLEAN, result)


@pytest.mark.parametrize("pseudo", [SECRET, TEST_WEAKENED, TEST_DELETED, API_BREAK],
                         ids=lambda p: p.name)
def test_该拦的伪PR没有合并(tmp_path: Path, pseudo: PseudoPR):
    result = _simulate(tmp_path, pseudo)
    assert result["done"], result["error"]
    assert result["merge_called"] is False
    assert result["outcome"] != "MERGED"
    assert result["pushed"] == 0
    assert set(pseudo.expected_rules) <= set(result["blocking_rules"]), result
    assert matches(pseudo, result)


def test_门控失败的伪PR不会误判为通过(tmp_path: Path):
    result = _simulate(tmp_path, SECRET)
    # 期望拦截，但结果说合并了 → 必须判不通过
    forged = {**result, "outcome": "MERGED", "merge_called": True}
    assert matches(SECRET, forged) is False
    # 期望放行，但结果被拦 → 也必须判不通过
    clean = _simulate(tmp_path, CLEAN)
    assert matches(CLEAN, {**clean, "outcome": "CHANGES_REQUESTED", "merge_called": False}) is False
