"""补丁写作工具：相关代码上下文读取 + 标准 Unified Diff 合成。

write_draft 节点的 Search 阶段用 read_relevant_code 按需裁剪上下文（Context
Engineering）；产出交付阶段用 synthesize_unified_diff 生成可一键 `git apply`
的标准 .patch 文本。
"""
from __future__ import annotations

import difflib
from pathlib import Path


def read_relevant_code(
    repo_dir: Path,
    file_paths: list[str],
    *,
    max_chars_per_file: int = 6000,
    max_total_chars: int = 40000,
) -> str:
    """读取指定文件内容并拼接为 LLM 上下文，超长截断，缺失文件显式标注。

    Args:
        repo_dir: 仓库根目录。
        file_paths: 相对路径列表。
        max_chars_per_file: 单文件最大字符数。
        max_total_chars: 总上下文最大字符数。

    Returns:
        形如 "=== path ===\n<content>" 的拼接文本。
    """
    parts: list[str] = []
    total = 0
    for rel in file_paths:
        fpath = repo_dir / rel
        if not fpath.is_file():
            parts.append(f"=== {rel} ===\n（文件不存在）")
            continue
        try:
            content = fpath.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file] + "\n...（截断）"
        remaining = max_total_chars - total
        if remaining <= 0:
            parts.append(f"=== {rel} ===\n（超出总上下文上限，未读取）")
            continue
        if len(content) > remaining:
            content = content[:remaining] + "\n...（截断）"
        total += len(content)
        parts.append(f"=== {rel} ===\n{content}")
    return "\n\n".join(parts)


def synthesize_unified_diff(repo_dir: Path, patches: dict[str, str]) -> str:
    """将 {相对路径: 修复后完整代码} 与仓库现状对比，合成标准 Unified Diff 文本。

    输出可直接保存为 .patch 供 `git apply` 使用。仅包含内容有变化的文件。
    """
    diff_chunks: list[str] = []
    for rel in sorted(patches):
        fpath = repo_dir / rel
        if fpath.is_file():
            try:
                old_text = fpath.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                old_text = fpath.read_text(encoding="utf-8", errors="replace")
            from_label = f"a/{rel}"
        else:
            old_text = ""
            from_label = f"a/{rel}"
            # 新增文件场景：原内容为空

        new_text = patches[rel]
        if old_text == new_text:
            continue
        diff = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=from_label,
            tofile=f"b/{rel}",
        )
        diff_chunks.append("".join(diff))
    return "\n".join(diff_chunks)
