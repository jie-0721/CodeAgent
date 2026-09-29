"""Mock LLM 测试：意图识别、工具计划、最终答复渲染、流式输出。

Mock LLM 既是「无 API Key 演示」的基础，也是 Agent 集成测试的确定性驱动源，
因此它本身也需要被测试覆盖。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codeagent.agent import Agent
from codeagent.config import Config
from codeagent.llm import Message, ToolCall
from codeagent.memory import Memory
from codeagent.mock import MockLLM
from codeagent.tools import Workspace, build_default_registry

BUGGY = """\
def load(path, cache=[]):
    try:
        return cache
    except:
        return []
"""


class MockLLMTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "buggy.py").write_text(BUGGY, encoding="utf-8")
        (self.root / "clean.py").write_text('"""doc"""\n\n\ndef add(a, b):\n    """doc"""\n    return a + b\n', encoding="utf-8")
        self.workspace = Workspace(self.root)
        self.tools = build_default_registry(self.workspace)
        self.config = Config(provider="mock", workspace=self.root, max_steps=6, stream=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_agent(self, prompt: str):
        memory = Memory(system_prompt="sys", token_budget=8000)
        agent = Agent(
            llm=MockLLM(self.config),
            tools=self.tools,
            config=self.config,
            memory=memory,
            approver=lambda tool, arguments: True,
        )
        return agent.run(prompt)


class TestIntentDetection(MockLLMTestCase):
    def test_intents(self) -> None:
        cases = {
            "请审查 buggy.py 的代码质量": "review",
            "解释一下 buggy.py 的逻辑": "explain",
            "给 buggy.py 生成单元测试": "test",
            "运行 buggy.py": "run",
            "搜索项目里的 TODO": "search",
            "有哪些文件": "list",
            "今天天气怎么样": "generic",
        }
        for text, expected in cases.items():
            self.assertEqual(MockLLM._detect_intent(text), expected, msg=text)

    def test_target_extraction(self) -> None:
        self.assertEqual(MockLLM._extract_target("审查 examples/buggy_code.py"), "examples/buggy_code.py")
        self.assertEqual(MockLLM._extract_target("看一下 src/utils.js 的实现"), "src/utils.js")
        self.assertIsNone(MockLLM._extract_target("帮我看看代码"))

    def test_search_term_extraction(self) -> None:
        self.assertEqual(MockLLM._extract_search_term("搜索 'load_users' 在哪里"), "load_users")
        self.assertEqual(MockLLM._extract_search_term("查找 TODO"), "TODO")


class TestPlanIntegration(MockLLMTestCase):
    def test_review_scenario_runs_two_tools(self) -> None:
        result = self.run_agent("请审查 buggy.py 的代码质量")
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual([step.tool for step in result.steps], ["analyze_code", "read_file"])
        self.assertIn("代码审查报告", result.answer)
        self.assertIn("bare-except", result.answer)
        self.assertIn("buggy.py:4", result.answer)  # 行号来自真实分析结果

    def test_explain_scenario(self) -> None:
        result = self.run_agent("解释 clean.py 的结构和主要逻辑")
        self.assertEqual([step.tool for step in result.steps], ["list_definitions", "read_file"])
        self.assertIn("代码说明", result.answer)

    def test_search_scenario(self) -> None:
        result = self.run_agent("搜索项目里所有的 TODO")
        self.assertEqual([step.tool for step in result.steps], ["search_text"])
        self.assertIn("检索结果", result.answer)

    def test_listing_scenario_when_no_file_given(self) -> None:
        result = self.run_agent("有哪些文件")
        self.assertEqual([step.tool for step in result.steps], ["list_files"])
        self.assertIn("工作目录概览", result.answer)

    def test_does_not_repeat_completed_call(self) -> None:
        """已完成 (工具, 参数) 不应被重复规划，否则会触发循环熔断。"""
        mock = MockLLM(self.config)
        messages = [
            Message.system("sys"),
            Message.user("请审查 buggy.py 的代码质量"),
            Message.assistant(
                "",
                [ToolCall(id="c1", name="analyze_code", arguments=json.dumps({"path": "buggy.py"}))],
            ),
            Message.tool("c1", "analyze_code", "静态分析: buggy.py | 共 1 项发现 (高 1 / 中 0 / 低 0 / 提示 0)"),
        ]
        schemas = self.tools.schemas()
        message = mock.generate(messages, tools=schemas)
        self.assertEqual(len(message.tool_calls), 1)
        self.assertEqual(message.tool_calls[0].name, "read_file")

    def test_final_message_when_tools_disabled(self) -> None:
        """tools=None（强制收敛）时必须直接产出文本而不是继续请求工具。"""
        mock = MockLLM(self.config)
        messages = [Message.system("sys"), Message.user("请审查 buggy.py 的代码质量")]
        message = mock.generate(messages, tools=None)
        self.assertEqual(message.tool_calls, [])
        self.assertTrue(message.content)

    def test_unavailable_tool_is_skipped(self) -> None:
        """计划中的工具如果没下发给模型，应跳过而不是产生非法调用。"""
        mock = MockLLM(self.config)
        messages = [Message.system("sys"), Message.user("请审查 buggy.py 的代码质量")]
        limited = [schema for schema in self.tools.schemas() if schema["function"]["name"] == "read_file"]
        message = mock.generate(messages, tools=limited)
        self.assertEqual(message.tool_calls[0].name, "read_file")


class TestStreaming(MockLLMTestCase):
    def test_deltas_reconstruct_content(self) -> None:
        mock = MockLLM(self.config)
        chunks: list = []
        message = mock.generate(
            [Message.system("sys"), Message.user("解释 clean.py 的结构")],
            tools=None,
            on_delta=chunks.append,
        )
        self.assertGreater(len(chunks), 1)  # 分块输出
        self.assertEqual("".join(chunks), message.content)

    def test_thought_is_streamed_before_tool_call(self) -> None:
        mock = MockLLM(self.config)
        chunks: list = []
        message = mock.generate(
            [Message.system("sys"), Message.user("请审查 buggy.py 的代码质量")],
            tools=self.tools.schemas(),
            on_delta=chunks.append,
        )
        self.assertEqual(message.tool_calls[0].name, "analyze_code")
        self.assertTrue("".join(chunks))
        self.assertEqual("".join(chunks), message.content)

    def test_agent_streams_final_answer_through_events(self) -> None:
        events: list = []
        memory = Memory(system_prompt="sys", token_budget=8000)
        agent = Agent(
            llm=MockLLM(self.config),
            tools=self.tools,
            config=self.config,
            memory=memory,
            on_event=events.append,
            approver=lambda tool, arguments: True,
        )
        agent.run("请审查 buggy.py 的代码质量")
        deltas = [event["content"] for event in events if event["type"] == "assistant_delta"]
        final = next(event for event in events if event["type"] == "final")
        self.assertTrue(deltas)
        self.assertIn("代码审查报告", "".join(deltas))
        self.assertIn("代码审查报告", final["content"])


if __name__ == "__main__":
    unittest.main()
