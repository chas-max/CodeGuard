"""测试隔离：每个用例在独立的临时工作目录里运行。

避免 e2e 把 `reports/`、`checkpoints.db`、`data/` 写进仓库（job record 是运行产物，
不属于测试夹具）。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
