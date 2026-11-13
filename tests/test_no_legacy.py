from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ("漫剧", "AI_Comic", "MangaDrama", "final_reviewer", "final_review")


def test_no_legacy_terms_in_source():
    paths = list((ROOT / "src").rglob("*.py")) + [
        ROOT / "README.md", ROOT / "pyproject.toml", ROOT / ".env.example",
    ]
    hits: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for term in LEGACY:
            if term in text:
                hits.append(f"{path.relative_to(ROOT)}: {term}")
    assert not hits, hits


def test_single_reviewer_agent_file():
    assert (ROOT / "src/codeguard/agents/reviewer.py").is_file()
    assert not (ROOT / "src/codeguard/agents/final_reviewer.py").exists()
