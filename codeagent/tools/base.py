"""工具框架：沙箱、工具契约与注册表。

设计要点
--------
* **统一契约**：每个工具声明 JSON Schema 形式的 ``parameters``，
  直接作为 OpenAI ``tools`` 字段下发给模型；模型只能看到它被允许看到的参数。
* **沙箱**：所有文件操作都经过 :class:`Workspace` 校验，路径必须位于工作目录内，
  防止模型读写工作区之外的文件（如 ``C:\\Windows`` 或 ``../../``）。
* **错误即观察**：工具抛出的 :class:`~codeagent.errors.ToolError` 不会中断 Agent，
  而是作为 tool 消息回传给模型，让它自己修正（例如换个参数重试）。
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from ..errors import PathNotAllowed, ToolError, ToolNotFound

#: 遍历目录时默认忽略的目录名
DEFAULT_EXCLUDES: frozenset = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "env",
        ".idea",
        ".vscode",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".codeagent",
        "dist",
        "build",
        ".eggs",
    }
)

#: 常见二进制后缀，读取时直接拒绝而非输出乱码
BINARY_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".zip", ".gz", ".tar", ".rar", ".7z",
     ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".mp4", ".mp3", ".woff", ".woff2", ".ttf"}
)


# --------------------------------------------------------------------------- #
# 工具返回值
# --------------------------------------------------------------------------- #
@dataclass
class ToolResult:
    """工具执行结果。``content`` 会原样回传给模型作为 observation。"""

    ok: bool
    content: str
    meta: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **meta: Any) -> "ToolResult":
        return cls(ok=True, content=content, meta=meta)

    @classmethod
    def failure(cls, content: str, **meta: Any) -> "ToolResult":
        return cls(ok=False, content=content, meta=meta)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "content": self.content, "meta": self.meta}


# --------------------------------------------------------------------------- #
# 工作区沙箱
# --------------------------------------------------------------------------- #
class Workspace:
    """把文件操作限制在给定根目录内。"""

    def __init__(
        self,
        root: Path,
        excludes: Iterable[str] = DEFAULT_EXCLUDES,
        max_read_bytes: int = 200_000,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.excludes = frozenset(excludes)
        self.max_read_bytes = max_read_bytes
        if not self.root.exists():
            raise ToolError(f"工作目录不存在: {self.root}")
        if not self.root.is_dir():
            raise ToolError(f"工作目录不是目录: {self.root}")

    # ------------------------------------------------------------------ 路径
    def resolve(self, raw_path: str, must_exist: bool = False) -> Path:
        """把用户/模型给的路径解析为绝对路径，并校验未越界。"""
        if raw_path is None or str(raw_path).strip() == "":
            candidate = self.root
        else:
            candidate = Path(str(raw_path).strip()).expanduser()
            if not candidate.is_absolute():
                candidate = self.root / candidate
        resolved = candidate.resolve()
        root_key = os.path.normcase(str(self.root))
        path_key = os.path.normcase(str(resolved))
        if path_key != root_key and not path_key.startswith(root_key + os.sep):
            raise PathNotAllowed(
                f"路径越界：{raw_path!r} 不在工作目录 {self.root} 内。"
                "出于安全考虑，Agent 只能操作工作目录中的文件。"
            )
        if must_exist and not resolved.exists():
            raise ToolError(f"路径不存在: {self.relative(resolved)}")
        return resolved

    def relative(self, path: Path) -> str:
        """转成相对工作目录的 POSIX 风格字符串，便于展示。"""
        try:
            return Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def is_excluded(self, path: Path) -> bool:
        return any(part in self.excludes for part in Path(path).parts)

    def is_binary(self, path: Path) -> bool:
        return path.suffix.lower() in BINARY_SUFFIXES

    def read_text(self, path: Path) -> str:
        """读取文本文件，自动做大小限制与编码容错。"""
        if self.is_binary(path):
            raise ToolError(f"不支持读取二进制文件: {self.relative(path)}")
        size = path.stat().st_size
        if size > self.max_read_bytes:
            raise ToolError(
                f"文件过大（{size} 字节 > 上限 {self.max_read_bytes} 字节），"
                "请使用 read_file 的 start_line/max_lines 分段读取。"
            )
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------ 遍历
    @staticmethod
    def _match(rel_path: str, filename: str, pattern: str) -> bool:
        """glob 匹配：``**/x.py`` 也要能匹配顶层的 ``x.py``。"""
        candidates = {pattern}
        if pattern.startswith("**/"):
            candidates.add(pattern[3:])
        return any(
            fnmatch.fnmatch(rel_path, candidate) or fnmatch.fnmatch(filename, candidate)
            for candidate in candidates
        )

    def iter_files(self, pattern: str = "*", start: Optional[Path] = None) -> List[Path]:
        """按 glob 模式遍历文件（自动跳过 :data:`DEFAULT_EXCLUDES`）。"""
        base = Path(start) if start else self.root
        results: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in self.excludes)
            current = Path(dirpath)
            if self.is_excluded(current.relative_to(self.root)) and current != self.root:
                continue
            for filename in sorted(filenames):
                file_path = current / filename
                rel = file_path.relative_to(self.root).as_posix()
                if pattern in ("*", "**/*", "**") or self._match(rel, filename, pattern):
                    results.append(file_path)
        return results


# --------------------------------------------------------------------------- #
# 工具契约
# --------------------------------------------------------------------------- #
@dataclass
class Tool:
    """一个可被 Agent 调用的工具。"""

    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., Any]
    mutating: bool = False
    requires_approval: bool = False

    def schema(self) -> Dict[str, Any]:
        """OpenAI function calling 格式的工具描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    # -------------------------------------------------------------- 参数校验
    def validate(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """校验并归一化参数；错误信息尽量对模型友好。"""
        properties = self.parameters.get("properties", {})
        required = self.parameters.get("required", [])
        cleaned = dict(arguments or {})

        unknown = [key for key in cleaned if key not in properties]
        if unknown:
            raise ToolError(
                f"工具 {self.name} 不支持参数 {unknown}；可用参数: {sorted(properties)}"
            )
        missing = [key for key in required if cleaned.get(key) in (None, "")]
        if missing:
            raise ToolError(
                f"工具 {self.name} 缺少必填参数 {missing}；"
                f"参数说明: { {k: properties[k].get('description', '') for k in missing} }"
            )

        # 轻量类型纠正：模型常把数字写成字符串
        for key, value in list(cleaned.items()):
            expected = properties.get(key, {}).get("type")
            if expected == "integer" and isinstance(value, str) and value.lstrip("-").isdigit():
                cleaned[key] = int(value)
            elif expected == "number" and isinstance(value, str):
                try:
                    cleaned[key] = float(value)
                except ValueError:
                    pass
            elif expected == "boolean" and isinstance(value, str):
                if value.lower() in {"true", "false"}:
                    cleaned[key] = value.lower() == "true"
        return cleaned

    def invoke(self, arguments: Dict[str, Any]) -> ToolResult:
        """执行工具，把任何异常转换成 :class:`ToolResult`。"""
        try:
            cleaned = self.validate(arguments)
            result = self.handler(**cleaned)
        except ToolError as exc:
            return ToolResult.failure(str(exc), error_type=type(exc).__name__)
        except TypeError as exc:
            return ToolResult.failure(
                f"调用 {self.name} 时参数错误: {exc}；请检查参数名与类型。", error_type="TypeError"
            )
        except Exception as exc:  # noqa: BLE001 - 工具内部异常统一兜底
            return ToolResult.failure(
                f"工具 {self.name} 执行失败: {type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
            )
        if isinstance(result, ToolResult):
            return result
        return ToolResult.success(str(result))


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
class ToolRegistry:
    """工具集合，负责查找、下发 schema、统一执行入口。"""

    def __init__(self, tools: Optional[Sequence[Tool]] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ToolError(f"工具名重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(
                f"未注册的工具 {name!r}；可用工具: {sorted(self._tools)}"
            ) from exc

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def all(self) -> List[Tool]:
        return [self._tools[name] for name in self.names()]

    def schemas(self) -> List[Dict[str, Any]]:
        return [tool.schema() for tool in self.all()]

    def run(self, name: str, arguments: Dict[str, Any]) -> ToolResult:
        """执行工具；未注册的工具返回失败结果而不是抛异常。"""
        try:
            tool = self.get(name)
        except ToolNotFound as exc:
            return ToolResult.failure(str(exc), error_type="ToolNotFound")
        return tool.invoke(arguments)

    def describe(self) -> str:
        """人类可读的工具清单（用于 CLI ``/tools``）。"""
        lines = []
        for tool in self.all():
            flags = []
            if tool.mutating:
                flags.append("写操作")
            if tool.requires_approval:
                flags.append("需确认")
            suffix = f"  [{'/'.join(flags)}]" if flags else ""
            lines.append(f"- {tool.name}{suffix}: {tool.description}")
        return "\n".join(lines)
