"""代码静态分析工具（AST + 正则规则）。

这是本 Agent 的“代码审查”核心能力：**不依赖 LLM 也能给出确定性的缺陷清单**，
LLM 只负责把清单翻译成人话、判断优先级、并调用 ``edit_file`` 落地修复。
这种"确定性工具 + LLM 编排"的分工是本项目的架构重点之一。

规则覆盖：异常吞噬、可变默认参数、None 比较、eval/exec、类型比较、
资源未释放、复杂度与超长函数、缺失文档、内建名遮蔽、未使用导入、
TODO 标记、过长行、shell=True 等。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from ..errors import ToolError
from .base import Tool, ToolResult, Workspace

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低", "info": "提示"}

LONG_FUNCTION_LINES = 60
COMPLEXITY_THRESHOLD = 12
LONG_LINE_CHARS = 120

SHADOWABLE = frozenset(
    {
        "id", "type", "list", "dict", "set", "str", "int", "float", "bool", "bytes", "object",
        "input", "print", "format", "filter", "map", "sum", "min", "max", "len", "next", "open",
        "file", "hash", "all", "any", "dir", "vars", "eval", "exec", "compile", "range", "slice",
        "tuple", "zip", "sorted", "reversed", "round", "abs", "pow", "callable", "getattr",
        "setattr", "hasattr", "delattr", "globals", "locals", "super", "iter", "enumerate",
        "isinstance", "issubclass", "repr", "exit", "quit", "property", "staticmethod", "classmethod",
    }
)

TODO_PATTERN = re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b[:\s]*(.*)", re.IGNORECASE)


@dataclass
class Finding:
    """一条静态分析发现。"""

    rule: str
    severity: str
    line: int
    message: str
    suggestion: str = ""
    snippet: str = ""

    def render(self) -> str:
        label = SEVERITY_LABEL.get(self.severity, self.severity)
        text = f"[{label}] L{self.line} {self.rule}: {self.message}"
        if self.snippet:
            text += f"\n        代码: {self.snippet}"
        if self.suggestion:
            text += f"\n        建议: {self.suggestion}"
        return text


def _complexity(node: ast.AST) -> int:
    """粗略的圈复杂度：1 + 判定节点数量。

    注意：**不统计嵌套函数/类内部的判定节点**，否则工厂函数（内部定义多个
    handler）会被严重高估——这是很多静态分析工具的常见误报来源。
    """
    score = 1
    stack: List[ast.AST] = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue  # 嵌套定义单独评估
        if isinstance(
            child,
            (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.With, ast.AsyncWith,
             ast.IfExp, ast.Assert, ast.BoolOp, ast.comprehension),
        ):
            score += 1
        stack.extend(ast.iter_child_nodes(child))
    return score


def _own_lines(node: Any) -> int:
    """函数自身行数（扣除内部嵌套定义的代码行）。"""
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    total = end - node.lineno + 1
    nested = 0
    for child in ast.walk(node):
        if child is node:
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            child_end = getattr(child, "end_lineno", child.lineno) or child.lineno
            nested += child_end - child.lineno + 1
    return max(1, total - nested)


class _Reviewer(ast.NodeVisitor):
    """遍历 AST 收集发现。"""

    def __init__(self, source: str, lines: List[str], path_label: str) -> None:
        self.source = source
        self.lines = lines
        self.path_label = path_label
        self.findings: List[Finding] = []
        self.functions: List[Tuple[str, int, int, int]] = []  # (qualname, lineno, end, complexity)
        self.classes: List[Tuple[str, int]] = []

    # ------------------------------------------------------------------ 工具
    def snippet(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1].strip()[:140]
        return ""

    def add(self, rule: str, severity: str, line: int, message: str, suggestion: str = "") -> None:
        self.findings.append(
            Finding(
                rule=rule,
                severity=severity,
                line=line,
                message=message,
                suggestion=suggestion,
                snippet=self.snippet(line),
            )
        )

    # --------------------------------------------------------------- 异常处理
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.add(
                "bare-except",
                "high",
                node.lineno,
                "使用了裸 except，会吞掉 KeyboardInterrupt / SystemExit 等所有异常",
                "改为捕获具体异常类型，如 `except ValueError:`；确实需要兜底时用 `except Exception:`",
            )
        elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                self.add(
                    "silent-except",
                    "high",
                    node.lineno,
                    "except Exception: pass 静默吞异常，出错后难以排查",
                    "至少记录日志（logging.exception）或重新抛出，不要 pass",
                )
        # except ...: return None 这类“吞掉异常继续”的写法
        if len(node.body) == 1 and isinstance(node.body[0], ast.Return) and node.body[0].value is None:
            self.add(
                "silent-except",
                "medium",
                node.lineno,
                "异常被吞掉并返回 None，调用方无法区分“正常空值”与“出错”",
                "考虑记录日志或抛出带上下文的异常",
            )
        self.generic_visit(node)

    # ------------------------------------------------------------------ 函数
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_function(node)

    def _check_function(self, node: Any) -> None:
        length = _own_lines(node)
        complexity = _complexity(node)
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        self.functions.append((node.name, node.lineno, end, complexity))

        # 可变默认参数
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
            if isinstance(default, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp)):
                self.add(
                    "mutable-default",
                    "high",
                    node.lineno,
                    f"函数 {node.name} 使用了可变对象作为默认参数，多次调用会共享同一对象",
                    "改为默认 None，在函数体内 `if arg is None: arg = []`",
                )
                break
        if length > LONG_FUNCTION_LINES:
            self.add(
                "long-function",
                "medium",
                node.lineno,
                f"函数 {node.name} 长达 {length} 行，可读性与可测试性下降",
                "按职责拆分为多个小函数",
            )
        if complexity > COMPLEXITY_THRESHOLD:
            self.add(
                "high-complexity",
                "medium",
                node.lineno,
                f"函数 {node.name} 圈复杂度约 {complexity}，分支过多",
                "提取条件判断为独立函数，或用早返回（early return）降低嵌套",
            )
        if ast.get_docstring(node) is None and not node.name.startswith("_"):
            self.add(
                "missing-docstring",
                "info",
                node.lineno,
                f"公开函数 {node.name} 缺少 docstring",
                "补充一行说明用途、参数与返回值",
            )
        # 参数遮蔽内建名
        for arg in [*node.args.args, *node.args.kwonlyargs]:
            if arg.arg in SHADOWABLE:
                self.add(
                    "shadow-builtin",
                    "low",
                    arg.lineno if hasattr(arg, "lineno") else node.lineno,
                    f"参数 {arg.arg} 遮蔽了内建函数名",
                    f"重命名为更具体的名字，如 {arg.arg}_",
                )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append((node.name, node.lineno))
        if ast.get_docstring(node) is None and not node.name.startswith("_"):
            self.add(
                "missing-docstring",
                "info",
                node.lineno,
                f"公开类 {node.name} 缺少 docstring",
                "补充类的职责说明",
            )
        self.generic_visit(node)

    # ------------------------------------------------------------------ 调用
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_builtin_call = isinstance(func, ast.Name)
        name = func.id if is_builtin_call else (func.attr if isinstance(func, ast.Attribute) else "")
        # 方法调用的接收者（用于区分 subprocess.run 与 obj.run）
        receiver = ""
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            receiver = func.value.id

        if is_builtin_call and name in {"eval", "exec"}:
            self.add(
                "dangerous-eval",
                "high",
                node.lineno,
                f"使用了 {name}()，任意代码执行风险",
                "改用 ast.literal_eval 或显式的解析逻辑",
            )
        if is_builtin_call and name == "open" and not self._inside_with(node):
            self.add(
                "file-not-closed",
                "medium",
                node.lineno,
                "open() 未配合 with 使用，异常路径下文件句柄可能泄漏",
                "改为 `with open(...) as f:`",
            )
        if receiver in {"subprocess", "os"} and name in {"system", "popen", "run", "call", "check_output", "Popen"}:
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    self.add(
                        "shell-true",
                        "high",
                        node.lineno,
                        "subprocess 使用 shell=True，存在命令注入风险",
                        "改为传列表参数（shell=False），避免拼接用户输入",
                    )
        if is_builtin_call and name == "print" and self._in_library_context(node):
            self.add(
                "print-statement",
                "info",
                node.lineno,
                "库代码中直接 print，建议使用 logging",
                "替换为 logging 模块调用",
            )
        self.generic_visit(node)

    def _inside_with(self, node: ast.AST) -> bool:
        """粗略判断 open() 是否处于 with 语句的上下文表达式中。"""
        parent = getattr(node, "parent", None)
        while parent is not None:
            if isinstance(parent, ast.With):
                return True
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return False
            parent = getattr(parent, "parent", None)
        return False

    def _in_library_context(self, node: ast.AST) -> bool:
        parent = getattr(node, "parent", None)
        while parent is not None:
            if isinstance(parent, ast.If):
                test = parent.test
                if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__":
                    return False
            parent = getattr(parent, "parent", None)
        return True

    # ------------------------------------------------------------------ 比较
    def visit_Compare(self, node: ast.Compare) -> None:
        for op, comparator in zip(node.ops, node.comparators):
            is_equality = isinstance(op, (ast.Eq, ast.NotEq))
            is_constant = isinstance(comparator, ast.Constant)
            if is_equality and is_constant and comparator.value is None:
                self.add(
                    "none-comparison",
                    "low",
                    node.lineno,
                    "使用 == / != 与 None 比较",
                    "改用 `is None` / `is not None`",
                )
            if is_equality and is_constant and isinstance(comparator.value, bool):
                self.add(
                    "bool-comparison",
                    "low",
                    node.lineno,
                    "与 True/False 显式比较，冗余",
                    "直接使用变量本身或 `not var`",
                )
            if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Call):
                call_name = comparator.func.id if isinstance(comparator.func, ast.Name) else ""
                if call_name == "type":
                    self.add(
                        "type-comparison",
                        "medium",
                        node.lineno,
                        "用 type(x) == T 判断类型，无法识别子类",
                        "改用 isinstance(x, T)",
                    )
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        iterator = node.iter
        if (
            isinstance(iterator, ast.Call)
            and isinstance(iterator.func, ast.Name)
            and iterator.func.id == "range"
            and len(iterator.args) == 1
            and isinstance(iterator.args[0], ast.Call)
            and isinstance(iterator.args[0].func, ast.Name)
            and iterator.args[0].func.id == "len"
        ):
            self.add(
                "range-len",
                "low",
                node.lineno,
                "使用 for i in range(len(seq)) 遍历",
                "改用 `for i, item in enumerate(seq)`",
            )
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.add(
            "global-statement",
            "low",
            node.lineno,
            f"使用了 global 声明（{', '.join(node.names)}），模块状态难以追踪",
            "考虑封装到类中或通过参数传递",
        )
        self.generic_visit(node)

    # ------------------------------------------------------------- 赋值/导入
    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in SHADOWABLE:
                self.add(
                    "shadow-builtin",
                    "low",
                    target.lineno,
                    f"变量 {target.id} 遮蔽了内建函数名",
                    "换一个语义更明确的名字",
                )
        self.generic_visit(node)


# --------------------------------------------------------------------------- #
# 分析与呈现
# --------------------------------------------------------------------------- #
def _attach_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]


def _unused_imports(tree: ast.AST, source: str) -> List[Tuple[str, int]]:
    """找出导入了但全文未再出现的名字（对 import 行本身做排除）。"""
    imported: Dict[str, int] = {}
    import_line_spans: List[Tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            import_line_spans.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                imported.setdefault(bound, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            import_line_spans.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
            for alias in node.names:
                if alias.name == "*":
                    return []
                bound = alias.asname or alias.name
                if bound == "annotations":
                    continue
                imported.setdefault(bound, node.lineno)

    if not imported:
        return []

    lines = source.splitlines()
    for start, end in import_line_spans:
        for index in range(start - 1, min(end, len(lines))):
            lines[index] = ""
    body = "\n".join(lines)

    unused: List[Tuple[str, int]] = []
    for name, lineno in imported.items():
        if name.startswith("_"):
            continue
        if name in {"annotations"}:
            continue
        if not re.search(rf"\b{re.escape(name)}\b", body):
            unused.append((name, lineno))
    return unused


def analyze_source(source: str, path_label: str = "<string>") -> Tuple[List[Finding], Dict[str, Any]]:
    """分析源码文本，返回 (发现列表, 结构摘要)。"""
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=path_label)
    except SyntaxError as exc:
        finding = Finding(
            rule="syntax-error",
            severity="high",
            line=exc.lineno or 0,
            message=f"语法错误: {exc.msg}",
            suggestion="先修复语法错误，否则代码无法导入或运行",
            snippet=(lines[exc.lineno - 1].strip() if exc.lineno and exc.lineno <= len(lines) else ""),
        )
        return [finding], {"functions": [], "classes": [], "imports": [], "total_lines": len(lines)}

    _attach_parents(tree)
    reviewer = _Reviewer(source, lines, path_label)
    reviewer.visit(tree)

    for name, lineno in _unused_imports(tree, source):
        reviewer.findings.append(
            Finding(
                rule="unused-import",
                severity="low",
                line=lineno,
                message=f"导入的 {name} 未被使用",
                suggestion=f"删除 `{name}` 的导入，保持依赖清晰",
                snippet=reviewer.snippet(lineno),
            )
        )

    for index, line in enumerate(lines, start=1):
        if len(line) > LONG_LINE_CHARS and not line.strip().startswith("#"):
            reviewer.findings.append(
                Finding(
                    rule="long-line",
                    severity="info",
                    line=index,
                    message=f"该行长度 {len(line)} 超过 {LONG_LINE_CHARS} 字符",
                    suggestion="按 PEP 8 换行，或用括号自动续行",
                    snippet=line.strip()[:140],
                )
            )
        todo = TODO_PATTERN.search(line)
        if todo:
            reviewer.findings.append(
                Finding(
                    rule="todo-marker",
                    severity="info",
                    line=index,
                    message=f"遗留标记 {todo.group(1).upper()}: {todo.group(2).strip()[:80]}",
                    suggestion="确认是否需要在提交前处理",
                    snippet=line.strip()[:140],
                )
            )

    imports: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(f"{node.module or ''}:{','.join(a.name for a in node.names)}")

    summary = {
        "functions": reviewer.functions,
        "classes": reviewer.classes,
        "imports": imports,
        "total_lines": len(lines),
    }
    return reviewer.findings, summary


def format_findings(findings: List[Finding], summary: Dict[str, Any], path_label: str) -> str:
    """把发现列表渲染成紧凑文本（控制 token 消耗）。"""
    counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    header = (
        f"静态分析: {path_label} | 共 {len(findings)} 项发现 "
        f"(高 {counts['high']} / 中 {counts['medium']} / 低 {counts['low']} / 提示 {counts['info']})"
    )
    parts = [header]
    if findings:
        ordered = sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.line))
        parts.append("\n".join(finding.render() for finding in ordered))
    else:
        parts.append("未发现规则内的问题。")

    if summary.get("functions"):
        funcs = ", ".join(f"{name}:L{line}(复杂度{cx})" for name, line, _end, cx in summary["functions"][:15])
        parts.append(f"函数: {funcs}")
    if summary.get("classes"):
        parts.append("类: " + ", ".join(f"{name}:L{line}" for name, line in summary["classes"][:15]))
    parts.append(f"总行数: {summary.get('total_lines', 0)}")
    return "\n".join(parts)


def build_analysis_tools(workspace: Workspace) -> List[Tool]:
    """构造静态分析与结构理解工具。"""

    def analyze_code(path: str, min_severity: str = "info") -> ToolResult:
        target = workspace.resolve(path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{workspace.relative(target)} 是目录，请指定具体文件（可用 list_files 查找）")
        if target.suffix.lower() != ".py":
            raise ToolError(f"目前仅支持分析 Python 文件，收到: {workspace.relative(target)}")
        source = workspace.read_text(target)
        findings, summary = analyze_source(source, workspace.relative(target))
        threshold = SEVERITY_ORDER.get(min_severity, 3)
        filtered = [f for f in findings if SEVERITY_ORDER.get(f.severity, 9) <= threshold]
        text = format_findings(filtered, summary, workspace.relative(target))
        return ToolResult.success(
            text,
            high=sum(1 for f in filtered if f.severity == "high"),
            total=len(filtered),
        )

    def list_definitions(path: str) -> ToolResult:
        target = workspace.resolve(path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{workspace.relative(target)} 是目录，请指定具体文件")
        source = workspace.read_text(target)
        try:
            tree = ast.parse(source, filename=str(target))
        except SyntaxError as exc:
            raise ToolError(f"文件存在语法错误，无法解析结构: 第 {exc.lineno} 行 {exc.msg}") from exc

        lines = source.splitlines()
        rows: List[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rows.append(f"L{node.lineno:<4} def {node.name}{_signature(node)}")
            elif isinstance(node, ast.ClassDef):
                rows.append(f"L{node.lineno:<4} class {node.name}")
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        rows.append(f"L{child.lineno:<4}     .{child.name}{_signature(child)}")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                rows.append(f"L{node.lineno:<4} {lines[node.lineno - 1].strip()[:100]}")
        if not rows:
            rows.append("(未发现顶层定义)")
        return ToolResult.success(
            f"结构概览: {workspace.relative(target)}（{len(lines)} 行）\n" + "\n".join(rows),
            count=len(rows),
        )

    return [
        Tool(
            name="analyze_code",
            description=(
                "对 Python 文件做静态分析，返回带行号与严重级别的缺陷清单"
                "（异常吞噬、可变默认参数、None 比较、复杂度、未使用导入、安全风险等）。"
                "这是代码审查的确定性依据，请基于它的结果给出结论，不要凭空猜测行号。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要分析的 .py 文件路径"},
                    "min_severity": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "info"],
                        "description": "只显示不低于该级别的发现，默认 info（全部）",
                    },
                },
                "required": ["path"],
            },
            handler=analyze_code,
        ),
        Tool(
            name="list_definitions",
            description="列出 Python 文件的顶层导入、类与方法/函数签名及行号，用于快速理解代码结构。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "要解析的 .py 文件路径"}},
                "required": ["path"],
            },
            handler=list_definitions,
        ),
    ]


def _signature(node: Any) -> str:
    """把函数参数渲染成简洁签名。"""
    args = [arg.arg for arg in getattr(node.args, "args", [])]
    if getattr(node.args, "vararg", None):
        args.append("*" + node.args.vararg.arg)
    args.extend(arg.arg for arg in getattr(node.args, "kwonlyargs", []))
    if getattr(node.args, "kwarg", None):
        args.append("**" + node.args.kwarg.arg)
    return f"({', '.join(args)})"
