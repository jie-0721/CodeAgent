"""Agent 主循环测试：多步工具调用、错误自愈、权限门、步数上限、循环熔断、事件流。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from codeagent.agent import Agent
from codeagent.config import Config
from codeagent.llm import BaseLLM, Message, ToolCall
from codeagent.memory import Memory
from codeagent.tools import Workspace, build_default_registry


class ScriptedLLM(BaseLLM):
    """按脚本返回预设响应，用于确定性测试 Agent 循环。"""

    name = "scripted"

    def __init__(self, script: Optional[List[Message]] = None, responder: Optional[Any] = None) -> None:
        self.script = list(script or [])
        self.responder = responder
        self.calls: List[Dict[str, Any]] = []

    def generate(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_delta: Optional[Any] = None,
        stream: bool = True,
    ) -> Message:
        index = len(self.calls)
        self.calls.append({"messages": list(messages), "tools": tools})
        if self.responder is not None:
            message = self.responder(index, messages, tools)
        elif self.script:
            message = self.script.pop(0)
        else:
            message = Message.assistant("(脚本已耗尽)")
        if on_delta is not None and message.content:
            on_delta(message.content)
        return message


def tool_call(name: str, arguments: Dict[str, Any], call_id: str = "call_1") -> Message:
    return Message.assistant("", [ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))])


class AgentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
        self.workspace = Workspace(self.root)
        self.tools = build_default_registry(self.workspace)
        self.events: List[Dict[str, Any]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_agent(
        self,
        llm: BaseLLM,
        max_steps: int = 6,
        approver: Optional[Any] = None,
        auto_approve: bool = False,
        stream: bool = True,
    ) -> Agent:
        config = Config(
            provider="mock",
            workspace=self.root,
            max_steps=max_steps,
            auto_approve=auto_approve,
            stream=stream,
            memory_token_budget=8000,
        )
        return Agent(
            llm=llm,
            tools=self.tools,
            config=config,
            memory=Memory(token_budget=8000),
            on_event=self.events.append,
            approver=approver,
        )


class TestLoop(AgentTestCase):
    def test_direct_answer_without_tools(self) -> None:
        agent = self.make_agent(ScriptedLLM([Message.assistant("你好，我是 CodeAgent。")]))
        result = agent.run("你好")
        self.assertEqual(result.answer, "你好，我是 CodeAgent。")
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual([m.role for m in agent.memory.messages], ["user", "assistant"])

    def test_tool_call_then_final(self) -> None:
        llm = ScriptedLLM(
            [
                tool_call("read_file", {"path": "a.py"}),
                Message.assistant("文件里有两行赋值。"),
            ]
        )
        agent = self.make_agent(llm)
        result = agent.run("读一下 a.py")

        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(result.tool_calls, 1)
        self.assertTrue(result.steps[0].ok)
        self.assertIn("x = 1", result.steps[0].observation)

        roles = [message.role for message in agent.memory.messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])

        # 第二次请求必须带上 tool 结果，且声明了工具 schema
        second_call = llm.calls[1]
        self.assertTrue(any(message.role == "tool" for message in second_call["messages"]))
        self.assertTrue(second_call["tools"])
        self.assertEqual(second_call["tools"][0]["type"], "function")

    def test_multiple_tool_calls_in_one_step(self) -> None:
        parallel = Message.assistant(
            "同时读取两个文件",
            [
                ToolCall(id="c1", name="read_file", arguments=json.dumps({"path": "a.py"})),
                ToolCall(id="c2", name="list_files", arguments=json.dumps({"pattern": "*.py"})),
            ],
        )
        agent = self.make_agent(ScriptedLLM([parallel, Message.assistant("都读完了。")]))
        result = agent.run("看看文件")
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual([step.tool for step in result.steps], ["read_file", "list_files"])
        tool_messages = [m for m in agent.memory.messages if m.role == "tool"]
        self.assertEqual([m.tool_call_id for m in tool_messages], ["c1", "c2"])

    def test_tool_failure_is_fed_back_and_agent_recovers(self) -> None:
        llm = ScriptedLLM(
            [
                tool_call("read_file", {"path": "missing.py"}),
                Message.assistant("文件不存在，我先列出目录。"),
            ]
        )
        agent = self.make_agent(llm)
        result = agent.run("读 missing.py")
        self.assertFalse(result.steps[0].ok)
        self.assertIn("执行失败", result.steps[0].observation)
        self.assertEqual(result.stopped_reason, "final")

    def test_unknown_tool_is_reported(self) -> None:
        llm = ScriptedLLM([tool_call("hack_the_planet", {}), Message.assistant("换个方式。")])
        agent = self.make_agent(llm)
        result = agent.run("做点什么")
        self.assertFalse(result.steps[0].ok)
        self.assertIn("未注册", result.steps[0].observation)

    def test_bad_json_arguments_are_reported(self) -> None:
        broken = Message.assistant("", [ToolCall(id="c1", name="read_file", arguments="{不是 JSON")])
        agent = self.make_agent(ScriptedLLM([broken, Message.assistant("参数写错了。")]))
        result = agent.run("读文件")
        self.assertFalse(result.steps[0].ok)
        self.assertIn("合法 JSON", result.steps[0].observation)


class TestPermissions(AgentTestCase):
    def test_denied_write_does_not_touch_filesystem(self) -> None:
        llm = ScriptedLLM(
            [
                tool_call("write_file", {"path": "new.py", "content": "print(1)"}),
                Message.assistant("用户拒绝了写操作。"),
            ]
        )
        agent = self.make_agent(llm, approver=lambda tool, arguments: False)
        result = agent.run("写个文件")
        self.assertFalse(result.steps[0].ok)
        self.assertIn("拒绝", result.steps[0].observation)
        self.assertFalse((self.root / "new.py").exists())

    def test_approved_write_creates_file(self) -> None:
        llm = ScriptedLLM(
            [
                tool_call("write_file", {"path": "new.py", "content": "print('hi')\n"}),
                Message.assistant("已写入。"),
            ]
        )
        agent = self.make_agent(llm, approver=lambda tool, arguments: True)
        result = agent.run("写个文件")
        self.assertTrue(result.steps[0].ok)
        self.assertTrue((self.root / "new.py").is_file())

    def test_read_only_tool_needs_no_approval(self) -> None:
        called: List[str] = []

        def approver(tool: Any, arguments: Dict[str, Any]) -> bool:
            called.append(tool.name)
            return False

        agent = self.make_agent(
            ScriptedLLM([tool_call("read_file", {"path": "a.py"}), Message.assistant("done")]),
            approver=approver,
        )
        result = agent.run("读文件")
        self.assertTrue(result.steps[0].ok)
        self.assertEqual(called, [])

    def test_auto_approve_skips_approver(self) -> None:
        called: List[str] = []

        def approver(tool: Any, arguments: Dict[str, Any]) -> bool:
            called.append(tool.name)
            return False

        agent = self.make_agent(
            ScriptedLLM([tool_call("write_file", {"path": "auto.py", "content": "x"}), Message.assistant("ok")]),
            approver=approver,
            auto_approve=True,
        )
        result = agent.run("写文件")
        self.assertTrue(result.steps[0].ok)
        self.assertEqual(called, [])


class TestGuards(AgentTestCase):
    def test_max_steps_triggers_forced_finalize(self) -> None:
        def responder(index: int, messages: Sequence[Message], tools: Optional[Any]) -> Message:
            if tools is None:  # 强制收敛阶段：不再提供工具
                return Message.assistant("已达到步数上限，我的阶段性结论是……")
            return tool_call("read_file", {"path": "a.py"}, call_id=f"c{index}")

        agent = self.make_agent(ScriptedLLM(responder=responder), max_steps=2)
        result = agent.run("一直读文件")
        self.assertEqual(result.stopped_reason, "max_steps")
        self.assertEqual(result.tool_calls, 2)
        self.assertIn("阶段性结论", result.answer)
        self.assertTrue(any(event["type"] == "warning" for event in self.events))

    def test_repeated_identical_call_triggers_loop_detection(self) -> None:
        def responder(index: int, messages: Sequence[Message], tools: Optional[Any]) -> Message:
            if tools is None:
                return Message.assistant("似乎陷入了重复调用，先给结论。")
            return tool_call("read_file", {"path": "a.py"}, call_id=f"c{index}")

        agent = self.make_agent(ScriptedLLM(responder=responder), max_steps=10)
        result = agent.run("读取 a.py")
        self.assertEqual(result.stopped_reason, "loop_detected")
        self.assertTrue(result.answer)
        # 一定注入了纠正提示（user 消息里带有系统提醒）
        self.assertTrue(any("相同参数" in m.content for m in agent.memory.messages if m.role == "user"))

    def test_llm_error_produces_error_result(self) -> None:
        from codeagent.errors import LLMConnectionError

        def responder(index: int, messages: Sequence[Message], tools: Optional[Any]) -> Message:
            raise LLMConnectionError("网络断了")

        agent = self.make_agent(ScriptedLLM(responder=responder))
        result = agent.run("你好")
        self.assertEqual(result.stopped_reason, "error")
        self.assertIn("网络断了", result.answer)


class TestEvents(AgentTestCase):
    def test_event_sequence_and_streaming(self) -> None:
        agent = self.make_agent(
            ScriptedLLM([tool_call("read_file", {"path": "a.py"}), Message.assistant("读完了。")]),
            stream=True,
        )
        agent.run("读文件")
        types = [event["type"] for event in self.events]
        self.assertIn("step_start", types)
        self.assertIn("tool_call", types)
        self.assertIn("tool_result", types)
        self.assertIn("assistant_delta", types)
        self.assertEqual(types[-1], "final")

        tool_call_event = next(event for event in self.events if event["type"] == "tool_call")
        self.assertEqual(tool_call_event["name"], "read_file")
        self.assertEqual(tool_call_event["arguments"], {"path": "a.py"})

    def test_non_streaming_mode_has_no_deltas(self) -> None:
        agent = self.make_agent(ScriptedLLM([Message.assistant("直接回答")]), stream=False)
        agent.run("你好")
        self.assertFalse(any(event["type"] == "assistant_delta" for event in self.events))


class TestMemoryIntegration(AgentTestCase):
    def test_reset_clears_history(self) -> None:
        agent = self.make_agent(ScriptedLLM([Message.assistant("ok")]))
        agent.run("你好")
        self.assertTrue(agent.memory.messages)
        agent.reset()
        self.assertEqual(agent.memory.messages, [])
        self.assertTrue(agent.memory.system_prompt)

    def test_session_roundtrip(self) -> None:
        agent = self.make_agent(ScriptedLLM([Message.assistant("ok")]))
        agent.run("你好")
        path = self.root / "session.json"
        agent.save_session(path)

        other = self.make_agent(ScriptedLLM([Message.assistant("ok")]))
        other.load_session(path)
        self.assertEqual(len(other.memory.messages), 2)
        self.assertEqual(other.memory.messages[0].content, "你好")

    def test_memory_stats(self) -> None:
        agent = self.make_agent(ScriptedLLM([Message.assistant("ok")]))
        agent.run("你好")
        stats = agent.memory_stats()
        self.assertEqual(stats["messages"], 2)
        self.assertGreater(stats["tokens"], 0)


if __name__ == "__main__":
    unittest.main()
