from __future__ import annotations

from pathlib import Path

from codeguard.engine.api_contract import check_api_contract
from codeguard.engine.diff import apply_patch, patch_paths, tree_hash, unified_diff
from codeguard.engine.hybrid import build_hybrid_report
from codeguard.engine.policy import Policy, load_policy_text
from codeguard.engine.repo_map import build_repo_map
from codeguard.engine.rules import RuleEngine
from codeguard.engine.static_scan import scan_tree
from codeguard.engine.tests_guard import guard_tests
from codeguard.models import DeterministicReport, SemanticReport


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "calc.py").write_text(
        "def divide(a, b):\n    return a / b\n", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(
        "from src.calc import divide\n\ndef test_ok():\n    assert divide(4, 2) == 2\n",
        encoding="utf-8")
    return root


def test_apply_patch_accepts_lf_patch_on_crlf_file(tmp_path: Path):
    root = tmp_path / "crlf"
    root.mkdir()
    target = root / "calc.py"
    target.write_bytes(b"def divide(a, b):\r\n    return a / b\r\n")
    before = "def divide(a, b):\n    return a / b\n"
    after = "def divide(a, b):\n    if b == 0:\n        return 0.0\n    return a / b\n"
    patch = unified_diff(before, after, "calc.py")
    ok, output = apply_patch(root, patch, check_only=True)
    assert ok, output
    ok, output = apply_patch(root, patch)
    assert ok, output
    assert b"return 0.0" in target.read_bytes()


def test_repo_map_and_static(tmp_path: Path):
    root = _tree(tmp_path)
    repo_map = build_repo_map(root)
    assert "src/calc.py" in {f["path"] for f in repo_map["files"]}
    report = scan_tree(root, ["src/calc.py"])
    assert report.findings == []
    (root / "src" / "bad.py").write_text("def x(:\n", encoding="utf-8")
    report = scan_tree(root, ["src/bad.py"])
    assert any(f.rule_id == "SYN-001" for f in report.findings)


def test_secret_and_dangerous_call(tmp_path: Path):
    root = _tree(tmp_path)
    (root / "src" / "evil.py").write_text(
        "KEY = 'AKIAABCDEFGHIJKLMNOP'\n\ndef run(x):\n    return eval(x)\n",
        encoding="utf-8",
    )
    report = scan_tree(root, ["src/evil.py"])
    ids = {f.rule_id for f in report.findings}
    assert "SEC-001" in ids and "SEC-002" in ids


def test_rules_protected_and_size(tmp_path: Path):
    _tree(tmp_path)
    policy = Policy()
    engine = RuleEngine(policy)
    findings = engine.evaluate(
        {}, [{"path": ".github/workflows/ci.yml"}, {"path": "src/calc.py"}],
        "diff --git a/x b/x\n+1\n-1\n",
    )
    ids = {f.rule_id for f in findings}
    assert "PATH-001" in ids
    assert "CI-001" in ids


def test_tests_guard_detects_deletion(tmp_path: Path):
    base = _tree(tmp_path)
    head = tmp_path / "head"
    import shutil
    shutil.copytree(base, head)
    (head / "tests" / "test_calc.py").write_text(
        "from src.calc import divide\n\ndef test_ok():\n    pass\n", encoding="utf-8")
    findings = guard_tests(base, head, ["tests/test_calc.py"])
    assert any(f.rule_id == "TEST-001" for f in findings)


def test_api_contract_detects_signature_break(tmp_path: Path):
    base = _tree(tmp_path)
    head = tmp_path / "head"
    import shutil
    shutil.copytree(base, head)
    (head / "src" / "calc.py").write_text(
        "def divide(a):\n    return a\n", encoding="utf-8")
    findings = check_api_contract(base, head, ["src/calc.py"])
    assert findings and findings[0].rule_id == "API-001"


def test_policy_yaml_and_globs():
    p = load_policy_text("""
version: 1
protected_paths: ['.github/workflows/**']
high_risk_paths: ['**/auth/**']
""")
    assert p.is_protected(".github/workflows/ci.yml")
    assert p.is_high_risk("src/auth/login.py")
    assert not p.is_high_risk("src/calc.py")


def test_patch_and_apply(tmp_path: Path):
    root = _tree(tmp_path)
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=False)
    old = (root / "src" / "calc.py").read_text(encoding="utf-8")
    new = "def divide(a, b):\n    if b == 0:\n        return 0\n    return a / b\n"
    patch = unified_diff(old, new, "src/calc.py")
    assert patch_paths(patch) == ["src/calc.py"]
    ok, _ = apply_patch(root, patch, check_only=True)
    assert ok
    ok, _ = apply_patch(root, patch)
    assert ok
    assert "if b == 0" in (root / "src" / "calc.py").read_text(encoding="utf-8")
    assert tree_hash(root)


def test_hybrid_report():
    det = DeterministicReport()
    sem = SemanticReport()
    report = build_hybrid_report(det, sem)
    assert report.findings == []
