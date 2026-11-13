"""分层评测用例生成器：测试集是数据，生成器是代码。

生成物 `eval/datasets/core/cases.core.jsonl` 提交仓库，可 diff 审阅；同 seed 同输出，
新增场景只加生成规则，不改执行器与评分卡。

覆盖：
- `det`：13 个确定性 rule_id 的正例、负例、边界（`TEST-001` 覆盖 5 种子语义）；
- `semantic`：确定性规则看不见的注入缺陷（空值、off-by-one、吞异常、资源泄漏…）；
- `gate`：tier 分级、审批绑定、merge gate 放行/拦截；
- `security`：沙箱隔离不变量、工具能力收敛、路径穿越、审批伪造；
- `e2e`：全链路（fake LLM + fake 沙箱）放行与拦截。
"""
from __future__ import annotations

import argparse
import json

from .loader import DEFAULT_SUITE, CaseSpec, write_suite

CALC = "def divide(a, b):\n    return a / b\n"
CALC_TEST = (
    "from src.calc import divide\n\n"
    "def test_ok():\n"
    "    assert divide(4, 2) == 2\n"
)
CLEAN_BASE: dict[str, str] = {
    "src/calc.py": CALC,
    "tests/test_calc.py": CALC_TEST,
}
RULE_IDS = (
    "SYN-001", "SEC-001", "SEC-002", "ENV-001", "SIZE-001",
    "PATH-001", "PATH-002", "CI-001", "DEP-001", "POL-001",
    "SCOPE-001", "API-001", "TEST-001",
)


def _case(
    case_id: str,
    *,
    layer: str = "det",
    base: dict[str, str] | None = None,
    head: dict[str, str | None] | None = None,
    block: bool = False,
    rules: tuple[str, ...] = (),
    semantic: bool = False,
    tier: str = "",
    approvals: int | None = None,
    weight: float = 1.0,
    tags: tuple[str, ...] = (),
    notes: str = "",
    probe: str = "",
    probe_args: dict | None = None,
    profile: str = "change",
) -> CaseSpec:
    base_files = {**CLEAN_BASE, **(base or {})}
    return CaseSpec(
        id=case_id.lower(),
        layer=layer,
        base=base_files,
        head=dict(head or {}),
        expect_block=block,
        expect_rules=rules,
        expect_semantic=semantic,
        expect_tier=tier,
        expect_approvals=approvals,
        weight=weight,
        tags=tags,
        notes=notes,
        probe=probe,
        probe_args=dict(probe_args or {}),
        profile=profile,
    )


def _lines(count: int, *, prefix: str = "VALUE") -> str:
    return "".join(f"{prefix}_{i} = {i}\n" for i in range(count))


# ────────────────────────── det：确定性规则矩阵 ──────────────────────────


def det_cases() -> list[CaseSpec]:
    cases: list[CaseSpec] = []

    # SYN-001：语法错误 / 未解决冲突标记
    for path in ("src/broken.py", "pkg/deep/broken.py", "app/calc.py"):
        cases.append(_case(
            f"det.syn001.syntax.{path.replace('/', '_')}",
            head={path: "def broken(a, b:\n    return a\n"},
            block=True, rules=("SYN-001",), tags=("syntax",),
        ))
    cases.append(_case(
        "det.syn001.conflict.python",
        head={"src/conflict.py": "def f():\n<<<<<<< HEAD\n    return 1\n=======\n    return 2\n>>>>>>> feature\n"},
        block=True, rules=("SYN-001",), tags=("conflict",),
    ))
    cases.append(_case(
        "det.syn001.conflict.markdown",
        head={"docs/notes.md": "intro\n<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> b\n"},
        block=True, rules=("SYN-001",), tags=("conflict",),
    ))
    cases.append(_case(
        "det.syn001.negative.valid",
        head={"src/fine.py": "def add(a, b):\n    return a + b\n"},
        block=False, tags=("negative",),
    ))

    # SEC-001：secret 命中（内容隐藏，仍然 blocking）
    secrets = {
        "aws": "KEY = 'AKIAABCDEFGHIJKLMNOP'\n",
        "private_key": "PEM = '''-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----'''\n",
        "github": "TOKEN = 'ghp_" + "a" * 36 + "'\n",
        "generic": "password = 'super-secret-value-1234'\n",
    }
    for kind, content in secrets.items():
        for root in ("src", "deploy"):
            cases.append(_case(
                f"det.sec001.{kind}.{root}",
                head={f"{root}/settings_{kind}.py": content},
                block=True, rules=("SEC-001",), tags=("security", "secret"),
            ))
    cases.append(_case(
        "det.sec001.negative.short_value",
        head={"src/settings_short.py": "api_key = 'short'\npassword = 'tiny'\n"},
        block=False, tags=("negative",),
    ))

    # SEC-002：高风险调用
    calls = {
        "eval": "def run(expr):\n    return eval(expr)\n",
        "exec": "def run(code):\n    exec(code)\n",
        "os_system": "import os\n\ndef shell(cmd):\n    return os.system(cmd)\n",
        "subprocess_run": "import subprocess\n\ndef run(cmd):\n    return subprocess.run(cmd)\n",
        "pickle_loads": "import pickle\n\ndef load(raw):\n    return pickle.loads(raw)\n",
        "compile": "def build(src):\n    return compile(src, '<x>', 'exec')\n",
        "import": "def load(name):\n    return __import__(name)\n",
        "os_popen": "import os\n\ndef run(cmd):\n    return os.popen(cmd)\n",
        "marshal": "import marshal\n\ndef load(raw):\n    return marshal.loads(raw)\n",
        "rmtree": "import shutil\n\ndef wipe(path):\n    shutil.rmtree(path)\n",
    }
    for name, content in calls.items():
        for root in ("src", "lib"):
            # 现状：SEC-002 是 major 但 static_scan 只让 critical 阻塞，因此只断言"被检出"。
            cases.append(_case(
                f"det.sec002.{name}.{root}",
                head={f"{root}/danger_{name}.py": content},
                block=False, rules=("SEC-002",), tags=("security", "dangerous_call"),
            ))
    cases.append(_case(
        "det.sec002.negative.safe_call",
        head={"src/safe.py": "def run(expr):\n    return len(expr)\n"},
        block=False, tags=("negative",),
    ))
    cases.append(_case(
        "det.gap.sec002_not_blocking",
        head={"src/danger_gap.py": "def run(expr):\n    return eval(expr)\n"},
        block=True, rules=("SEC-002",), tags=("gap", "severity"),
        notes="gap：SEC-002 severity=major 却默认 non-blocking，危险调用可被放行",
    ))

    # ENV-001：候选工作区不存在（不应被当成"干净"放行）
    cases.append(_case(
        "det.env001.missing_workspace",
        probe="env_missing_workspace",
        block=True, rules=("ENV-001",), tags=("infra",),
    ))

    # SIZE-001：文件数 / 改动行 / diff 字节
    many = {f"src/mod_{i:02d}.py": f"VALUE = {i}\n" for i in range(51)}
    cases.append(_case(
        "det.size001.files_over_limit",
        head=many, block=True, rules=("SIZE-001", "SCOPE-001"),
        tags=("limit",),
    ))
    cases.append(_case(
        "det.size001.lines_over_limit",
        head={"src/big.py": _lines(3001)}, block=True, rules=("SIZE-001",),
        tags=("limit",),
    ))
    cases.append(_case(
        "det.size001.diff_bytes_over_limit",
        probe="size_diff_bytes", probe_args={"path": "src/huge.py", "bytes": 2_000_001},
        block=True, rules=("SIZE-001",), tags=("limit",),
    ))

    # PATH-002：敏感/密钥路径
    forbidden = {
        ".env": "DEBUG=1\n",
        ".env.production": "DEBUG=0\n",
        ".env.local": "DEBUG=1\n",
        "deploy/server.pem": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        "id_rsa": "ssh-rsa AAAAB3NzaC1yc2E\n",
        "ssh/id_ed25519": "ssh-ed25519 AAAAC3Nza\n",
        "certs/client.key": "not-a-real-key\n",
        "secrets.yaml": "token: short\n",
        "deploy/secrets.json": "{}\n",
    }
    for path, content in forbidden.items():
        tags = ("path",) if path != "ssh/id_ed25519" else ("path", "gap")
        cases.append(_case(
            f"det.path002.{path.replace('/', '_').replace('.', '-')}",
            head={path: content}, block=True, rules=("PATH-002",), tags=tags,
        ))
    cases.append(_case(
        "det.path002.negative.env_loader",
        head={"src/env_loader.py": "def load_env(path):\n    return path\n"},
        block=False, tags=("negative",),
    ))

    # PATH-001：受保护路径（CODEOWNERS / .github / .codeguard）
    cases.append(_case(
        "det.path001.codeowners",
        head={"CODEOWNERS": "* @acme/core\n"}, block=True, rules=("PATH-001",),
    ))
    cases.append(_case(
        "det.path001.workflow",
        head={".github/workflows/deploy.yml": "name: deploy\non: push\n"},
        block=True, rules=("PATH-001", "CI-001"),
    ))
    cases.append(_case(
        "det.path001.codeguard_policy",
        head={".codeguard/policy.yml": "version: 1\n"},
        block=True, rules=("PATH-001", "POL-001"),
    ))
    cases.append(_case(
        "det.path001.negative.owner_module",
        head={"src/owner.py": "OWNER = 'core'\n"}, block=False, tags=("negative",),
    ))

    # CI-001：CI/构建配置
    ci_files = {
        ".gitlab-ci.yml": "stages: [test]\n",
        "Jenkinsfile": "pipeline { agent any }\n",
        "azure-pipelines.yml": "trigger: [main]\n",
        ".circleci/config.yml": "version: 2\n",
        ".github/workflows/test.yml": "name: test\n",
    }
    for path, content in ci_files.items():
        rules = ("CI-001", "PATH-001") if path.startswith(".github/") else ("CI-001",)
        cases.append(_case(
            f"det.ci001.{path.replace('/', '_').replace('.', '-')}",
            head={path: content}, block=True, rules=rules, tags=("ci",),
        ))
    cases.append(_case(
        "det.ci001.negative.issue_template",
        head={".github/ISSUE_TEMPLATE/bug.md": "# bug\n"}, block=False,
        tags=("negative",),
    ))

    # DEP-001：依赖/lockfile
    dep_files = {
        "requirements.txt": "requests==2.32.0\n",
        "requirements-dev.txt": "pytest==8.0.0\n",
        "package.json": '{"name": "demo"}\n',
        "poetry.lock": "# lock\n",
        "go.mod": "module example.com/demo\n",
        "setup.py": "from setuptools import setup\nsetup(name='demo')\n",
        "Cargo.toml": "[package]\nname = 'demo'\n",
        "pom.xml": "<project/>\n",
        "yarn.lock": "# lock\n",
    }
    for path, content in dep_files.items():
        cases.append(_case(
            f"det.dep001.{path.replace('/', '_').replace('.', '-')}",
            head={path: content}, block=True, rules=("DEP-001",), tags=("dependency",),
        ))
    cases.append(_case(
        "det.dep001.negative.module",
        head={"src/deps.py": "DEPS = ()\n"}, block=False, tags=("negative",),
    ))

    # POL-001：PR 修改策略
    cases.append(_case(
        "det.pol001.policy_yml",
        head={".codeguard/policy.yml": "version: 1\nmode: internal\n"},
        block=True, rules=("POL-001", "PATH-001"),
    ))
    cases.append(_case(
        "det.pol001.review_md",
        head={".codeguard/review.md": "# 放宽要求\n"}, block=True,
        rules=("POL-001", "PATH-001"),
    ))

    # SCOPE-001：大范围非测试改动（minor，不阻塞）
    cases.append(_case(
        "det.scope001.many_non_tests",
        head={f"src/scope_{i}.py": f"VALUE = {i}\n" for i in range(12)},
        block=False, rules=("SCOPE-001",), tags=("scope",),
    ))
    cases.append(_case(
        "det.scope001.boundary_nine_files",
        head={f"src/scope_{i}.py": f"VALUE = {i}\n" for i in range(9)},
        block=False, tags=("boundary",),
    ))
    cases.append(_case(
        "det.scope001.mixed_tests",
        head={
            **{f"src/mixed_{i}.py": f"VALUE = {i}\n" for i in range(8)},
            **{f"tests/test_mixed_{i}.py": "def test_ok():\n    assert True\n" for i in range(4)},
        },
        block=False, tags=("boundary",),
    ))

    # API-001：公共符号删除 / 签名破坏
    cases.append(_case(
        "det.api001.symbol_removed",
        head={"src/calc.py": "def other(a, b):\n    return a + b\n"},
        block=True, rules=("API-001",),
    ))
    cases.append(_case(
        "det.api001.signature_changed",
        head={"src/calc.py": "def divide(a, b, c):\n    return a / b\n"},
        block=True, rules=("API-001",),
    ))
    cases.append(_case(
        "det.gap.api001_kwonly_ignored",
        head={"src/calc.py": "def divide(a, b, *, strict=False):\n    return a / b\n"},
        block=True, rules=("API-001",), tags=("gap",),
        notes="gap：api_contract 只比较 positional args，kw-only/默认值变更不会被识别",
    ))
    cases.append(_case(
        "det.api001.class_removed",
        base={"src/models.py": "class Widget:\n    pass\n"},
        head={"src/models.py": None},
        block=True, rules=("API-001",),
    ))
    cases.append(_case(
        "det.api001.async_signature_changed",
        base={"src/async_api.py": "async def fetch(url):\n    return url\n"},
        head={"src/async_api.py": "async def fetch(url, timeout=1):\n    return url\n"},
        block=True, rules=("API-001",),
    ))
    cases.append(_case(
        "det.api001.negative.add_symbol",
        head={"src/calc.py": CALC + "\ndef multiply(a, b):\n    return a * b\n"},
        block=False, tags=("negative",),
    ))

    # TEST-001：五种子语义 × 三条测试路径
    test_paths = ("tests/test_calc.py", "pkg/tests/test_math.py", "src/tests/test_api.py")
    test_base = (
        "def test_ok():\n"
        "    assert 1 == 1\n"
        "    assert 2 == 2\n"
    )
    for idx, path in enumerate(test_paths):
        base = {path: test_base}
        cases.append(_case(
            f"det.test001.deleted_file.{idx}",
            base=base, head={path: None}, block=True, rules=("TEST-001",),
        ))
        cases.append(_case(
            f"det.test001.removed_function.{idx}",
            base=base,
            head={path: "def test_other():\n    assert 1 == 1\n    assert 2 == 2\n"},
            block=True, rules=("TEST-001",),
        ))
        cases.append(_case(
            f"det.test001.assert_reduced.{idx}",
            base=base,
            head={path: "def test_ok():\n    assert 1 == 1\n"},
            block=True, rules=("TEST-001",),
        ))
        cases.append(_case(
            f"det.test001.new_skip.{idx}",
            base=base,
            head={path: "import pytest\n\n@pytest.mark.skip\ndef test_ok():\n    assert 1 == 1\n    assert 2 == 2\n"},
            block=True, rules=("TEST-001",),
        ))
        cases.append(_case(
            f"det.test001.coverage_lowered.{idx}",
            base={path: test_base + "COVERAGE_MIN_PCT = 80\n"},
            head={path: test_base + "COVERAGE_MIN_PCT = 50\n"},
            block=True, rules=("TEST-001",),
        ))
    cases.append(_case(
        "det.test001.negative.add_test",
        base={"tests/test_calc.py": test_base},
        head={"tests/test_calc.py": test_base + "\ndef test_more():\n    assert 3 == 3\n"},
        block=False, tags=("negative",),
    ))

    # 干净变更（负例）：文档、功能、重构、纯注释
    clean_cases = {
        "docs_guide": {"docs/guide.md": "# 指南\n\n新增说明。\n"},
        "readme": {"README.md": "# CodeGuard\n\n更新。\n"},
        "new_feature": {"src/feature.py": "def feature(x):\n    return x * 2\n"},
        "add_test": {"tests/test_more.py": "def test_more():\n    assert True\n"},
        "comment_only": {"src/calc.py": CALC + "\n# 说明：保持行为不变\n"},
        "changelog": {"CHANGELOG.md": "## 1.0.1\n- 修复\n"},
        "issue_template": {".github/ISSUE_TEMPLATE/feature.md": "# feature\n"},
        "typing_only": {"src/calc.py": "def divide(a: float, b: float) -> float:\n    return a / b\n"},
        "rename_local": {"src/calc.py": "def divide(a, b):\n    result = a / b\n    return result\n"},
        "new_module": {"src/util/strings.py": "def slug(text):\n    return text.strip().lower()\n"},
    }
    gap_negatives = {"typing_only"}
    for name, head in clean_cases.items():
        tags = ("negative", "clean") + (("gap",) if name in gap_negatives else ())
        cases.append(_case(f"det.clean.{name}", head=head, block=False, tags=tags))

    return cases


# ────────────────────────── semantic：规则看不见的缺陷 ──────────────────────────


SEMANTIC_CASES: list[tuple[str, dict[str, str], str, str]] = [
    (
        "null_deref",
        {"src/user.py": "def display(user):\n    return 'anonymous'\n"},
        "src/user.py",
        "def display(user):\n    return user.name\n",
    ),
    (
        "off_by_one",
        {"src/pager.py": "def pages(items, size):\n    return len(items) // size\n"},
        "src/pager.py",
        "def pages(items, size):\n    return len(items) // size + 1 - 1\n",
    ),
    (
        "swallowed_exception",
        {"src/parse.py": "def parse(raw):\n    return int(raw)\n"},
        "src/parse.py",
        "def parse(raw):\n    try:\n        return int(raw)\n    except Exception:\n        pass\n",
    ),
    (
        "resource_leak",
        {"src/read.py": "def read(path):\n    with open(path) as fh:\n        return fh.read()\n"},
        "src/read.py",
        "def read(path):\n    fh = open(path)\n    return fh.read()\n",
    ),
    (
        "mutable_default",
        {"src/bucket.py": "def add(item, bucket=None):\n    bucket = bucket or []\n    bucket.append(item)\n    return bucket\n"},
        "src/bucket.py",
        "def add(item, bucket=[]):\n    bucket.append(item)\n    return bucket\n",
    ),
    (
        "race_counter",
        {"src/counter.py": "import threading\n\nLOCK = threading.Lock()\nCOUNT = 0\n\ndef bump():\n    global COUNT\n    with LOCK:\n        COUNT += 1\n"},
        "src/counter.py",
        "import threading\n\nCOUNT = 0\n\ndef bump():\n    global COUNT\n    COUNT += 1\n",
    ),
    (
        "return_type_mismatch",
        {"src/service.py": "def load():\n    return {'id': 1}\n"},
        "src/service.py",
        "def load():\n    return [1]\n",
    ),
    (
        "unbounded_retry",
        {"src/retry.py": "def retry(fn):\n    return fn()\n"},
        "src/retry.py",
        "def retry(fn):\n    while True:\n        try:\n            return fn()\n        except Exception:\n            continue\n",
    ),
    (
        "float_equality",
        {"src/money.py": "def is_zero(total):\n    return abs(total) < 1e-9\n"},
        "src/money.py",
        "def is_zero(total):\n    return total == 0.3\n",
    ),
    (
        "unbounded_cache",
        {"src/cache.py": "def get(cache, key):\n    return cache.get(key)\n"},
        "src/cache.py",
        "def put(cache, key, value):\n    cache[key] = value\n    return cache\n",
    ),
    (
        "missing_await",
        {"src/job.py": "async def fetch():\n    return 1\n\nasync def run():\n    return await fetch()\n"},
        "src/job.py",
        "async def fetch():\n    return 1\n\nasync def run():\n    return fetch()\n",
    ),
    (
        "sql_injection",
        {"src/db.py": "def query(cursor, value):\n    cursor.execute('SELECT 1')\n    return value\n"},
        "src/db.py",
        "def query(cursor, value):\n    cursor.execute(f'SELECT * FROM t WHERE v = {value}')\n    return value\n",
    ),
    (
        "log_secret",
        {"src/login.py": "def login(user, logger):\n    logger.info('login')\n    return user\n"},
        "src/login.py",
        "def login(user, logger):\n    logger.info('login %s %s', user.name, user.password)\n    return user\n",
    ),
    (
        "division_by_zero",
        {"src/ratio.py": "def ratio(a, b):\n    if b == 0:\n        return 0.0\n    return a / b\n"},
        "src/ratio.py",
        "def ratio(a, b):\n    return a / b\n",
    ),
    (
        "always_success_status",
        {"src/status.py": "def status(ok):\n    return 'ok' if ok else 'failed'\n"},
        "src/status.py",
        "def status(ok):\n    return 'ok'\n",
    ),
    (
        "naive_datetime",
        {"src/stamp.py": "import datetime\n\ndef now():\n    return datetime.datetime.now(datetime.timezone.utc)\n"},
        "src/stamp.py",
        "import datetime\n\ndef now():\n    return datetime.datetime.now()\n",
    ),
    (
        "mutate_during_iteration",
        {"src/filter.py": "def drop(items, target):\n    return [x for x in items if x != target]\n"},
        "src/filter.py",
        "def drop(items, target):\n    for item in items:\n        if item == target:\n            items.remove(item)\n    return items\n",
    ),
    (
        "lock_not_released",
        {"src/lock.py": "import threading\n\nLOCK = threading.Lock()\n\ndef run(fn):\n    with LOCK:\n        return fn()\n"},
        "src/lock.py",
        "import threading\n\nLOCK = threading.Lock()\n\ndef run(fn):\n    LOCK.acquire()\n    return fn()\n",
    ),
    (
        "partial_write",
        {"src/save.py": "def save(path, text):\n    with open(path, 'w') as fh:\n        fh.write(text)\n    return True\n"},
        "src/save.py",
        "def save(path, text):\n    with open(path, 'w') as fh:\n        fh.write(text[:10])\n    return True\n",
    ),
    (
        "cache_key_collision",
        {"src/key.py": "def key(user_id, tenant):\n    return f'{tenant}:{user_id}'\n"},
        "src/key.py",
        "def key(user_id, tenant):\n    return str(user_id)\n",
    ),
]


def semantic_cases() -> list[CaseSpec]:
    cases: list[CaseSpec] = []
    for name, base, path, buggy in SEMANTIC_CASES:
        cases.append(_case(
            f"semantic.{name}",
            layer="semantic",
            base=base,
            head={path: buggy},
            block=True,
            semantic=True,
            tags=("semantic", name),
            notes="确定性规则应当看不见该缺陷，只能靠语义检测召回",
        ))
    safe = {
        "safe_refactor": ("src/refactor.py", "def f(a):\n    return a + 1\n", "def f(a):\n    b = a\n    return b + 1\n"),
        "safe_rename": ("src/rename.py", "def g(x):\n    return x * 2\n", "def g(x):\n    doubled = x * 2\n    return doubled\n"),
        "safe_guard": ("src/guard.py", "def h(v):\n    return v\n", "def h(v):\n    if v is None:\n        return None\n    return v\n"),
        "safe_context": ("src/file.py", "def r(p):\n    return p\n", "def r(p):\n    with open(p) as fh:\n        return fh.read()\n"),
    }
    for name, (path, base, head) in safe.items():
        cases.append(_case(
            f"semantic.negative.{name}", layer="semantic",
            base={path: base}, head={path: head}, block=False, tags=("negative",),
        ))
    return cases


# ────────────────────────── gate：tier / 审批 / 合入门控 ──────────────────────────

SHA = "a" * 40
TREE = "tree-1"


def gate_cases() -> list[CaseSpec]:
    cases: list[CaseSpec] = []

    def tier(case_id: str, *, paths: list[str], lines: int, expected: str,
             approvals: int, author: str = "alice", body: str = "", labels: list[str] | None = None,
             policy: dict | None = None, api_break: bool = False, tags: tuple[str, ...] = ()) -> None:
        cases.append(_case(
            case_id, layer="gate", tier=expected, approvals=approvals, tags=("tier",) + tags,
            probe="tier",
            probe_args={
                "changed_paths": paths, "changed_lines": lines, "author": author,
                "pr_body": body, "labels": labels or [], "policy": policy or {},
                "api_break": api_break,
            },
        ))

    trusted = {"trusted_authors": ["alice"]}
    tier("gate.tier.docs_trusted", paths=["docs/a.md"], lines=50, expected="T0",
         approvals=1, policy=trusted)
    tier("gate.tier.docs_untrusted", paths=["docs/a.md"], lines=50, expected="T1",
         approvals=1, author="bob", policy=trusted)
    tier("gate.tier.docs_over_limit", paths=["docs/a.md"], lines=201, expected="T1",
         approvals=1, policy=trusted)
    tier("gate.tier.src_default", paths=["src/a.py"], lines=10, expected="T1", approvals=1)
    tier("gate.tier.auth_path", paths=["src/auth/login.py"], lines=10, expected="T2",
         approvals=2, tags=("high_risk",))
    tier("gate.tier.billing_path", paths=["src/billing/invoice.py"], lines=10,
         expected="T2", approvals=2, tags=("high_risk",))
    tier("gate.tier.pyproject", paths=["pyproject.toml"], lines=5, expected="T2", approvals=2)
    tier("gate.tier.workflow", paths=[".github/workflows/ci.yml"], lines=5, expected="T2", approvals=2)
    tier("gate.tier.large_change", paths=["src/a.py"], lines=1000, expected="T2", approvals=2)
    tier("gate.tier.large_change_boundary", paths=["src/a.py"], lines=999, expected="T1", approvals=1)
    tier("gate.tier.agent_authored_docs", paths=["docs/a.md"], lines=20, expected="T1",
         approvals=1, body="Co-authored-by: Claude <noreply@anthropic.com>", policy=trusted,
         tags=("agent",))
    tier("gate.tier.agent_label", paths=["docs/a.md"], lines=20, expected="T1",
         approvals=1, labels=["ai-generated"], policy=trusted, tags=("agent",))
    tier("gate.tier.api_break", paths=["src/a.py"], lines=20, expected="T1",
         approvals=1, api_break=True)
    tier("gate.tier.docs_and_src", paths=["docs/a.md", "src/a.py"], lines=20,
         expected="T1", approvals=1, policy=trusted)

    # 审批绑定：sha / tree_hash / 去重
    def approvals(case_id: str, items: list[dict], expected: int, *,
                  head_sha: str = SHA, tree: str = TREE, tags: tuple[str, ...] = ()) -> None:
        cases.append(_case(
            case_id, layer="gate", approvals=expected, tags=("approval",) + tags,
            probe="approvals",
            probe_args={"approvals": items, "head_sha": head_sha, "current_tree_hash": tree},
        ))

    approvals("gate.approval.valid", [{"approver": "bob", "sha": SHA, "tree_hash": TREE}], 1)
    approvals("gate.approval.duplicate",
              [{"approver": "bob", "sha": SHA, "tree_hash": TREE},
               {"approver": "BOB", "sha": SHA, "tree_hash": TREE}], 1)
    approvals("gate.approval.stale_sha", [{"approver": "bob", "sha": "b" * 40, "tree_hash": TREE}], 0)
    approvals("gate.approval.tree_mismatch",
              [{"approver": "bob", "sha": SHA, "tree_hash": "tree-old"}], 0)
    approvals("gate.approval.empty_approver", [{"approver": " ", "sha": SHA, "tree_hash": TREE}], 0)
    approvals("gate.approval.legacy_without_binding", [{"approver": "bob"}], 1)
    approvals("gate.approval.mixed",
              [{"approver": "bob", "sha": SHA, "tree_hash": TREE},
               {"approver": "carol", "sha": SHA, "tree_hash": TREE},
               {"approver": "dave", "sha": "b" * 40, "tree_hash": TREE}], 2)
    approvals("gate.approval.bot_only", [{"approver": "github-actions[bot]", "sha": SHA, "tree_hash": TREE}], 1)

    # merge gate：默认全部通过，逐项破坏一个条件
    def gate(case_id: str, *, overrides: dict, blockers: tuple[str, ...],
             tags: tuple[str, ...] = ()) -> None:
        cases.append(_case(
            case_id, layer="gate", block=bool(blockers), tags=("merge_gate",) + tags,
            probe="merge", probe_args={"overrides": overrides, "blockers": list(blockers)},
        ))

    gate("gate.merge.all_green", overrides={}, blockers=())
    gate("gate.merge.stale_sha", overrides={"reviewed_sha": "b" * 40}, blockers=("SHA",))
    gate("gate.merge.tree_mismatch", overrides={"reviewed_tree_hash": "tree-old"},
         blockers=("树",))
    gate("gate.merge.validation_missing", overrides={"validation_status": "failed"},
         blockers=("Docker",))
    gate("gate.merge.validation_infra", overrides={"validation_status": "infra_error"},
         blockers=("Docker",))
    gate("gate.merge.no_review", overrides={"verdict": ""}, blockers=("审查",))
    gate("gate.merge.review_request_changes", overrides={"verdict": "request_changes"},
         blockers=("审查",))
    gate("gate.merge.blocking_open", overrides={"blocking_open": 1}, blockers=("未解决",))
    gate("gate.merge.draft", overrides={"is_draft": True}, blockers=("draft",))
    gate("gate.merge.not_mergeable", overrides={"mergeable": False}, blockers=("不可合并",))
    gate("gate.merge.pr_closed", overrides={"pr_open": False}, blockers=("open",))
    gate("gate.merge.no_files", overrides={"changed_paths": []}, blockers=("没有可合并",))
    gate("gate.merge.evidence_missing", overrides={"evidence_ok": False}, blockers=("证据",))
    gate("gate.merge.needs_two_approvals",
         overrides={"tier": "T2", "min_approvals": 2,
                    "approvals": [{"approver": "bob", "sha": SHA, "tree_hash": TREE}]},
         blockers=("2",))
    gate("gate.merge.self_approval_only",
         overrides={"tier": "T2", "min_approvals": 1, "require_non_author": True, "author": "bob",
                    "approvals": [{"approver": "bob", "sha": SHA, "tree_hash": TREE}]},
         blockers=("非作者",))
    gate("gate.merge.high_risk_single_approval",
         overrides={"tier": "T1", "min_approvals": 1,
                    "changed_paths": ["src/auth/login.py"],
                    "approvals": [{"approver": "bob", "sha": SHA, "tree_hash": TREE}]},
         blockers=("高风险",))
    gate("gate.merge.human_mode", overrides={"merge_mode": "human"},
         blockers=("MERGE_MODE",))
    gate("gate.merge.high_risk_approved_by_human",
         overrides={"tier": "T1", "min_approvals": 1, "high_risk_approved": True,
                    "changed_paths": ["src/auth/login.py"],
                    "approvals": [{"approver": "bob", "sha": SHA, "tree_hash": TREE},
                                  {"approver": "carol", "sha": SHA, "tree_hash": TREE}]},
         blockers=())
    return cases


# ────────────────────────── security：不变量探针 ──────────────────────────


def security_cases() -> list[CaseSpec]:
    probes = [
        ("security.sandbox.network_none", "sandbox_kwargs", {"network": "none"}),
        ("security.sandbox.network_bridge", "sandbox_kwargs", {"network": "prepare"}),
        ("security.sandbox.read_only_root", "sandbox_kwargs", {}),
        ("security.sandbox.cap_drop_all", "sandbox_kwargs", {}),
        ("security.sandbox.non_root_user", "sandbox_kwargs", {}),
        ("security.sandbox.tmpfs_tmp", "sandbox_kwargs", {}),
        ("security.sandbox.no_new_privileges", "sandbox_kwargs", {}),
        ("security.sandbox.no_docker_socket", "sandbox_kwargs", {}),
        ("security.sandbox.no_host_env", "sandbox_kwargs", {}),
        ("security.sandbox.memory_and_pids_limit", "sandbox_kwargs", {}),
        ("security.tools.reviewer_t0_read_only", "tool_capability",
         {"role": "reviewer", "tier": "T0"}),
        ("security.tools.reviewer_t1_exec", "tool_capability",
         {"role": "reviewer", "tier": "T1"}),
        ("security.tools.external_mode_read", "tool_capability",
         {"role": "fixer", "tier": "T1", "mode": "external"}),
        ("security.tools.disabled", "tool_capability",
         {"role": "fixer", "tier": "T1", "disabled": True}),
        ("security.tools.fixer_without_allowed_read_only", "toolbox_names",
         {"role": "fixer", "allowed_files": []}),
        ("security.tools.fixer_with_allowed_has_write", "toolbox_names",
         {"role": "fixer", "allowed_files": ["src/calc.py"]}),
        ("security.tools.dispatch_denied", "tool_dispatch_denied", {}),
        ("security.fs.read_absolute_denied", "fs_traversal", {"mode": "absolute"}),
        ("security.fs.read_parent_denied", "fs_traversal", {"mode": "parent"}),
        ("security.fs.grep_parent_denied", "fs_traversal", {"mode": "grep"}),
        ("security.merge.bot_approval_not_counted", "bot_approval", {}),
        ("security.merge.approval_without_binding_not_counted", "unbound_approval", {}),
        ("security.budget.routing_finalize", "budget_routing", {}),
        ("security.secret.evidence_masked", "secret_masked", {}),
    ]
    gap_ids = {
        "security.merge.bot_approval_not_counted",
        "security.merge.approval_without_binding_not_counted",
    }
    return [
        _case(case_id, layer="security", block=True, probe=name, probe_args=args,
              tags=("security", "gap") if case_id in gap_ids else ("security",),
              notes="gap：当前门控未过滤机器人/未绑定 commit 的审批" if case_id in gap_ids else "")
        for case_id, name, args in probes
    ]


# ────────────────────────── e2e：全链路 ──────────────────────────


def e2e_cases() -> list[CaseSpec]:
    return [
        _case("e2e.clean.feature", layer="e2e",
              head={"src/feature.py": "def feature(x):\n    return x * 2\n"}, block=False),
        _case("e2e.clean.docs", layer="e2e",
              head={"docs/guide.md": "# guide\n"}, block=False),
        _case("e2e.block.syntax", layer="e2e",
              head={"src/broken.py": "def broken(a:\n    return a\n"}, block=True,
              rules=("SYN-001",)),
        _case("e2e.block.secret", layer="e2e",
              head={"src/config.py": "TOKEN = 'ghp_" + "b" * 36 + "'\n"}, block=True,
              rules=("SEC-001",)),
        _case("e2e.block.test_weakening", layer="e2e",
              base={"tests/test_calc.py": "def test_ok():\n    assert 1 == 1\n"},
              head={"tests/test_calc.py": None}, block=True, rules=("TEST-001",)),
        _case("e2e.block.api_break", layer="e2e",
              head={"src/calc.py": "def other(a, b):\n    return a + b\n"}, block=True,
              rules=("API-001",)),
        _case("e2e.block.protected_path", layer="e2e",
              head={".github/workflows/ci.yml": "name: ci\n"}, block=True,
              rules=("PATH-001", "CI-001")),
        _case("e2e.approval.high_risk_auth", layer="e2e",
              head={"src/auth/login.py": "def login(user):\n    return user\n"}, block=False),
    ]


def build() -> list[CaseSpec]:
    cases = det_cases() + semantic_cases() + gate_cases() + security_cases() + e2e_cases()
    covered = {rule for case in cases if case.layer == "det" for rule in case.expect_rules}
    missing = [rule for rule in RULE_IDS if rule not in covered]
    if missing:
        raise SystemExit(f"生成器遗漏 rule_id: {missing}")
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise SystemExit("生成器产生了重复 id")
    return cases


def summary(cases: list[CaseSpec]) -> dict:
    by_layer: dict[str, int] = {}
    for case in cases:
        by_layer[case.layer] = by_layer.get(case.layer, 0) + 1
    rules: dict[str, int] = {}
    for case in cases:
        if case.layer != "det":
            continue
        for rule in case.expect_rules:
            rules[rule] = rules.get(rule, 0) + 1
    return {"total": len(cases), "by_layer": by_layer, "rule_coverage": rules}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.build_cases")
    parser.add_argument("--out", default=str(DEFAULT_SUITE))
    parser.add_argument("--check", action="store_true", help="只校验生成器，不写文件")
    args = parser.parse_args(argv)
    cases = build()
    if not args.check:
        write_suite(cases, args.out)
    print(json.dumps(summary(cases), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
