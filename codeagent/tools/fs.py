"""文件系统工具：读取、写入、定向编辑、列目录、全文检索。

所有路径都会经过 :class:`~codeagent.tools.base.Workspace` 校验，
确保 Agent 不会读写工作目录之外的内容。
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..errors import ToolError
from .base import Tool, ToolResult, Workspace

MAX_LINES_PER_READ = 2000


def _number_lines(lines: List[str], start_line: int) -> str:
    width = len(str(start_line + len(lines) - 1))
    return "\n".join(f"{start_line + idx:>{width}}| {text}" for idx, text in enumerate(lines))


def build_fs_tools(workspace: Workspace) -> List[Tool]:
    """构造文件系统工具集。"""

    # ------------------------------------------------------------------ read
    def read_file(path: str, start_line: int = 1, max_lines: int = 400) -> ToolResult:
        target = workspace.resolve(path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{workspace.relative(target)} 是目录，请改用 list_files 工具")
        text = workspace.read_text(target)
        all_lines = text.splitlines()
        total = len(all_lines)
        start = max(1, int(start_line))
        limit = max(1, min(int(max_lines), MAX_LINES_PER_READ))
        if start > total and total > 0:
            raise ToolError(f"start_line={start} 超出文件总行数 {total}")
        window = all_lines[start - 1 : start - 1 + limit]
        body = _number_lines(window, start) if window else "(空文件)"
        header = (
            f"文件: {workspace.relative(target)}  共 {total} 行  "
            f"显示第 {start}-{start + len(window) - 1} 行"
        )
        if start - 1 + len(window) < total:
            header += f"\n（内容已截断，剩余 {total - (start - 1 + len(window))} 行，可增大 start_line 继续读取）"
        return ToolResult.success(f"{header}\n{body}", path=workspace.relative(target), total_lines=total)

    # ----------------------------------------------------------------- write
    def write_file(path: str, content: str, overwrite: bool = False) -> ToolResult:
        target = workspace.resolve(path)
        if target.is_dir():
            raise ToolError(f"{workspace.relative(target)} 是已存在的目录")
        existed = target.exists()
        if existed and not overwrite:
            raise ToolError(
                f"文件已存在: {workspace.relative(target)}。"
                "如需覆盖请设置 overwrite=true；如只改局部请用 edit_file。"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content if isinstance(content, str) else str(content)
        target.write_text(data, encoding="utf-8", newline="\n")
        action = "覆盖写入" if existed else "新建"
        return ToolResult.success(
            f"已{action} {workspace.relative(target)}（{len(data)} 字符，{data.count(chr(10)) + 1} 行）",
            path=workspace.relative(target),
        )

    # ------------------------------------------------------------------ edit
    def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> ToolResult:
        target = workspace.resolve(path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{workspace.relative(target)} 是目录，无法编辑")
        text = workspace.read_text(target)
        occurrences = text.count(old_text)
        if occurrences == 0:
            raise ToolError(
                f"在 {workspace.relative(target)} 中未找到待替换文本；"
                "请先用 read_file 确认原文（注意缩进与换行需完全一致）。"
            )
        if occurrences > 1 and not replace_all:
            raise ToolError(
                f"待替换文本出现 {occurrences} 次，为避免误改请提供更精确的上下文，"
                "或设置 replace_all=true。"
            )
        updated = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8", newline="\n")
        return ToolResult.success(
            f"已修改 {workspace.relative(target)}：替换 {occurrences if replace_all else 1} 处",
            path=workspace.relative(target),
            replaced=occurrences if replace_all else 1,
        )

    # ------------------------------------------------------------------ list
    def list_files(pattern: str = "*", max_results: int = 200) -> ToolResult:
        start = None
        normalized = pattern or "*"
        if normalized not in ("*", "**/*", "**"):
            # 支持 "src/*.py" 这类写法：把目录部分当作遍历起点
            prefix = normalized.split("*", 1)[0]
            candidate = (workspace.root / prefix).parent if prefix else workspace.root
            if candidate.exists() and candidate.is_dir():
                start = candidate.resolve()
                start = workspace.resolve(str(start))
        files = workspace.iter_files(normalized, start=start)
        limit = max(1, min(int(max_results), 1000))
        shown = files[:limit]
        if not shown:
            return ToolResult.success(f"未找到匹配 {normalized} 的文件（工作目录 {workspace.root}）", count=0)
        lines = []
        for file_path in shown:
            try:
                size = file_path.stat().st_size
            except OSError:
                size = 0
            lines.append(f"{workspace.relative(file_path)}  ({size} B)")
        summary = f"匹配 {normalized} 的文件共 {len(files)} 个，显示前 {len(shown)} 个："
        if len(files) > len(shown):
            summary += "\n（结果已截断，可提高 max_results 或使用更精确的 pattern）"
        return ToolResult.success(f"{summary}\n" + "\n".join(lines), count=len(files))

    # ---------------------------------------------------------------- search
    def search_text(
        pattern: str,
        path: str = ".",
        include: Optional[str] = None,
        max_results: int = 60,
        ignore_case: bool = False,
    ) -> ToolResult:
        base = workspace.resolve(path, must_exist=True)
        flags = re.IGNORECASE if ignore_case else 0

        def compile_pattern(text: str, literal: bool) -> "re.Pattern[str]":
            source = re.escape(text) if literal else text
            return re.compile(source, flags)

        def scan(regex: "re.Pattern[str]", limit: int) -> tuple:
            found: List[str] = []
            hits = 0
            scanned_files = 0
            for file_path in candidates:
                if workspace.is_binary(file_path):
                    continue
                scanned_files += 1
                try:
                    content = workspace.read_text(file_path)
                except (ToolError, OSError):
                    continue
                file_hits = 0
                for lineno, line in enumerate(content.splitlines(), start=1):
                    if regex.search(line):
                        file_hits += 1
                        if len(found) < limit:
                            found.append(f"{workspace.relative(file_path)}:{lineno}: {line.strip()[:200]}")
                if file_hits:
                    hits += 1
                if len(found) >= limit:
                    break
            return found, hits, scanned_files

        if base.is_file():
            candidates = [base]
        else:
            candidates = workspace.iter_files(include or "*", start=base)

        limit = max(1, min(int(max_results), 500))
        used_literal = False
        try:
            regex = compile_pattern(pattern, literal=False)
        except re.error:
            regex = compile_pattern(pattern, literal=True)
            used_literal = True

        matches, hit_files, scanned = scan(regex, limit)

        # 正则没命中时，再按纯文本找一遍（用户常把 () 等当成普通字符）
        if not matches and not used_literal and pattern != re.escape(pattern):
            literal_matches, literal_hits, literal_scanned = scan(compile_pattern(pattern, literal=True), limit)
            if literal_matches:
                matches, hit_files, scanned, used_literal = (
                    literal_matches,
                    literal_hits,
                    literal_scanned,
                    True,
                )

        if not matches:
            note = "（pattern 不是合法正则，已按纯文本匹配）" if used_literal else ""
            return ToolResult.success(
                f"未找到匹配 {pattern!r} 的内容{note}；已扫描 {scanned} 个文件", count=0
            )
        truncated = "\n（结果已截断，可提高 max_results 或细化 pattern）" if len(matches) >= limit else ""
        note = "\n（未命中正则结果，已按纯文本匹配）" if used_literal else ""
        header = f"在 {workspace.relative(base)} 下匹配 {pattern!r}：{len(matches)} 条（命中 {hit_files} 个文件）"
        return ToolResult.success(f"{header}{note}\n" + "\n".join(matches) + truncated, count=len(matches))

    return [
        Tool(
            name="read_file",
            description=(
                "读取工作目录内文本文件的内容，返回带行号的代码。"
                "支持 start_line / max_lines 分段读取大文件。修改文件前必须先读取确认原文。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，相对工作目录（也可用绝对路径，但必须在工作目录内）"},
                    "start_line": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
                    "max_lines": {"type": "integer", "description": "最多读取的行数，默认 400，上限 2000"},
                },
                "required": ["path"],
            },
            handler=read_file,
        ),
        Tool(
            name="write_file",
            description="新建或覆盖写入文本文件。文件已存在时必须显式设置 overwrite=true。仅用于创建新文件或整文件重写。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径（相对工作目录）"},
                    "content": {"type": "string", "description": "要写入的完整文件内容"},
                    "overwrite": {"type": "boolean", "description": "文件已存在时是否覆盖，默认 false"},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
            mutating=True,
            requires_approval=True,
        ),
        Tool(
            name="edit_file",
            description=(
                "对文件做精确的字符串替换（比 write_file 更安全，适合局部修改）。"
                "old_text 必须与文件内容完全一致（含缩进），且默认要求唯一匹配。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "old_text": {"type": "string", "description": "要被替换的原文（需带足上下文保证唯一）"},
                    "new_text": {"type": "string", "description": "替换后的新文本"},
                    "replace_all": {"type": "boolean", "description": "是否替换所有匹配项，默认 false（只替换 1 处）"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            handler=edit_file,
            mutating=True,
            requires_approval=True,
        ),
        Tool(
            name="list_files",
            description="列出工作目录中匹配 glob 模式的文件（自动忽略 .git/__pycache__ 等目录）并显示文件大小。",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，如 '**/*.py'、'src/*.js'，默认 '*'"},
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 200"},
                },
                "required": [],
            },
            handler=list_files,
        ),
        Tool(
            name="search_text",
            description=(
                "在工作目录内做全文正则检索，返回 '文件:行号: 内容'。"
                "用于定位函数定义、调用点、TODO 等。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则或纯文本，如 'def main'、'TODO'"},
                    "path": {"type": "string", "description": "检索起点（文件或目录），默认工作目录"},
                    "include": {"type": "string", "description": "文件名 glob 过滤，如 '*.py'"},
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 60"},
                    "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 false"},
                },
                "required": ["pattern"],
            },
            handler=search_text,
        ),
    ]
