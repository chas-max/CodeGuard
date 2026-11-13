"""PR 代码审查领域专用内置工具测试。"""
from __future__ import annotations

import json
from pathlib import Path

from codeguard.tools import review_tools
from codeguard.tools.review_tools import (
    AstSymbolsArgs,
    CheckPatchGuardArgs,
    DiffBaseHeadArgs,
    FindSymbolUsagesArgs,
    GetPrMetadataArgs,
    InspectTestsArgs,
    QueryFindingsArgs,
)


def test_ast_symbols_and_traversal_guard(tmp_path: Path):
    src = tmp_path / "app.py"
    src.write_text(
        'class Service:\n    """处理核心逻辑"""\n    def run(self, user_id: int) -> bool:\n        return True\n',
        encoding="utf-8",
    )
    res = review_tools.ast_symbols(tmp_path, AstSymbolsArgs(path="app.py"))
    assert res.ok
    assert "class Service" in res.content
    assert "def Service.run(self, user_id) -> bool" in res.content

    filtered = review_tools.ast_symbols(tmp_path, AstSymbolsArgs(path="app.py", symbol="run"))
    assert filtered.ok
    assert "Service.run" in filtered.content

    denied = review_tools.ast_symbols(tmp_path, AstSymbolsArgs(path="../secret.py"))
    assert not denied.ok
    assert denied.status == "denied"


def test_find_symbol_usages(tmp_path: Path):
    (tmp_path / "mod.py").write_text("def verify_token(tok: str) -> bool:\n    return bool(tok)\n", encoding="utf-8")
    (tmp_path / "api.py").write_text(
        "from mod import verify_token\n\ndef handle():\n    return verify_token('abc')\n",
        encoding="utf-8",
    )
    res = review_tools.find_symbol_usages(tmp_path, FindSymbolUsagesArgs(symbol="verify_token"))
    assert res.ok
    assert "mod.py:1 [def]" in res.content
    assert "api.py:1 [import]" in res.content
    assert "api.py:4 [ref]" in res.content

    invalid = review_tools.find_symbol_usages(tmp_path, FindSymbolUsagesArgs(symbol="bad symbol!"))
    assert invalid.status == "invalid_args"


def test_diff_base_head_with_signature_change(tmp_path: Path):
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir()
    head.mkdir()
    (base / "svc.py").write_text("def compute(x: int) -> int:\n    return x\n", encoding="utf-8")
    (head / "svc.py").write_text("def compute(x: int, strict: bool = True) -> int:\n    return x\n", encoding="utf-8")

    res = review_tools.diff_base_head(head, base, DiffBaseHeadArgs(path="svc.py"))
    assert res.ok
    assert "-def compute(x: int)" in res.content
    assert "+def compute(x: int, strict: bool = True)" in res.content
    assert "Python 公共签名变化" in res.content

    no_base = review_tools.diff_base_head(head, None, DiffBaseHeadArgs(path="svc.py"))
    assert no_base.ok
    assert "未提供 base 分支快照" in no_base.content


def test_get_pr_metadata_and_query_findings():
    meta_res = review_tools.get_pr_metadata(
        {
            "repo": "acme/demo",
            "pr_number": 42,
            "title": "Fix SQL query",
            "body": "Use parameterized cursor",
            "changed_files": ["src/db.py"],
        },
        GetPrMetadataArgs(include_body=True),
    )
    assert meta_res.ok
    parsed = json.loads(meta_res.content)
    assert parsed["pr_number"] == 42
    assert parsed["changed_files"] == ["src/db.py"]

    empty_res = review_tools.get_pr_metadata(None, GetPrMetadataArgs())
    assert empty_res.ok
    assert "未注入 PR 元数据" in empty_res.content

    findings = [
        {"id": "f1", "severity": "CRITICAL", "source": "bandit", "rule_id": "B608", "file": "src/db.py", "line": 12, "title": "SQLi"},
        {"id": "f2", "severity": "MINOR", "source": "ruff", "rule_id": "F401", "file": "src/util.py", "line": 3, "title": "Unused"},
    ]
    q_res = review_tools.query_findings(findings, QueryFindingsArgs(severity="critical"))
    assert q_res.ok
    q_json = json.loads(q_res.content)
    assert q_json["matched"] == 1
    assert q_json["items"][0]["id"] == "f1"


def test_inspect_tests_and_check_patch_guard(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login():\n    return True\n", encoding="utf-8")
    (tmp_path / "tests" / "test_auth.py").write_text(
        "def test_login_ok():\n    assert True\n\ndef test_login_fail():\n    assert True\n",
        encoding="utf-8",
    )
    test_res = review_tools.inspect_tests(tmp_path, InspectTestsArgs(path="src/auth.py"))
    assert test_res.ok
    assert "test_login_ok" in test_res.content
    assert "test_login_fail" in test_res.content

    good_patch = "--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1,2 +1,2 @@\n def login():\n-    return True\n+    return False\n"
    ok_guard = review_tools.check_patch_guard(tmp_path, ["src/auth.py"], CheckPatchGuardArgs(patch=good_patch))
    assert ok_guard.ok
    assert json.loads(ok_guard.content)["ok"] is True

    test_patch = "--- a/tests/test_auth.py\n+++ b/tests/test_auth.py\n@@ -1 +1 @@\n-def test_login_ok():\n+def test_hack():\n"
    bad_guard = review_tools.check_patch_guard(tmp_path, ["src/auth.py", "tests/test_auth.py"], CheckPatchGuardArgs(patch=test_patch))
    assert bad_guard.ok
    assert json.loads(bad_guard.content)["ok"] is False
