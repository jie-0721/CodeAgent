"""记忆模块测试：token 估算、预算裁剪、工具调用配对完整性、落盘恢复。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.llm import Message, ToolCall
from codeagent.memory import Memory, estimate_tokens, message_tokens


class TestTokenEstimate(unittest.TestCase):
    def test_empty_text(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)

    def test_english_and_chinese(self) -> None:
        self.assertGreater(estimate_tokens("hello world"), 0)
        self.assertGreater(estimate_tokens("你好，世界"), 0)

    def test_tool_calls_add_cost(self) -> None:
        plain = Message.assistant("hi")
        with_call = Message.assistant("hi", [ToolCall(id="1", name="read_file", arguments='{"path":"a.py"}')])
        self.assertGreater(message_tokens(with_call), message_tokens(plain))


class TestMemory(unittest.TestCase):
    def test_build_always_contains_system(self) -> None:
        memory = Memory(system_prompt="你是助手", token_budget=1000)
        memory.add_user("你好")
        built = memory.build()
        self.assertEqual(built[0].role, "system")
        self.assertIn("你是助手", built[0].content)

    def test_trimming_keeps_recent_messages(self) -> None:
        memory = Memory(system_prompt="sys", token_budget=600)
        for index in range(40):
            memory.add_user(f"第 {index} 条消息 " + "内容" * 20)
        built = memory.build()
        self.assertLess(len(built), 41)
        self.assertGreater(len(built), 1)
        self.assertEqual(built[-1].content, memory.messages[-1].content)

    def test_trimming_does_not_orphan_tool_message(self) -> None:
        """裁剪后第一条非 system 消息不能是 tool（否则接口会报 400）。"""
        memory = Memory(system_prompt="sys", token_budget=500)
        memory.add_user("开始")
        memory.add_assistant("", [ToolCall(id="c1", name="read_file", arguments="{}")])
        memory.add_tool_result("c1", "read_file", "很长很长的一堆文件内容 " * 60)
        memory.add_user("继续")
        built = memory.build()
        body = built[1:]
        self.assertTrue(body)
        self.assertNotEqual(body[0].role, "tool")

    def test_dropped_count_notice(self) -> None:
        memory = Memory(system_prompt="sys", token_budget=400)
        for index in range(30):
            memory.add_user("消息" * 30 + str(index))
        memory.build()
        self.assertGreater(memory.dropped_count, 0)
        self.assertIn("省略", memory.build()[0].content)

    def test_stats(self) -> None:
        memory = Memory(system_prompt="sys", token_budget=1000)
        memory.add_user("a")
        memory.add_assistant("b")
        stats = memory.stats()
        self.assertEqual(stats["messages"], 2)
        self.assertEqual(stats["by_role"]["user"], 1)
        self.assertGreater(stats["tokens"], 0)

    def test_save_and_load_roundtrip(self) -> None:
        memory = Memory(system_prompt="sys", token_budget=1000)
        memory.add_user("问题")
        memory.add_assistant("思考", [ToolCall(id="c1", name="analyze_code", arguments='{"path": "a.py"}')])
        memory.add_tool_result("c1", "analyze_code", "发现 1 项问题")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            memory.save(path)
            loaded = Memory.load(path)
        self.assertEqual(len(loaded.messages), 3)
        self.assertEqual(loaded.messages[1].tool_calls[0].name, "analyze_code")
        self.assertEqual(loaded.messages[2].tool_call_id, "c1")

    def test_load_missing_file_raises(self) -> None:
        from codeagent.errors import AgentError

        with self.assertRaises(AgentError):
            Memory.load(Path("不存在的会话.json"))

    def test_clear_keeps_system_prompt(self) -> None:
        memory = Memory(system_prompt="sys")
        memory.add_user("a")
        memory.clear()
        self.assertEqual(memory.messages, [])
        self.assertEqual(memory.build()[0].content, "sys")


if __name__ == "__main__":
    unittest.main()
