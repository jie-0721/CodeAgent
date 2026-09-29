"""CLI 层测试：斜杠命令派发、端到端 ask 子命令、参数构建。"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from codeagent.agent import Agent
from codeagent.cli import (
    Console,
    _build_slash_commands,
    _handle_slash_command,
    build_config,
    build_parser,
    main,
)
from codeagent.config import Config
from codeagent.memory import Memory
from codeagent.tools import Workspace, build_default_registry

from tests.test_agent import ScriptedLLM


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.console = Console(color=False)
        self.config = Config(provider="mock", workspace=self.root, stream=False)
        self.agent = Agent(
            llm=ScriptedLLM([]),
            tools=build_default_registry(Workspace(self.root)),
            config=self.config,
            memory=Memory(system_prompt="sys"),
            approver=lambda tool, arguments: True,
        )
        self.commands = _build_slash_commands(self.agent, self.config, self.console)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def capture(self, func, *args):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = func(*args)
        return result, buffer.getvalue()


class TestSlashCommands(CliTestCase):
    def test_all_documented_commands_are_registered(self) -> None:
        for name in ("help", "tools", "config", "review", "memory", "history", "reset", "save", "load", "verbose", "exit", "quit"):
            self.assertIn(name, self.commands, msg=name)

    def test_exit_variants(self) -> None:
        for text in ("/exit", "/quit", "/q", "/EXIT"):
            should_exit, _ = self.capture(_handle_slash_command, text, self.commands, self.console)
            self.assertTrue(should_exit, msg=text)

    def test_unknown_command_does_not_exit(self) -> None:
        should_exit, output = self.capture(_handle_slash_command, "/nope", self.commands, self.console)
        self.assertFalse(should_exit)
        self.assertIn("未知命令", output)

    def test_tools_command_lists_every_tool(self) -> None:
        _, output = self.capture(_handle_slash_command, "/tools", self.commands, self.console)
        for name in self.agent.tools.names():
            self.assertIn(name, output)

    def test_reset_clears_history(self) -> None:
        self.agent.memory.add_user("hi")
        self.capture(_handle_slash_command, "/reset", self.commands, self.console)
        self.assertEqual(self.agent.memory.messages, [])

    def test_verbose_toggles(self) -> None:
        self.assertFalse(self.config.verbose)
        self.capture(_handle_slash_command, "/verbose", self.commands, self.console)
        self.assertTrue(self.config.verbose)

    def test_save_writes_session_file(self) -> None:
        self.agent.memory.add_user("hi")
        target = self.root / "s.json"
        self.capture(_handle_slash_command, f"/save {target}", self.commands, self.console)
        self.assertTrue(target.is_file())

    def test_load_reports_bad_path(self) -> None:
        should_exit, output = self.capture(_handle_slash_command, "/load 不存在.json", self.commands, self.console)
        self.assertFalse(should_exit)
        self.assertIn("加载失败", output)

    def test_review_without_argument_prints_usage(self) -> None:
        _, output = self.capture(_handle_slash_command, "/review", self.commands, self.console)
        self.assertIn("用法", output)

    def test_command_argument_is_parsed(self) -> None:
        """带空格的命令参数应完整传给处理函数。"""
        target = self.root / "有 空格.json"
        self.capture(_handle_slash_command, f"/save {target}", self.commands, self.console)
        self.assertTrue(target.is_file())


class TestParser(unittest.TestCase):
    def test_subcommands_exist(self) -> None:
        parser = build_parser()
        for command in ("chat", "ask", "demo", "analyze", "tools", "web"):
            args = parser.parse_args([command, "x"] if command in {"ask", "analyze"} else [command])
            self.assertEqual(args.command, command)

    def test_requires_subcommand(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])

    def test_advanced_options_are_wired(self) -> None:
        """--timeout / --max-retries / --memory-tokens 必须真正透传到配置。"""
        args = build_parser().parse_args(
            [
                "ask", "--no-color", "--no-stream",
                "--timeout", "5", "--max-retries", "1", "--memory-tokens", "1234",
                "--temperature", "0.9", "--max-steps", "3",
                "任务",
            ]
        )
        config = build_config(args, Console(color=False))
        self.assertEqual(config.timeout, 5.0)
        self.assertEqual(config.max_retries, 1)
        self.assertEqual(config.memory_token_budget, 1234)
        self.assertAlmostEqual(config.temperature, 0.9)
        self.assertEqual(config.max_steps, 3)
        self.assertFalse(config.stream)


class TestEndToEnd(CliTestCase):
    def test_ask_with_mock_provider(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(
                [
                    "ask", "--provider", "mock", "--no-color", "--no-stream",
                    "--workspace", str(self.root), "有哪些文件",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("工作目录概览", buffer.getvalue())

    def test_analyze_subcommand_flags_high_severity(self) -> None:
        buggy = self.root / "buggy.py"
        buggy.write_text("def f(a=[]):\n    try:\n        pass\n    except:\n        pass\n", encoding="utf-8")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["analyze", "--no-color", "--workspace", str(self.root), "buggy.py"])
        self.assertEqual(code, 1)  # 存在 high 级别问题
        self.assertIn("mutable-default", buffer.getvalue())
        self.assertIn("bare-except", buffer.getvalue())

    def test_analyze_subcommand_missing_file(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["analyze", "--no-color", "--workspace", str(self.root), "不存在.py"])
        self.assertEqual(code, 2)

    def test_tools_subcommand(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["tools", "--provider", "mock", "--no-color", "--workspace", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("read_file", buffer.getvalue())
        self.assertIn("analyze_code", buffer.getvalue())

    def test_demo_subcommand_runs_offline(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(
                ["demo", "--scenario", "search", "--no-color", "--no-stream", "--workspace", str(self.root)]
            )
        self.assertEqual(code, 0)
        self.assertIn("离线演示", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
