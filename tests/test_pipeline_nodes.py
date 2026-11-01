"""12 节点 + 7 路由的节点级测试（FakeLLM + 真实微型仓库沙箱）。"""
from __future__ import annotations

from pathlib import Path

from codeguard.state import initial_state
from tests._codeguard_fakes import (
    F2P_OK,
    FIXED_CALC,
    HAPPY_MAPPING,
    PARSE_CLEAR,
    PLAN_OK,
    SUPERVISE_APPROVE,
    SUPERVISE_VERIFY_OK,
    WRITE_OK,
    WRITE_REFLECT_OK,
    make_buggy_repo,
    make_nodes,
)


def _state(tmp_path: Path, **overrides):
    repo = make_buggy_repo(tmp_path)
    st = initial_state("divide_safe(10, 0) 抛 ZeroDivisionError，期望返回 0.0", "job-t", str(repo))
    st.update(overrides)
    return st


# ── ① understand ──


def test_understand_parses_issue(tmp_path: Path):
    nodes = make_nodes(tmp_path, {"understand": [PARSE_CLEAR]})
    st = nodes.understand(_state(tmp_path))
    assert st["status"] == "parsed"
    ic = st["issue_context"]
    assert ic["completeness"] == "clear"
    assert "divide_safe" in " ".join(ic["suspected_modules"])
    assert not st.get("open_issues")
    assert nodes.route_after_understand(st) == "director_plan"


def test_understand_llm_failure_routes_to_resolve(tmp_path: Path):
    nodes = make_nodes(tmp_path, {})  # understand 队列为空 → 调用失败
    st = nodes.understand(_state(tmp_path))
    assert st["open_issues"], "LLM 失败必须产生路由信号"
    assert st["agent_issues"], "审计日志必须留痕"
    assert nodes.route_after_understand(st) == "director_resolve"


# ── ② director_plan ──


def test_director_plan_locates_and_freezes(tmp_path: Path):
    nodes = make_nodes(tmp_path, {"director_plan": [PLAN_OK]})
    st = _state(tmp_path)
    st["issue_context"] = {"title": "t", "symptom": "divide_safe 除零", "completeness": "clear"}
    out = nodes.director_plan(st)
    assert "calc.py" in out["located_files"]
    assert out["symbol_bible"], "符号契约圣经必须冻结"
    assert any(s["symbol_name"] == "divide_safe" for s in out["symbol_bible"].values())
    assert out["patch_units"][0]["target_file"] == "calc.py"
    assert out["p2p_tests"] == ["test_calc.py"]  # 既有测试进入 P2P 门控


# ── ③ write_draft ──


def test_write_draft_generates_patches(tmp_path: Path):
    nodes = make_nodes(tmp_path, {
        "write_outline": ['{"outline": "o"}'],
        "write": [WRITE_OK],
        "write_reflect": [WRITE_REFLECT_OK],
    })
    st = _state(tmp_path, patch_units=[{
        "unit_id": "U001", "target_file": "calc.py", "status": "pending",
        "issue_hypothesis": "缺除零保护",
    }])
    out = nodes.write_draft(st)
    u = out["patch_units"][0]
    assert "if b == 0" in u["patched_code"]
    assert u["git_diff"].startswith("---")
    assert out["status"] == "writing"


def test_machine_review_gate(tmp_path: Path):
    nodes = make_nodes(tmp_path, {})
    bible = {"SYM_001": {
        "symbol_id": "SYM_001", "file_path": "calc.py", "symbol_name": "divide_safe",
        "arg_names": ["a", "b"], "frozen_signature": True,
    }}
    # 通过路径
    st = _state(tmp_path, symbol_bible=bible, patch_units=[{
        "unit_id": "U001", "target_file": "calc.py", "status": "pending",
        "patched_code": FIXED_CALC,
    }])
    out = nodes.machine_review(st)
    assert out["static_verdict"]["passed"] is True
    assert nodes.route_after_machine(out) == "director_supervise"

    # 签名破坏拦截
    broken = "def divide_safe(a):\n    return a\n"
    st2 = _state(tmp_path, symbol_bible=bible, patch_units=[{
        "unit_id": "U001", "target_file": "calc.py", "status": "pending",
        "patched_code": broken,
    }])
    out2 = nodes.machine_review(st2)
    assert out2["static_verdict"]["passed"] is False
    assert out2["static_verdict"]["category"] == "signature_broken"
    assert out2["machine_revision_count"] == 1
    assert out2["patch_units"][0]["status"] == "failed"
    assert nodes.route_after_machine(out2) == "write_draft"

    # 语法错误拦截
    st3 = _state(tmp_path, symbol_bible=bible, patch_units=[{
        "unit_id": "U001", "target_file": "calc.py", "status": "pending",
        "patched_code": "def divide_safe(a, b):\n    return a +\n",
    }])
    out3 = nodes.machine_review(st3)
    assert out3["static_verdict"]["category"] == "syntax_error"


def test_machine_review_cap_releases_to_supervise(tmp_path: Path):
    nodes = make_nodes(tmp_path, {})
    st = _state(tmp_path, machine_revision_count=3, patch_units=[{
        "unit_id": "U001", "target_file": "calc.py", "status": "failed", "patched_code": "x = 1\n",
    }])
    out = nodes.machine_review(st)
    assert nodes.route_after_machine(out) == "director_supervise"  # 封顶放行交人工


# ── ④ director_supervise ──


def test_director_supervise_two_stage(tmp_path: Path):
    nodes = make_nodes(tmp_path, {
        "review": [SUPERVISE_APPROVE],
        "review_verify": [SUPERVISE_VERIFY_OK],
    })
    st = _state(tmp_path, patch_units=[{
        "unit_id": "U001", "target_file": "calc.py", "status": "pending",
        "patched_code": FIXED_CALC,
    }])
    out = nodes.director_supervise(st)
    assert out["director_decision"] == "approve"
    assert nodes.route_after_supervise(out) == "human_review"


# ── ⑤ build_anchors：F2P Red 锚定 ──


def test_build_anchors_red_verified(tmp_path: Path):
    nodes = make_nodes(tmp_path, {"f2p_writer": [F2P_OK]})
    st = _state(tmp_path, patch_units=[{
        "unit_id": "U001", "target_file": "calc.py", "status": "pending",
        "patched_code": FIXED_CALC,
    }])
    out = nodes.build_anchors(st)
    assert out["f2p_tests"] == ["test_repro_issue.py::test_bug_repro"]
    assert out["f2p_test_code"], "F2P 测试代码必须存入 state 供沙箱使用"
    assert "open_issues" not in out or not out.get("open_issues")


# ── ⑥ sandbox_execute：ReAct 自愈 ──


def test_sandbox_execute_happy_path(tmp_path: Path):
    nodes = make_nodes(tmp_path, {})
    st = _state(tmp_path,
                patch_units=[{
                    "unit_id": "U001", "target_file": "calc.py", "status": "pending",
                    "patched_code": FIXED_CALC, "attempts": 0,
                }],
                f2p_tests=["test_repro_issue.py::test_bug_repro"],
                f2p_test_file="test_repro_issue.py",
                f2p_test_code=(
                    "from calc import divide_safe\n\n\n"
                    "def test_bug_repro():\n    assert divide_safe(10, 0) == 0.0\n"
                ),
                p2p_tests=["test_calc.py"])
    out = nodes.sandbox_execute(st)
    assert out["status"] == "done"
    assert out["patch_units"][0]["status"] == "passed"
    patch = Path(out["patch_path"]).read_text(encoding="utf-8")
    assert "+++ b/calc.py" in patch
    assert "+++ b/test_repro_issue.py" in patch  # 单测一并交付
    assert Path(out["coverage_report_path"]).is_file()
    assert nodes.route_after_execute(out) == "__end__"
    # 宿主仓库未被污染
    assert "if b == 0" not in (Path(st["repo_dir"]) / "calc.py").read_text(encoding="utf-8")


def test_sandbox_execute_react_heal(tmp_path: Path):
    """第一轮补丁错误 → Think 分析 Traceback → 第二轮自愈通过。"""
    wrong_code = "def divide_safe(a: float, b: float) -> float:\n    return a / b if b else 1.0\n"
    nodes = make_nodes(tmp_path, {"produce_react": [
        '{"analysis": "除零分支返回值错误", "patched_code": "%s"}' % FIXED_CALC.replace("\n", "\\n"),
    ]})
    st = _state(tmp_path,
                patch_units=[{
                    "unit_id": "U001", "target_file": "calc.py", "status": "pending",
                    "patched_code": wrong_code, "attempts": 0,
                }],
                f2p_tests=["test_repro_issue.py::test_bug_repro"],
                f2p_test_file="test_repro_issue.py",
                f2p_test_code=(
                    "from calc import divide_safe\n\n\n"
                    "def test_bug_repro():\n    assert divide_safe(10, 0) == 0.0\n"
                ),
                p2p_tests=["test_calc.py"])
    out = nodes.sandbox_execute(st)
    assert out["status"] == "done"
    u = out["patch_units"][0]
    assert u["attempts"] == 2  # 首次失败 + Think 重写后通过
    assert u["status"] == "passed"
    assert "if b == 0" in u["patched_code"]


def test_sandbox_execute_heal_exhausted(tmp_path: Path):
    """自愈封顶：produce_react 一直返回错误补丁 → 轮次失败并上报。"""
    bad_code = "def divide_safe(a: float, b: float) -> float:\n    return 1.0\n"
    bad_json = '{"analysis": "x", "patched_code": "%s"}' % bad_code.replace("\n", "\\n")
    nodes = make_nodes(tmp_path, {"produce_react": [bad_json, bad_json]})
    st = _state(tmp_path,
                patch_units=[{
                    "unit_id": "U001", "target_file": "calc.py", "status": "pending",
                    "patched_code": bad_code, "attempts": 0,
                }],
                f2p_tests=["test_repro_issue.py::test_bug_repro"],
                f2p_test_file="test_repro_issue.py",
                f2p_test_code=(
                    "from calc import divide_safe\n\n\n"
                    "def test_bug_repro():\n    assert divide_safe(10, 0) == 0.0\n"
                ),
                p2p_tests=["test_calc.py"])
    out = nodes.sandbox_execute(st)
    # 第一轮自愈耗尽：轮次失败但还有第二轮机会（max_prod=2）
    assert out["status"] == "executing"
    assert out["production_attempt_count"] == 1
    assert out["patch_units"][0]["status"] == "failed"
    assert out["patch_units"][0]["attempts"] == 3  # 首次 + 2 次 Think 重写
    assert nodes.route_after_execute(out) == "sandbox_execute"  # 进入第二轮


# ── ⑦ director_resolve：两处修复的回归测试 ──


def test_director_resolve_clears_open_issues(tmp_path: Path):
    """修复①回归：处理完成后 open_issues 必须真正清空（而非 append 空列表无效操作）。"""
    mapping = {"director_resolve": [
        '{"action": "retry", "target_node": "write_draft", "strategy": "重新生成补丁"}'
    ]}
    nodes = make_nodes(tmp_path, mapping)
    st = _state(tmp_path,
                open_issues=[{"agent": "write_draft", "issue_type": "llm_error", "detail": "x"}],
                status="writing")
    out = nodes.director_resolve(st)
    assert out["open_issues"] == []  # 真正清空
    assert out["director_resolve_count"] == 1
    assert out["director_resolution"]["action"] == "retry"
    assert nodes.route_after_resolve(out) == "write_draft"


def test_director_resolve_hard_cap_escalates(tmp_path: Path):
    """修复②回归：resolve_count >= max_resolve 时不调 LLM 直接 escalate。"""
    nodes = make_nodes(tmp_path, {})  # 无任何 LLM 响应：若调用了会抛错导致测试失败
    st = _state(tmp_path, director_resolve_count=3,
                open_issues=[{"agent": "x", "issue_type": "llm_error", "detail": "y"}])
    out = nodes.director_resolve(st)
    assert out["status"] == "failed"
    assert out["director_resolution"]["action"] == "escalate"
    assert nodes.route_after_resolve(out) == "escalate"


def test_route_after_resolve_skip_status_map(tmp_path: Path):
    nodes = make_nodes(tmp_path, {})
    st = _state(tmp_path, director_resolution={"action": "skip", "target_node": ""},
                status="executing")
    assert nodes.route_after_resolve(st) == "sandbox_execute"
    st2 = _state(tmp_path, director_resolution={"action": "retry", "target_node": "sandbox_execute"})
    assert nodes.route_after_resolve(st2) == "sandbox_execute"


# ── ⑧ escalate ──


def test_escalate_node(tmp_path: Path):
    nodes = make_nodes(tmp_path, {})
    st = _state(tmp_path)
    out = nodes.escalate(st)
    assert out["status"] == "failed"
    assert out["error"]
