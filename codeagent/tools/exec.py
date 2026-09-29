"""代码执行工具：运行 Python 代码片段、运行项目测试。

安全说明
--------
这是“作业级”沙箱，不是内核级隔离：

* 路径层面由 :class:`~codeagent.tools.base.Workspace` 限制在工作目录内；
* 子进程级别有执行超时与输出截断；
* 但被执行的代码本身仍拥有当前用户的权限，因此这两个工具都标记为
  ``requires_approval=True``，默认需要用户确认（``--yes`` 可跳过确认）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import ToolError
from .base import Tool, ToolResult, Workspace

MAX_OUTPUT_CHARS = 6000
DEFAULT_TIMEOUT = 20.0
MAX_TIMEOUT = 120.0


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n...（中间省略 {len(text) - limit} 字符）...\n{tail}"


def _child_env(workspace: Workspace) -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.pop("PYTHONSTARTUP", None)
    return env


def _run_process(
    argv: List[str],
    workspace: Workspace,
    timeout: float,
    cwd: Optional[Path] = None,
) -> Dict[str, Any]:
    """执行子进程并返回结构化结果（超时也会返回已产生的输出）。"""
    started = time.time()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd or workspace.root),
            env=_child_env(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
            "timed_out": False,
            "elapsed": round(time.time() - started, 2),
        }
    except subprocess.TimeoutExpired as exc:
        def _decode(value: Any) -> str:
            if value is None:
                return ""
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)

        return {
            "returncode": None,
            "stdout": _decode(exc.stdout),
            "stderr": _decode(exc.stderr),
            "timed_out": True,
            "elapsed": round(time.time() - started, 2),
        }
    except FileNotFoundError as exc:
        raise ToolError(f"无法启动子进程: {exc}") from exc


def _format_result(payload: Dict[str, Any], title: str) -> str:
    parts = [title]
    if payload["timed_out"]:
        parts.append(f"⏱ 执行超时（{payload['elapsed']}s），进程已被终止")
    else:
        parts.append(f"退出码: {payload['returncode']}  耗时: {payload['elapsed']}s")
    stdout = payload["stdout"].strip()
    stderr = payload["stderr"].strip()
    parts.append("--- stdout ---\n" + (_truncate(stdout) if stdout else "(空)"))
    if stderr:
        parts.append("--- stderr ---\n" + _truncate(stderr))
    return "\n".join(parts)


def build_exec_tools(workspace: Workspace) -> List[Tool]:
    """构造代码执行工具集。"""

    def run_python(
        code: Optional[str] = None,
        path: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ToolResult:
        if bool(code) == bool(path):
            raise ToolError("必须且只能提供 code 或 path 其中之一")
        limit = max(1.0, min(float(timeout), MAX_TIMEOUT))

        temp_file: Optional[Path] = None
        if code:
            temp_dir = workspace.root / ".codeagent" / "tmp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".py", dir=temp_dir, delete=False, encoding="utf-8", newline="\n"
            )
            handle.write(code)
            handle.close()
            temp_file = Path(handle.name)
            argv = [sys.executable, str(temp_file)]
            label = f"运行内联代码（{len(code)} 字符）"
        else:
            target = workspace.resolve(str(path), must_exist=True)
            if target.is_dir():
                raise ToolError(f"{workspace.relative(target)} 是目录，不能直接运行")
            argv = [sys.executable, str(target)]
            label = f"运行脚本 {workspace.relative(target)}"

        try:
            payload = _run_process(argv, workspace, limit)
        finally:
            if temp_file is not None:
                try:
                    temp_file.unlink()
                except OSError:
                    pass

        text = _format_result(payload, f"{label} | python {sys.version.split()[0]}")
        if payload["timed_out"]:
            return ToolResult.failure(text, timed_out=True)
        if payload["returncode"] != 0:
            return ToolResult.failure(text, returncode=payload["returncode"])
        return ToolResult.success(text, returncode=0)

    def run_tests(path: str = ".", timeout: float = 90, framework: str = "auto") -> ToolResult:
        target = workspace.resolve(path, must_exist=True)
        limit = max(1.0, min(float(timeout), MAX_TIMEOUT))

        chosen = framework
        if chosen == "auto":
            chosen = "pytest" if _module_available("pytest") else "unittest"

        if chosen == "pytest":
            argv = [sys.executable, "-m", "pytest", str(target), "-q", "--no-header"]
        else:
            start_dir = target if target.is_dir() else target.parent
            argv = [sys.executable, "-m", "unittest", "discover", "-s", str(start_dir), "-v"]
            if target.is_file():
                argv = [sys.executable, "-m", "unittest", "discover", "-s", str(target.parent), "-p", target.name, "-v"]

        payload = _run_process(argv, workspace, limit)
        text = _format_result(payload, f"测试执行（{chosen}）: {workspace.relative(target)}")
        if payload["timed_out"]:
            return ToolResult.failure(text, timed_out=True)
        summary = _summarize_tests(payload["stdout"] + payload["stderr"])
        text = f"{text}\n--- 摘要 ---\n{summary}"
        if payload["returncode"] != 0:
            return ToolResult.failure(text, returncode=payload["returncode"])
        return ToolResult.success(text, returncode=0)

    return [
        Tool(
            name="run_python",
            description=(
                "执行 Python 代码并返回 stdout/stderr 与退出码。"
                "可传 code（内联代码片段）或 path（工作目录内的 .py 文件，二者只能选一个）。"
                "常用于验证修复是否有效、复现 Bug。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 源码（与 path 二选一）"},
                    "path": {"type": "string", "description": "要执行的 .py 文件路径（与 code 二选一）"},
                    "timeout": {"type": "number", "description": f"超时秒数，默认 {DEFAULT_TIMEOUT}，上限 {MAX_TIMEOUT}"},
                },
                "required": [],
            },
            handler=run_python,
            mutating=True,
            requires_approval=True,
        ),
        Tool(
            name="run_tests",
            description=(
                "在指定目录上运行测试（自动选择 pytest 或标准库 unittest）并给出通过/失败摘要。"
                "修复 Bug 或新增功能后用它做回归验证。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "测试目录或文件，默认工作目录"},
                    "timeout": {"type": "number", "description": "超时秒数，默认 90"},
                    "framework": {
                        "type": "string",
                        "enum": ["auto", "pytest", "unittest"],
                        "description": "测试框架，默认 auto",
                    },
                },
                "required": [],
            },
            handler=run_tests,
            mutating=True,
            requires_approval=True,
        ),
    ]


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover
        return False


def _summarize_tests(output: str) -> str:
    """从测试输出中抽取关键行，避免把整篇日志塞回模型。"""
    keys = (
        "FAILED", "ERROR", "PASSED", "OK", "Ran ", "=====", "---",
        "failures=", "errors=", "passed", "failed",
    )
    picked: List[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(key in line for key in keys) and len(picked) < 25:
            picked.append(stripped[:200])
    if not picked:
        return _truncate(output.strip()[-1500:] or "(无输出)")
    return "\n".join(picked)
