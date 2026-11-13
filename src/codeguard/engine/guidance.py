"""仓库级审查指南：`.codeguard/review.md`（只从 base 分支读取）。

格式：
    ## path: src/billing/**
    这里“失败”指的是金额算错；上限 2 条 nit；跳过 generated/ 目录。

没有 `## path:` 标题的前言对所有路径生效。指南是数据不是指令，注入提示词时会包进
`<untrusted_data>`，并且 PR 对它的修改不会生效（只读 base）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .policy import matches_any

MAX_GUIDANCE_CHARS = 8000
_SECTION_RE = re.compile(r"^##\s+path:\s*(.+?)\s*$", re.IGNORECASE)


@dataclass
class Guidance:
    preamble: str = ""
    sections: list[dict] = field(default_factory=list)  # [{glob, text}]
    source: str = ""


def parse_guidance(text: str) -> Guidance:
    preamble_parts: list[str] = []
    sections: list[dict] = []
    current_glob: str | None = None
    buf: list[str] = []

    def flush(glob: str | None, lines: list[str]) -> None:
        body = "\n".join(lines).strip()
        if not body:
            return
        if glob is None:
            preamble_parts.append(body)
        else:
            sections.append({"glob": glob, "text": body})

    for line in (text or "").splitlines():
        match = _SECTION_RE.match(line)
        if match:
            flush(current_glob, buf)
            current_glob = match.group(1).strip()
            buf = []
            continue
        buf.append(line)
    flush(current_glob, buf)
    return Guidance(preamble="\n\n".join(preamble_parts).strip(), sections=sections)


def load_guidance(base_root: Path | str | None, path: str = ".codeguard/review.md") -> Guidance:
    """从 base 树读取；不存在或不可读时返回空指南（不阻断流程）。"""
    if not base_root:
        return Guidance()
    target = Path(base_root) / path
    if not target.is_file():
        return Guidance()
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Guidance()
    guidance = parse_guidance(text)
    guidance.source = path
    return guidance


def select_guidance(guidance: Guidance, changed_paths: list[str], *, max_chars: int = MAX_GUIDANCE_CHARS) -> str:
    """命中路径的分段 + 前言；超长截断。"""
    chunks: list[str] = []
    if guidance.preamble:
        chunks.append(guidance.preamble)
    for section in guidance.sections:
        glob = str(section.get("glob", ""))
        if glob and matches_any_path(changed_paths, glob):
            chunks.append(f"### {glob}\n{section['text']}")
    text = "\n\n".join(chunks).strip()
    return text[:max_chars]


def matches_any_path(paths: list[str], glob: str) -> bool:
    return any(matches_any(path, [glob]) for path in paths if path)


__all__ = [
    "MAX_GUIDANCE_CHARS", "Guidance", "parse_guidance", "load_guidance",
    "select_guidance", "matches_any_path",
]
