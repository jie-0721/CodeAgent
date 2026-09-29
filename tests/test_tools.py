"""工具层测试：沙箱边界、文件读写、检索、静态分析、代码执行。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.tools import Tool, ToolRegistry, ToolResult, Workspace, build_default_registry
from codeagent.tools.analysis import analyze_source

BUGGY_SOURCE = '''\
def f(a=[]):
    try:
        pass
    except:
        pass
    eval("1")
    return a
'''

CLEAN_SOURCE = '''\
"""A clean module."""


def add(a, b):
    """Return a + b."""
    return a + b
'''


class ToolTestCase(unittest.TestCase):
    """公共基类：为每个用例准备独立的工作目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = Workspace(self.root)
        self.registry = build_default_registry(self.workspace)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run_tool(self, name: str, **arguments) -> ToolResult:
        return self.registry.run(name, arguments)


class TestSandbox(ToolTestCase):
    def test_escape_is_rejected(self) -> None:
        result = self.run_tool("read_file", path="../secrets.txt")
        self.assertFalse(result.ok)
        self.assertIn("越界", result.content)

    def test_absolute_path_outside_is_rejected(self) -> None:
        outside = str(self.root.parent / "other.txt")
        result = self.run_tool("read_file", path=outside)
        self.assertFalse(result.ok)
        self.assertIn("越界", result.content)

    def test_write_escape_is_rejected(self) -> None:
        result = self.run_tool("write_file", path="../evil.py", content="x")
        self.assertFalse(result.ok)
        self.assertIn("越界", result.content)

    def test_workspace_rejects_missing_root(self) -> None:
        with self.assertRaises(Exception):
            Workspace(self.root / "不存在")


class TestFileTools(ToolTestCase):
    def test_read_file_numbers_lines(self) -> None:
        self.write("a.py", "line1\nline2\nline3\n")
        result = self.run_tool("read_file", path="a.py")
        self.assertTrue(result.ok)
        self.assertIn("1| line1", result.content)
        self.assertIn("3| line3", result.content)
        self.assertEqual(result.meta["total_lines"], 3)

    def test_read_file_window(self) -> None:
        self.write("a.py", "\n".join(f"line{i}" for i in range(1, 11)) + "\n")
        result = self.run_tool("read_file", path="a.py", start_line=3, max_lines=2)
        self.assertTrue(result.ok)
        self.assertIn("3| line3", result.content)
        self.assertIn("4| line4", result.content)
        self.assertNotIn("line5", result.content)
        self.assertIn("截断", result.content)

    def test_read_missing_file(self) -> None:
        result = self.run_tool("read_file", path="nope.py")
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.content)

    def test_read_directory_is_rejected(self) -> None:
        (self.root / "sub").mkdir()
        result = self.run_tool("read_file", path="sub")
        self.assertFalse(result.ok)
        self.assertIn("目录", result.content)

    def test_write_then_edit(self) -> None:
        created = self.run_tool("write_file", path="pkg/new.py", content="a = 1\n")
        self.assertTrue(created.ok)
        self.assertTrue((self.root / "pkg" / "new.py").is_file())

        overwrite_denied = self.run_tool("write_file", path="pkg/new.py", content="a = 2\n")
        self.assertFalse(overwrite_denied.ok)
        self.assertIn("已存在", overwrite_denied.content)

        edited = self.run_tool("edit_file", path="pkg/new.py", old_text="a = 1", new_text="a = 42")
        self.assertTrue(edited.ok)
        self.assertIn("a = 42", (self.root / "pkg" / "new.py").read_text(encoding="utf-8"))

    def test_edit_requires_unique_match(self) -> None:
        self.write("dup.py", "x = 1\nx = 1\n")
        ambiguous = self.run_tool("edit_file", path="dup.py", old_text="x = 1", new_text="x = 2")
        self.assertFalse(ambiguous.ok)
        self.assertIn("2 次", ambiguous.content)

        forced = self.run_tool("edit_file", path="dup.py", old_text="x = 1", new_text="x = 2", replace_all=True)
        self.assertTrue(forced.ok)
        self.assertEqual((self.root / "dup.py").read_text(encoding="utf-8"), "x = 2\nx = 2\n")

    def test_edit_missing_text(self) -> None:
        self.write("a.py", "a = 1\n")
        result = self.run_tool("edit_file", path="a.py", old_text="b = 2", new_text="b = 3")
        self.assertFalse(result.ok)
        self.assertIn("未找到", result.content)

    def test_list_files(self) -> None:
        self.write("a.py", "x")
        self.write("sub/b.py", "y")
        self.write("sub/c.txt", "z")
        (self.root / "__pycache__").mkdir()
        (self.root / "__pycache__" / "junk.py").write_text("junk", encoding="utf-8")

        result = self.run_tool("list_files", pattern="**/*.py")
        self.assertTrue(result.ok)
        self.assertIn("a.py", result.content)
        self.assertIn("sub/b.py", result.content)
        self.assertNotIn("junk.py", result.content)

    def test_search_text(self) -> None:
        self.write("a.py", "# TODO: 修一下\nb = 2\n")
        self.write("c.py", "c = 3\n")
        result = self.run_tool("search_text", pattern="TODO", include="*.py")
        self.assertTrue(result.ok)
        self.assertIn("a.py:1", result.content)
        self.assertEqual(result.meta["count"], 1)

    def test_search_text_invalid_regex_falls_back_to_literal(self) -> None:
        self.write("a.py", "value = compute(1)\n")
        result = self.run_tool("search_text", pattern="compute(1)")
        self.assertTrue(result.ok)
        self.assertIn("纯文本匹配", result.content)
        self.assertIn("a.py:1", result.content)

    def test_search_no_match(self) -> None:
        self.write("a.py", "x = 1\n")
        result = self.run_tool("search_text", pattern="绝不存在")
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["count"], 0)


class TestAnalysisTools(ToolTestCase):
    def test_analyze_code_finds_known_issues(self) -> None:
        self.write("buggy.py", BUGGY_SOURCE)
        result = self.run_tool("analyze_code", path="buggy.py")
        self.assertTrue(result.ok)
        self.assertIn("bare-except", result.content)
        self.assertIn("mutable-default", result.content)
        self.assertIn("dangerous-eval", result.content)
        self.assertGreaterEqual(result.meta["high"], 3)

    def test_analyze_code_clean_file(self) -> None:
        self.write("clean.py", CLEAN_SOURCE)
        result = self.run_tool("analyze_code", path="clean.py", min_severity="medium")
        self.assertTrue(result.ok)
        self.assertIn("共 0 项发现", result.content)

    def test_analyze_code_reports_syntax_error(self) -> None:
        self.write("broken.py", "def f(:\n    pass\n")
        result = self.run_tool("analyze_code", path="broken.py")
        self.assertTrue(result.ok)
        self.assertIn("syntax-error", result.content)

    def test_analyze_source_detects_unused_import(self) -> None:
        findings, _ = analyze_source("import os\n\n\nx = 1\n", "t.py")
        self.assertIn("unused-import", {finding.rule for finding in findings})

    def test_analyze_rejects_non_python(self) -> None:
        self.write("a.txt", "hello")
        result = self.run_tool("analyze_code", path="a.txt")
        self.assertFalse(result.ok)
        self.assertIn("Python", result.content)

    def test_list_definitions(self) -> None:
        self.write("m.py", "import os\n\n\nclass A:\n    def m(self, x):\n        pass\n\n\ndef top(a, b=1):\n    pass\n")
        result = self.run_tool("list_definitions", path="m.py")
        self.assertTrue(result.ok)
        self.assertIn("class A", result.content)
        self.assertIn("top(a, b)", result.content)
        self.assertIn("import os", result.content)


class TestExecTools(ToolTestCase):
    def test_run_python_code(self) -> None:
        result = self.run_tool("run_python", code="print('hello from tool')")
        self.assertTrue(result.ok)
        self.assertIn("hello from tool", result.content)
        self.assertIn("退出码: 0", result.content)

    def test_run_python_reports_failure(self) -> None:
        result = self.run_tool("run_python", code="raise SystemExit(3)")
        self.assertFalse(result.ok)
        self.assertEqual(result.meta["returncode"], 3)

    def test_run_python_file(self) -> None:
        self.write("script.py", "print('from file')\n")
        result = self.run_tool("run_python", path="script.py")
        self.assertTrue(result.ok)
        self.assertIn("from file", result.content)

    def test_run_python_timeout(self) -> None:
        result = self.run_tool("run_python", code="import time\ntime.sleep(10)", timeout=1)
        self.assertFalse(result.ok)
        self.assertTrue(result.meta["timed_out"])

    def test_run_python_requires_exactly_one_input(self) -> None:
        both = self.run_tool("run_python", code="print(1)", path="a.py")
        self.assertFalse(both.ok)
        neither = self.run_tool("run_python")
        self.assertFalse(neither.ok)


class TestToolContract(ToolTestCase):
    def test_unknown_tool_returns_failure(self) -> None:
        result = self.run_tool("not_a_tool")
        self.assertFalse(result.ok)
        self.assertIn("未注册", result.content)

    def test_unknown_argument_is_reported(self) -> None:
        result = self.run_tool("read_file", path="a.py", bogus=1)
        self.assertFalse(result.ok)
        self.assertIn("不支持参数", result.content)

    def test_missing_required_argument_is_reported(self) -> None:
        result = self.run_tool("read_file")
        self.assertFalse(result.ok)
        self.assertIn("缺少必填参数", result.content)

    def test_integer_coercion(self) -> None:
        self.write("a.py", "1\n2\n3\n")
        result = self.run_tool("read_file", path="a.py", start_line="2", max_lines="1")
        self.assertTrue(result.ok)
        self.assertIn("2| 2", result.content)

    def test_handler_exception_is_captured(self) -> None:
        def boom(**kwargs):
            raise RuntimeError("炸了")

        tool = Tool(name="boom", description="d", parameters={"type": "object", "properties": {}}, handler=boom)
        result = tool.invoke({})
        self.assertFalse(result.ok)
        self.assertIn("RuntimeError", result.content)

    def test_schemas_are_openai_shaped(self) -> None:
        registry = self.registry
        schemas = registry.schemas()
        self.assertEqual(len(schemas), len(registry))
        for schema in schemas:
            self.assertEqual(schema["type"], "function")
            self.assertIn("name", schema["function"])
            self.assertIn("parameters", schema["function"])

    def test_registry_rejects_duplicate_names(self) -> None:
        registry = ToolRegistry()
        tool = Tool(name="t", description="d", parameters={"type": "object", "properties": {}}, handler=lambda **_: "ok")
        registry.register(tool)
        with self.assertRaises(Exception):
            registry.register(tool)


if __name__ == "__main__":
    unittest.main()
