"""工具集装配。

:func:`build_default_registry` 是 Agent 与具体工具之间的唯一耦合点：
新增工具只需要在这里注册，Agent 主循环无需修改（开闭原则）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .analysis import build_analysis_tools
from .base import Tool, ToolRegistry, ToolResult, Workspace
from .exec import build_exec_tools
from .fs import build_fs_tools

__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "Workspace",
    "build_default_registry",
    "build_all_tools",
]


def build_all_tools(workspace: Workspace) -> list:
    """返回全部工具的列表（文件系统 + 执行 + 静态分析）。"""
    tools = []
    tools.extend(build_fs_tools(workspace))
    tools.extend(build_analysis_tools(workspace))
    tools.extend(build_exec_tools(workspace))
    return tools


def build_default_registry(workspace: Optional[Workspace] = None, root: Optional[Path] = None) -> ToolRegistry:
    """构造默认工具注册表。

    参数二选一：直接给 ``workspace``，或给 ``root`` 由本函数创建。
    """
    if workspace is None:
        if root is None:
            raise ValueError("必须提供 workspace 或 root")
        workspace = Workspace(Path(root))
    return ToolRegistry(build_all_tools(workspace))
