"""真实配置的伪 PR：真 LLM + 真 Docker 沙箱，唯一不真实的是 GitHub。

没有 API key、没有 Docker daemon 或没有沙箱镜像时自动 skip。
`eval/simulate_pr.py` 本身默认就走这套配置，这里只是把最关键的一条链路
固化成可回归的用例。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import load_dotenv

from codeguard.config import load_settings
from codeguard.sandbox.health import check_docker, check_image
from eval.pseudo_pr import PseudoPR
from eval.simulate_pr import simulate

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[1]

REAL_CASE = PseudoPR(
    name="real_change",
    title="feat: 新增 clamp",
    body="新增 clamp(v, low, high) 工具函数，并补充单测。",
    head={
        "src/util.py": (
            "def clamp(v, low, high):\n"
            '    """把 v 限制在 [low, high]。"""\n'
            "    return max(low, min(v, high))\n"
        ),
        "tests/test_util.py": (
            "from src.util import clamp\n\n"
            "def test_clamp_above():\n    assert clamp(5, 0, 3) == 3\n\n"
            "def test_clamp_inside():\n    assert clamp(1, 0, 3) == 1\n"
        ),
    },
)


def _real_cfg(tmp_path: Path):
    load_dotenv(REPO_ROOT / ".env", override=False)
    cfg = load_settings(data_dir=tmp_path / "data", checkpoint_path=tmp_path / "ckpt.db")
    ok, message = check_docker()
    if not ok:
        pytest.skip(f"Docker 不可用: {message}")
    image_ok, image_msg = check_image(cfg.sandbox_image)
    if not image_ok:
        pytest.skip(f"沙箱镜像不存在: {image_msg}")
    if not (cfg.deepseek_api_key and cfg.dashscope_api_key):
        pytest.skip("需要 DEEPSEEK_API_KEY 与 DASHSCOPE_API_KEY（各角色 provider 不同）")
    return cfg


def test_真实沙箱与真实LLM跑完一次伪PR(tmp_path: Path):
    cfg = _real_cfg(tmp_path)
    result = simulate(REAL_CASE, cfg=cfg, root=tmp_path / "real_change")
    assert result["done"], result["error"]
    assert result["outcome"] != "INFRA_FAILED", result
    # 真 docker：DEFAULT_FILES 里的 tests/test_calc.py 在 head 上应当通过
    assert result["validation"] == "passed", result
    # 真 LLM：确实发生了调用并采集到成本
    assert result["llm_calls"] > 0, result
